"""Solana verification build: ``cargo certora-sbf`` and its JSON output.

The slow compile gate (:mod:`composer.cargo.session` is the fast one), and the
point where the backend and ``certoraSolanaProver`` agree on what was built.

``cargo certora-sbf --json`` prints a JSON document (stdout; compile log on
stderr). ``certoraSolanaProver``'s ``build_script`` is defined as "run this and
read that JSON from stdout" (``CertoraProver/certoraParseBuildScript.py``). The
backend runs the build, reads the JSON, and gives the prover a
:func:`build_script` that reruns the same command, confined the same way.

The prover reruns the build instead of replaying a saved manifest. The second
run is a warm cargo no-op. A saved manifest goes stale if anything moves, and
the prover can then upload the wrong sources or a missing artifact.

The conf uses a build script instead of handing over the finished ``.so`` via
``files``. ``set_rust_build_directory`` only copies the project's Rust sources
into ``.certora_sources`` on the build-script path; with ``files`` it copies the
artifact and nothing else. ``certoraSolanaProver``'s own from-sources path also
runs ``cargo certora-sbf`` unconfined inside its process, which the sandbox does
not allow.
"""

import asyncio
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from composer.cargo.session import CargoSession, CompileFailed, WarmFailed

_log = logging.getLogger(__name__)

#: Certora's tool, not ``cargo-build-sbf``: it pins the platform-tools version
#: and prints the build manifest.
SBF_SUBCOMMAND = "certora-sbf"

#: Same default and override variable as the tool. Both the build's argv and its
#: read-only grant are taken from this one value.
PLATFORM_TOOLS_ROOT = Path(
    os.environ.get("CERTORA_PLATFORM_TOOLS_ROOT", Path.home() / ".cache" / "solana")
)

BUILD_TIMEOUT_S = 1800

#: Under the workdir (the only path the confinement policy grants read-write).
BUILD_DIR = Path(".certora_build")


class PlatformToolsMissing(RuntimeError):
    """The requested platform-tools version is not installed, and a confined
    build cannot download it the way ``cargo certora-sbf`` would unconfined."""

    def __init__(self, version: str, root: Path):
        self.version = version
        self.root = root
        super().__init__(
            f"Solana platform tools {version} are not installed under {root}. A confined build "
            f"cannot fetch them (no network, and the cache is read-only). Install them once, "
            f"unconfined, with `cargo certora-sbf --tools-version {version}` in any Solana crate, "
            f"or point CERTORA_PLATFORM_TOOLS_ROOT at a root that has them."
        )


class SbfSubcommandMissing(RuntimeError):
    """``cargo certora-sbf`` is not installed."""

    def __init__(self, detail: str):
        super().__init__(
            f"`cargo certora-sbf` is not available: {detail}. The pre-submission build and the "
            f"prover's build script both run it. Install it with `cargo install cargo-certora-sbf` "
            f"(Rust 1.81 or newer)."
        )


async def sbf_subcommand_version() -> str:
    """Output of ``cargo certora-sbf --version``, or raise :class:`SbfSubcommandMissing`.

    Runs the subcommand instead of searching ``PATH``. Cargo also looks in
    ``$CARGO_HOME/bin`` and the active toolchain's libexec, so a failed ``which``
    would reject working installs.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "cargo", SBF_SUBCOMMAND, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise SbfSubcommandMissing(f"cargo could not be run ({exc})") from exc
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise SbfSubcommandMissing(stderr.decode().strip() or f"exit {proc.returncode}")
    return stdout.decode().strip()


def _one_or_many(value: object) -> object:
    """``certoraParseBuildScript.add_solana_files_to_context`` reads a string as one path."""
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(value)
    return value


type _SolanaFiles = Annotated[tuple[str, ...], BeforeValidator(_one_or_many)]


class BuildManifest(BaseModel):
    """Output of ``cargo certora-sbf --json``, checked against what the prover requires.

    Paths are as cargo printed them: ``project_directory`` absolute, everything
    else relative to it. ``certoraParseBuildScript`` resolves them against
    ``project_directory``, so rewriting them here would add a second convention.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    #: The prover refuses a manifest that reports a failed build.
    success: Literal[True]
    project_directory: Path
    executables: str
    sources: tuple[str, ...]
    solana_inlining: _SolanaFiles = ()
    solana_summaries: _SolanaFiles = ()

    @property
    def artifact(self) -> Path:
        return self.project_directory / self.executables


class MalformedBuildManifest(ValueError):
    """``cargo certora-sbf --json`` printed something the prover would reject."""


def parse_manifest(stdout: str) -> BuildManifest:
    """Parse the build JSON into the shape ``certoraParseBuildScript`` accepts.

    Checked here so a bad manifest is a build-tool error next to the build, not a
    ``CertoraUserInputError`` inside a prover run that has already started.
    """
    try:
        return BuildManifest.model_validate_json(stdout)
    except ValidationError as exc:
        raise MalformedBuildManifest(f"unusable build manifest: {exc}") from exc


@dataclass(frozen=True)
class Built:
    manifest: BuildManifest


type SbfVerdict = Built | CompileFailed


@dataclass(frozen=True)
class SbfRun:
    duration_ms: int
    verdict: SbfVerdict
    confined: bool

    @property
    def ok(self) -> bool:
        return isinstance(self.verdict, Built)


