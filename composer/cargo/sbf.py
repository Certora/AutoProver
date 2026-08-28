"""Solana's verification build: ``cargo certora-sbf``, and the JSON it speaks.

This is the **slow tier** of ``docs/cvlr-backend-plan.md`` §5.1 — the real chain build, run before a
prover submission rather than per write — and it is also the one place where the backend and
``certoraSolanaProver`` have to agree on what was built.

They agree through a JSON document that neither of them invents. ``cargo certora-sbf --json`` prints
it (to stdout, with the compile log on stderr), and ``certoraSolanaProver``'s ``build_script``
attribute is defined as "run this and read that JSON from its stdout"
(``CertoraProver/certoraParseBuildScript.py``): ``project_directory``, ``executables``, ``sources``,
and — read out of the package's own ``[package.metadata.certora]`` — ``solana_inlining`` /
``solana_summaries``. So the backend does not construct a manifest, does not translate one, and does
not record one to replay later. It runs the build, reads the JSON, and hands the prover a
:func:`build_script` that reruns *the same command*, confined the same way.

Why rerun rather than replay, given the build is already done: the second run is a warm cargo no-op
(sub-second), and what it buys is that the manifest the prover acts on always describes a build that
exists on disk right now. A recorded one goes stale the moment anything moves, and its failure mode
is the prover uploading the wrong sources or a missing artifact — which surfaces as a verification
result, not as a build error.

Why a build script at all, rather than handing over the finished ``.so`` through the conf's ``files``
key: ``set_rust_build_directory`` only collects the project's Rust sources into ``.certora_sources``
on the build-script path (with ``files`` it copies the artifact and nothing else), and those sources
are what the report renders and what a counterexample analyzer reads (§5.3). The other half of the
same answer is confinement: ``certoraSolanaProver``'s own from-sources path runs ``cargo certora-sbf``
directly inside its process, unconfined, which §3 item 2 does not allow. Owning the build script is
what lets the build stay inside the sandbox while the prover still sees a from-sources run.
"""

import dataclasses
import json
import logging
import os
import stat
import time
from pathlib import Path

from composer.cargo.session import CargoSession, CompileFailed

_log = logging.getLogger(__name__)

#: The cargo subcommand that builds a Solana program for verification. Certora's own, not
#: ``cargo-build-sbf``: it pins the platform-tools version and emits the build manifest below.
SBF_SUBCOMMAND = "certora-sbf"

#: Where ``cargo certora-sbf`` keeps its platform toolchains. Mirrors the tool's own default and the
#: read-only grant in :func:`composer.sandbox.recipes.rust_build_policy`; overridable by the same
#: environment variable the tool reads.
PLATFORM_TOOLS_ROOT = Path(
    os.environ.get("CERTORA_PLATFORM_TOOLS_ROOT", Path.home() / ".cache" / "solana")
)

#: A full sbf build of a real program, from a warm cache.
BUILD_TIMEOUT_S = 1800

#: Where the generated build script and its command file live inside the workdir. Under the workdir
#: because that is the one path the confinement policy grants read-write, and beside each other
#: because the script reads the command from its own directory.
BUILD_DIR = Path(".certora_build")
BUILD_SCRIPT_NAME = "confined_build.py"
BUILD_COMMAND_NAME = "confined_build.json"


class PlatformToolsMissing(RuntimeError):
    """The requested platform-tools version is not installed.

    Its own exception because the failure is otherwise mute: ``cargo certora-sbf`` installs missing
    tools by downloading them, which a confined build cannot do (no network, and the cache is granted
    read-only), so the build fails somewhere inside the toolchain with a message that names neither
    the version nor the fact that installing it is an operator action. Naming it here is the same
    treatment a missing solc gets, for the same reason."""

    def __init__(self, version: str, root: Path):
        self.version = version
        self.root = root
        super().__init__(
            f"Solana platform tools {version} are not installed under {root}. A confined build "
            f"cannot fetch them (no network, and the cache is read-only). Install them once, "
            f"unconfined, with `cargo certora-sbf --tools-version {version}` in any Solana crate, "
            f"or point CERTORA_PLATFORM_TOOLS_ROOT at a root that has them."
        )


@dataclasses.dataclass(frozen=True)
class BuildManifest:
    """What ``cargo certora-sbf --json`` reported, validated against what the prover requires.

    Paths are as cargo printed them: ``project_directory`` absolute, everything else relative to it.
    Left that way on purpose — ``certoraParseBuildScript`` re-resolves them against
    ``project_directory`` itself, so normalizing here would only create a second convention.
    """

    project_directory: Path
    executables: str
    sources: tuple[str, ...]
    solana_inlining: tuple[str, ...] = ()
    solana_summaries: tuple[str, ...] = ()

    @property
    def artifact(self) -> Path:
        """The built ``.so``."""
        return self.project_directory / self.executables


class MalformedBuildManifest(ValueError):
    """``cargo certora-sbf --json`` printed something the prover would reject."""


