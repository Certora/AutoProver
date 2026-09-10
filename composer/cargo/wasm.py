"""Soroban's verification build: cargo to wasm, and the manifest the prover reads.

:mod:`composer.cargo.sbf`'s peer, with the same contract: ``certoraParseBuildScript.run_rust_build``
reads ``success`` / ``project_directory`` / ``sources`` / ``executables`` from the build script's
stdout. ``cargo certora-sbf`` prints that manifest itself; a plain wasm build prints nothing, so the
backend composes it from the workspace it already read, and the generated build script re-emits it
after rerunning the same build — a ``certora_build.py`` without the hand-maintained constants.

Nothing Solana-specific carries over: no platform-tools (the wasm target is an ordinary rustup
component), no tools version or arch, and no inlining/summaries files — ``SorobanProverAttributes``
declares neither key.
"""

import dataclasses
import json
import stat
import time
from pathlib import Path

from composer.cargo.sbf import (
    BUILD_COMMAND_NAME,
    BUILD_DIR,
    BUILD_SCRIPT_NAME,
    BUILD_TIMEOUT_S,
    Built,
    BuildManifest,
)
from composer.cargo.session import CargoSession, CompileFailed

#: soroban-sdk 22's target.
WASM_TARGET = "wasm32-unknown-unknown"

#: The 0.4 CVLR line declares the ``CVT_*`` intrinsics as bare ``extern "C"``, relying on wasm-ld's
#: ``--allow-undefined`` to turn them into the ``env`` imports the prover reads. Current rustc no
#: longer passes it for this target (the link fails on ``CVT_satisfy``), so it is passed here. The
#: cost: a genuinely missing symbol becomes an import the prover rejects, not a link error. Drop
#: this once the pinned ``cvlr`` declares ``wasm_import_module = "env"`` (unreleased main does).
LINK_ARGS = ("-C", "link-arg=--allow-undefined")


@dataclasses.dataclass(frozen=True)
class WasmIdentity:
    """Where one wasm build's inputs and output are. Carried from preflight, which already resolved
    them against the same workspace, rather than re-derived."""

    workspace_root: Path
    #: Relative to ``workspace_root``.
    package_dir: Path
    target_directory: Path
    artifact_stem: str


@dataclasses.dataclass(frozen=True)
class WasmRun:
    """One slow-tier wasm build. Same shape as :class:`composer.cargo.sbf.SbfRun`, so submission
    code handles one outcome vocabulary."""

    duration_ms: int
    verdict: Built | CompileFailed
    confined: bool

    @property
    def ok(self) -> bool:
        return isinstance(self.verdict, Built)


def _cargo_args(manifest_path: Path, features: tuple[str, ...]) -> list[str]:
    args = ["rustc", "--lib", "--target", WASM_TARGET, "--release"]
    args += ["--manifest-path", str(manifest_path)]
    if features:
        args += ["--features", " ".join(features)]
    return args


def wasm_argv(*, manifest_path: Path, features: tuple[str, ...] = ()) -> list[str]:
    """The ``cargo`` argument vector, shared by the direct build and the build script.

    ``cargo rustc --lib`` rather than ``cargo build`` so :data:`LINK_ARGS` reach only the contract's
    own link step, leaving dependencies and the project's ``RUSTFLAGS`` alone.
    """
    return [*_cargo_args(manifest_path, features), "--", *LINK_ARGS]


def wasm_artifact(target_directory: Path, artifact_stem: str) -> Path:
    """Where cargo puts the wasm for :func:`wasm_argv`'s flags."""
    return target_directory / WASM_TARGET / "release" / f"{artifact_stem}.wasm"


def wasm_manifest(identity: WasmIdentity) -> BuildManifest:
    """The build manifest a successful wasm build stands behind.

    ``sources`` are globs the prover resolves against ``project_directory``; they are what
    ``.certora_sources`` carries for the report and the counterexample analyzer.
    """
    root = identity.workspace_root
    rel_pkg = identity.package_dir.as_posix()
    sources = ["Cargo.toml"]
    if (root / "Cargo.lock").is_file():
        sources.append("Cargo.lock")
    if rel_pkg not in ("", "."):
        sources += [f"{rel_pkg}/Cargo.toml", f"{rel_pkg}/**/*.rs"]
    else:
        sources.append("**/*.rs")
    artifact = wasm_artifact(identity.target_directory, identity.artifact_stem)
    return BuildManifest(
        project_directory=root,
        executables=str(artifact.relative_to(root)),
        sources=tuple(sources),
    )


