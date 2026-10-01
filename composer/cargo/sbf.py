"""Solana verification build: ``cargo certora-sbf`` and its JSON output.

The slow compile gate. :mod:`composer.cargo.session` is the fast one.

``cargo certora-sbf --json`` prints the build manifest on stdout and the
compile log on stderr. ``certoraSolanaProver`` reads that JSON from its build
script's stdout (``CertoraProver/certoraParseBuildScript.py``). This module
runs the build and writes a script that reruns the same command, confined the
same way.

The prover reruns the build. A saved manifest goes stale when sources move,
and the prover can then upload the wrong sources or a missing artifact. The
rerun compiles nothing new when that target directory already holds the
build. Each :class:`SbfBuild` has its own target directory, so two builds of
one crate with different features do not replace each other's ``.so``.

The conf names a build script. On that path, ``set_rust_build_directory``
copies the Rust sources into ``.certora_sources``. With ``files``, it copies
the ``.so`` and nothing else. The prover's own from-sources path runs
``cargo certora-sbf`` unconfined, which the sandbox does not allow.
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
    """What ``cargo certora-sbf --version`` prints.

    Runs the subcommand. Cargo also finds it under ``$CARGO_HOME/bin`` and the
    active toolchain's libexec, so a ``PATH`` search rejects working installs.
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

    ``project_directory`` is absolute. The other paths are relative to it.
    ``certoraParseBuildScript`` resolves them against ``project_directory``.
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

    A bad manifest fails here, before a prover run starts. During submission
    the same JSON is a ``CertoraUserInputError`` after the upload.
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


@dataclass(frozen=True)
class SbfBuild:
    """One ``cargo certora-sbf`` build. The gate runs it, and the build script reruns it.

    ``target_dir`` is absolute and under the workspace root. The tool reports
    the artifact relative to that root. A relative target directory is resolved
    against the working directory by ``cargo metadata`` and against the package
    directory by the build, so the reported path is not the file that was built.
    """

    manifest_path: Path
    target_dir: Path
    features: tuple[str, ...] = ()
    tools_version: str | None = None

    def __post_init__(self) -> None:
        if not self.target_dir.is_absolute():
            raise ValueError(f"target_dir must be absolute, not {self.target_dir}")

    def argv(self) -> list[str]:
        """The full command line, program included.

        The target directory is ``CARGO_TARGET_DIR`` on the argv.
        ``cargo certora-sbf`` has no flag for it, and it reads the artifact
        path from its own ``cargo metadata`` call, which ``--cargo-args`` does
        not reach. The launcher keeps only
        :data:`~composer.sandbox.recipes.DEFAULT_ENV_PASSTHROUGH`, so the same
        variable in the child's environment is dropped.

        ``--no-rustup`` is required. Without it the tool registers a
        ``certora-solana`` rustup toolchain and writes ``RUSTUP_HOME``, which
        is read-only under confinement.

        ``--platform-tools-root`` is on the argv. The launcher would drop
        ``$CERTORA_PLATFORM_TOOLS_ROOT``. The build and the build script grant
        that root read-only.
        """
        args = [
            "env",
            f"CARGO_TARGET_DIR={self.target_dir}",
            "cargo",
            SBF_SUBCOMMAND,
            "--json",
            "--no-rustup",
            "--platform-tools-root",
            str(PLATFORM_TOOLS_ROOT),
            "--manifest-path",
            str(self.manifest_path),
        ]
        if self.tools_version is not None:
            args += ["--tools-version", self.tools_version]
        if self.features:
            args += ["--features", " ".join(self.features)]
        return args


def platform_tools_installed(version: str, *, root: Path = PLATFORM_TOOLS_ROOT) -> bool:
    return (root / version).is_dir()


def platform_tools_cargos(version: str, *, root: Path = PLATFORM_TOOLS_ROOT) -> tuple[Path, ...]:
    """``cargo`` binaries shipped with a platform-tools version.

    Not the cargo on ``PATH``. A git source's cache directory name depends on
    the cargo version, so a fetch with the host cargo misses the cache the
    platform-tools cargo reads. An offline build then reports ``can't checkout
    ... you are in the offline mode``.

    One version may contain both ``platform-tools`` and ``platform-tools-certora``.
    The tool picks which binary runs, so both are returned. They share the
    registry cache.
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

    A fetch failure is logged. The later build names the crate it could not find.
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
    session: CargoSession, build: SbfBuild, *, timeout_s: int = BUILD_TIMEOUT_S
) -> SbfRun:
    """Run ``cargo certora-sbf`` in ``session``'s workdir, confined.

    A missing toolchain raises :class:`PlatformToolsMissing`. An operator
    installs it. A failed compile returns rustc's diagnostics.
    """
    tools_version = build.tools_version
    if tools_version is not None and session.confined:
        if not platform_tools_installed(tools_version):
            raise PlatformToolsMissing(tools_version, PLATFORM_TOOLS_ROOT)
        await _warm_for_the_build_cargo(session, tools_version, build.manifest_path)
    program, *args = build.argv()
    started = time.perf_counter()
    built = await session.run_confined(
        program, args, timeout_s=timeout_s, extra_ro=(PLATFORM_TOOLS_ROOT,)
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
        # The compiler succeeded and the JSON did not. Report the manifest error, not stderr.
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
    build: SbfBuild,
    *,
    name: str,
    timeout_s: int = BUILD_TIMEOUT_S,
) -> Path:
    """Write the ``build_script`` the conf points at, and return its path.

    ``name`` separates scripts when several submissions share one workdir.
    Confinement is the ``argv_prefix`` from :meth:`CargoSession.backend_spec`
    (``docs/command-sandbox.md`` §4). If the provider cannot confine, that
    call raises before anything is written.

    The command file names the workdir, the target directory, and the
    confinement grants by absolute path. The script runs only in the tree it
    was written for.
    """
    spec = await session.backend_spec(timeout_s=timeout_s, extra_ro=(PLATFORM_TOOLS_ROOT,))
    build_dir = session.workdir / BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    script = build_dir / f"{name}.py"
    build_command_path(script).write_text(
        json.dumps(
            {
                "cwd": str(session.workdir.resolve()),
                "argv_prefix": spec["argv_prefix"],
                "argv": build.argv(),
            },
            indent=2,
        )
    )
    script.write_text((files("composer.cargo") / "sbf_build_script.py").read_text())
    # certoraRun execs the script directly, so the shebang needs the execute bit.
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script