def parse_manifest(stdout: str) -> BuildManifest:
    """Parse the build JSON, requiring exactly what ``certoraParseBuildScript`` requires.

    Checked here rather than left to the prover because the two failures read completely differently:
    a missing key found here is a build-tool problem reported next to the build, while the same key
    missing at submission time is a ``CertoraUserInputError`` inside a prover run that has already
    started."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MalformedBuildManifest(f"build did not print JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MalformedBuildManifest(f"build printed {type(payload).__name__}, not an object")
    missing = [k for k in ("success", "project_directory", "sources", "executables") if k not in payload]
    if missing:
        raise MalformedBuildManifest(f"build JSON is missing {', '.join(missing)}")
    if not payload["success"]:
        raise MalformedBuildManifest("build JSON reports success: false")
    return BuildManifest(
        project_directory=Path(payload["project_directory"]),
        executables=payload["executables"],
        sources=tuple(payload["sources"]),
        solana_inlining=tuple(payload.get("solana_inlining") or ()),
        solana_summaries=tuple(payload.get("solana_summaries") or ()),
    )


@dataclasses.dataclass(frozen=True)
class Built:
    """The chain build succeeded, and this is what it produced."""

    manifest: BuildManifest


type SbfVerdict = Built | CompileFailed


@dataclasses.dataclass(frozen=True)
class SbfRun:
    """One slow-tier build: what it decided, what it cost, and whether it was confined."""

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
    arch: str | None = None,
) -> list[str]:
    """The ``cargo certora-sbf`` argument vector, shared by the direct build and the build script.

    One function so the two cannot drift: the prover's rerun has to be the same command as the
    gate's, or the artifact it reports is not the artifact the gate approved.

    ``--no-rustup`` is not optional. The tool registers a ``certora-solana`` rustup toolchain around
    each build and removes it afterwards, which writes to ``RUSTUP_HOME`` — read-only under
    confinement. The flag skips that entirely and the build resolves the platform toolchain by path.
    """
    args = [
        SBF_SUBCOMMAND,
        "--json",
        "--no-rustup",
        "--manifest-path",
        str(manifest_path),
    ]
    if tools_version is not None:
        args += ["--tools-version", tools_version]
    if arch is not None:
        args += ["--arch", arch]
    if features:
        args += ["--features", " ".join(features)]
    return args


def platform_tools_installed(version: str, *, root: Path = PLATFORM_TOOLS_ROOT) -> bool:
    return (root / version).is_dir()


async def sbf_build(
    session: CargoSession,
    *,
    manifest_path: Path,
    features: tuple[str, ...] = (),
    tools_version: str | None = None,
    arch: str | None = None,
    timeout_s: int = BUILD_TIMEOUT_S,
) -> SbfRun:
    """Run the slow tier in ``session``'s workdir, confined.

    Raises :class:`PlatformToolsMissing` rather than returning a failed run when the toolchain is
    absent: a compile failure is something an authoring agent can act on, and this is not — it is an
    operator action, and returning it through the same channel would put it in front of an agent that
    will try to fix the Rust.
    """
    if tools_version is not None and session.confined and not platform_tools_installed(tools_version):
        raise PlatformToolsMissing(tools_version, PLATFORM_TOOLS_ROOT)
    argv = sbf_argv(
        manifest_path=manifest_path, features=features, tools_version=tools_version, arch=arch
    )
    started = time.perf_counter()
    built = await session.run_confined("cargo", argv, timeout_s=timeout_s)
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
        # The compiler was happy and the tool was not, so the compile log is the wrong thing to
        # report; what went wrong is the manifest, and it is the tool's contract that broke.
        return SbfRun(
            elapsed,
            CompileFailed(diagnostics=str(exc), exit_code=built.exit_code),
            session.confined,
        )
    return SbfRun(elapsed, Built(manifest), session.confined)


_SCRIPT_TEMPLATE = '''\
#!/usr/bin/env python3
"""Generated by composer.cargo.sbf — do not edit.

certoraSolanaProver runs this as its `build_script` and reads the build manifest from stdout. The
build itself is AutoProver's: the command and its confinement wrapper were fixed when the backend
ran the same build as its pre-submission gate, so this rerun is a warm cargo no-op that reprints the
manifest for the run that is about to happen.
"""

import json
import pathlib
import subprocess
import sys

command = json.loads(pathlib.Path(__file__).with_name({command_name!r}).read_text())
argv = [*command["argv_prefix"], *command["argv"]]

# Certora passes --json and -l; the manifest is printed unconditionally, and the compile log goes to
# stderr either way, so the flags carry no decision here. --cargo_features is the exception: it is
# how the prover adds features to a build, and dropping it would silently build the wrong crate.
extra = sys.argv[sys.argv.index("--cargo_features") + 1:] if "--cargo_features" in sys.argv else []
if extra:
    argv += ["--features", " ".join(extra)]

result = subprocess.run(argv, capture_output=True, text=True, cwd=command["cwd"])
sys.stderr.write(result.stderr)
sys.stdout.write(result.stdout)
sys.exit(result.returncode)
'''


async def write_build_script(
    session: CargoSession,
    *,
    manifest_path: Path,
    features: tuple[str, ...] = (),
    tools_version: str | None = None,
    arch: str | None = None,
    timeout_s: int = BUILD_TIMEOUT_S,
) -> Path:
    """Write the ``build_script`` the conf points at, and return its path.

    The script carries no policy of its own. :meth:`SandboxConfig.backend_spec` resolves the
    confinement into an opaque ``argv_prefix`` — the same mechanism a Rust wheel is handed
    (``docs/command-sandbox.md`` §4) — so the script names no sandbox, and swapping the mechanism
    changes nothing here. It is fail-closed for the same reason that call is: a provider that cannot
    confine raises before anything runs.
    """
    spec = await session.sandbox.backend_spec(session.workdir, timeout_s=timeout_s)
    build_dir = session.workdir / BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / BUILD_COMMAND_NAME).write_text(
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
                        arch=arch,
                    ),
                ],
            },
            indent=2,
        )
    )
    script = build_dir / BUILD_SCRIPT_NAME
    script.write_text(_SCRIPT_TEMPLATE.format(command_name=BUILD_COMMAND_NAME))
    # certoraRun execs the script directly rather than through an interpreter, so the shebang needs
    # the execute bit to mean anything.
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script