def sbf_argv(
    *,
    manifest_path: Path,
    features: tuple[str, ...] = (),
    tools_version: str | None = None,
) -> list[str]:
    """``cargo certora-sbf`` arguments, shared by the direct build and the build script.

    ``--no-rustup`` is required under confinement. The tool otherwise registers a
    ``certora-solana`` rustup toolchain and writes to ``RUSTUP_HOME``, which is
    read-only.

    ``--platform-tools-root`` is passed on the argv, not left to
    ``$CERTORA_PLATFORM_TOOLS_ROOT``. The confined child never sees that
    variable: the launcher keeps only
    :data:`~composer.sandbox.recipes.DEFAULT_ENV_PASSTHROUGH`, which does not
    include it. The build and the build script grant the same root read-only.
    """
    args = [
        SBF_SUBCOMMAND,
        "--json",
        "--no-rustup",
        "--platform-tools-root",
        str(PLATFORM_TOOLS_ROOT),
        "--manifest-path",
        str(manifest_path),
    ]
    if tools_version is not None:
        args += ["--tools-version", tools_version]
    if features:
        args += ["--features", " ".join(features)]
    return args


def platform_tools_installed(version: str, *, root: Path = PLATFORM_TOOLS_ROOT) -> bool:
    return (root / version).is_dir()


def platform_tools_cargos(version: str, *, root: Path = PLATFORM_TOOLS_ROOT) -> tuple[Path, ...]:
    """``cargo`` binaries shipped with a platform-tools version.

    Not the cargo on ``PATH``. Cargo hashes a git source into a cache directory
    name, and that hash is not stable across cargo versions. Host cargo 1.89
    fetches ``Certora/anchor`` into ``git/db/anchor-8a7e45e4c93a95b5``; cargo
    1.79 in platform-tools v1.43 looks for ``git/db/anchor-1f3eb14fb7b4e8f1``,
    finds nothing, and offline reports::

        can't checkout from '...': you are in the offline mode (--offline)

    A version can ship both ``platform-tools`` and ``platform-tools-certora``.
    Warming both is cheap (they share the registry cache).
    """
    return tuple(
        cargo
        for flavour in ("platform-tools-certora", "platform-tools")
        if (cargo := root / version / flavour / "rust" / "bin" / "cargo").is_file()
    )


async def _warm_for_the_build_cargo(
    session: CargoSession, tools_version: str, manifest_path: Path
) -> None:
    """Fetch with the cargos the chain build will run. Confined builds only.

    A failure is logged, not raised — a partial cache still compiles what it
    has, and the build names the crate it could not find.
    """
    for cargo in platform_tools_cargos(tools_version):
        if session.already_warmed(cargo):
            continue
        outcome = await session.warm(manifest_dirs=(manifest_path.parent,), cargo=cargo)
        if isinstance(outcome, WarmFailed):
            _log.warning(
                "cargo fetch with %s (platform-tools %s) did not complete: %s",
                cargo, tools_version, outcome.diagnostics,
            )


async def sbf_build(
    session: CargoSession,
    *,
    manifest_path: Path,
    features: tuple[str, ...] = (),
    tools_version: str | None = None,
    timeout_s: int = BUILD_TIMEOUT_S,
) -> SbfRun:
    """Run the slow tier in ``session``'s workdir, confined.

    Raises :class:`PlatformToolsMissing` instead of returning a failed run when
    the toolchain is missing: that is an operator problem, not a Rust error an
    authoring agent can fix.
    """
    if tools_version is not None and session.confined and not platform_tools_installed(tools_version):
        raise PlatformToolsMissing(tools_version, PLATFORM_TOOLS_ROOT)
    if tools_version is not None and session.confined:
        await _warm_for_the_build_cargo(session, tools_version, manifest_path)
    argv = sbf_argv(manifest_path=manifest_path, features=features, tools_version=tools_version)
    started = time.perf_counter()
    built = await session.run_confined(
        "cargo", argv, timeout_s=timeout_s, extra_ro=(PLATFORM_TOOLS_ROOT,)
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    if built.exit_code != 0:
        return SbfRun(
            elapsed,
            CompileFailed(diagnostics=built.stderr.strip(), exit_code=built.exit_code),
            session.confined,
        )
    try:
        manifest = parse_manifest(built.stdout)
    except MalformedBuildManifest as exc:
        # Compiler succeeded; JSON did not. Report the manifest error, not stderr.
        return SbfRun(
            elapsed,
            CompileFailed(diagnostics=str(exc), exit_code=built.exit_code),
            session.confined,
        )
    return SbfRun(elapsed, Built(manifest), session.confined)


def build_command_path(script: Path) -> Path:
    """The command file a build script reads, which sits beside it."""
    return script.with_suffix(".json")


async def write_build_script(
    session: CargoSession,
    *,
    name: str,
    manifest_path: Path,
    features: tuple[str, ...] = (),
    tools_version: str | None = None,
    timeout_s: int = BUILD_TIMEOUT_S,
) -> Path:
    """Write the ``build_script`` the conf points at, and return its path.

    ``name`` keeps scripts apart when several submissions share one workdir.
    Confinement is an opaque ``argv_prefix`` from :meth:`CargoSession.backend_spec`
    (``docs/command-sandbox.md`` §4). If the provider cannot confine, that call
    raises before anything runs.
    """
    spec = await session.backend_spec(timeout_s=timeout_s, extra_ro=(PLATFORM_TOOLS_ROOT,))
    build_dir = session.workdir / BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    script = build_dir / f"{name}.py"
    build_command_path(script).write_text(
        json.dumps(
            {
                "cwd": str(session.workdir),
                "argv_prefix": spec["argv_prefix"],
                "argv": [
                    "cargo",
                    *sbf_argv(
                        manifest_path=manifest_path,
                        features=features,
                        tools_version=tools_version,
                    ),
                ],
            },
            indent=2,
        )
    )
    script.write_text((files("composer.cargo") / "sbf_build_script.py").read_text())
    # certoraRun execs the script directly, so the shebang needs the execute bit.
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script