class WasmArtifactMissing(RuntimeError):
    """The build exited 0 but the expected ``.wasm`` is not on disk — a scaffold or package
    selection problem (not a ``cdylib``, or the wrong stem), not a compile error."""

    def __init__(self, expected: Path):
        self.expected = expected
        super().__init__(
            f"the wasm build succeeded but produced no {expected}. The package's library target "
            f"must be a cdylib and the artifact stem must match its name."
        )


async def wasm_build(
    session: CargoSession,
    identity: WasmIdentity,
    *,
    manifest_path: Path,
    features: tuple[str, ...] = (),
    timeout_s: int = BUILD_TIMEOUT_S,
) -> WasmRun:
    """Run the slow tier in ``session``'s workdir, confined."""
    argv = wasm_argv(manifest_path=manifest_path, features=features)
    started = time.perf_counter()
    built = await session.run_confined("cargo", argv, timeout_s=timeout_s)
    elapsed = int((time.perf_counter() - started) * 1000)
    if built.exit_code != 0:
        return WasmRun(
            elapsed,
            CompileFailed(diagnostics=built.stderr.strip(), exit_code=built.exit_code),
            session.confined,
        )
    manifest = wasm_manifest(identity)
    if not manifest.artifact.is_file():
        raise WasmArtifactMissing(manifest.artifact)
    return WasmRun(elapsed, Built(manifest), session.confined)


_SCRIPT_TEMPLATE = '''\
#!/usr/bin/env python3
"""Generated by composer.cargo.wasm — do not edit.

certoraSorobanProver runs this as its `build_script` and reads the build manifest from stdout. The
command, its confinement wrapper and the manifest were fixed when AutoProver ran the same build as
its pre-submission gate, so this rerun is a warm cargo no-op.
"""

import json
import pathlib
import subprocess
import sys

command = json.loads(pathlib.Path(__file__).with_name({command_name!r}).read_text())
argv = [*command["argv_prefix"], *command["argv"]]

# --cargo_features is how the prover adds features; --json and -l need no handling.
extra = sys.argv[sys.argv.index("--cargo_features") + 1:] if "--cargo_features" in sys.argv else []
if extra:
    argv += ["--features", " ".join(extra)]
# After the features: everything past `--` goes to rustc, not cargo.
argv += ["--", *command["rustc_args"]]

result = subprocess.run(argv, capture_output=True, text=True, cwd=command["cwd"])
sys.stderr.write(result.stderr)

manifest = dict(command["manifest"])
manifest["success"] = result.returncode == 0
print(json.dumps(manifest, indent=2))
sys.exit(result.returncode)
'''


async def write_build_script(
    session: CargoSession,
    identity: WasmIdentity,
    *,
    manifest_path: Path,
    features: tuple[str, ...] = (),
    timeout_s: int = BUILD_TIMEOUT_S,
) -> Path:
    """Write the ``build_script`` the conf points at, and return its path.

    Same mechanics as :func:`composer.cargo.sbf.write_build_script`, plus the composed manifest.
    """
    spec = await session.sandbox.backend_spec(session.workdir, timeout_s=timeout_s)
    manifest = wasm_manifest(identity)
    build_dir = session.workdir / BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / BUILD_COMMAND_NAME).write_text(
        json.dumps(
            {
                "cwd": str(session.workdir),
                "argv_prefix": spec["argv_prefix"],
                # Split at `--` so the script can add the prover's features on the cargo side.
                "argv": ["cargo", *_cargo_args(manifest_path, features)],
                "rustc_args": list(LINK_ARGS),
                "manifest": {
                    "project_directory": str(manifest.project_directory),
                    "executables": manifest.executables,
                    "sources": list(manifest.sources),
                },
            },
            indent=2,
        )
    )
    script = build_dir / BUILD_SCRIPT_NAME
    script.write_text(_SCRIPT_TEMPLATE.format(command_name=BUILD_COMMAND_NAME))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script
