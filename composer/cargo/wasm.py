"""Soroban's verification build: cargo to wasm, and the manifest the prover reads.

:mod:`composer.cargo.sbf`'s peer, with the same contract: ``certoraParseBuildScript.run_rust_build``
reads ``success`` / ``project_directory`` / ``sources`` / ``executables`` from the build script's
stdout. ``cargo certora-sbf`` prints that manifest itself; a plain wasm build prints nothing, so the
backend composes it from the workspace it already read, and the generated build script re-emits it
after rerunning the same build — a ``certora_build.py`` without the hand-maintained constants.

Nothing Solana-specific carries over: no platform-tools (the wasm target is an ordinary rustup
component), no tools version, and no inlining/summaries files — ``SorobanProverAttributes``
declares neither key.
"""

import dataclasses
import json
import stat
import time
from importlib.resources import files
from pathlib import Path

from composer.cargo.sbf import Built, BuildManifest, build_command_path, resolved_build_timeout_s
from composer.cargo.session import CargoSession, CompileFailed

#: What a Soroban contract is built for. ``wasm32v1-none`` rather than ``wasm32-unknown-unknown``,
#: and the reason is CVLR's own: it declares ``wasm_import_module = "env"`` on the ``CVT_*``
#: intrinsics under ``cfg(all(target_family = "wasm", target_os = "none"))``, and only this target
#: satisfies that — ``wasm32-unknown-unknown``'s ``target_os`` is ``unknown``. Built both ways on one
#: project: every ``CVT_*`` resolves here and none of them does there. It is also the more
#: conservative artifact, emitting Wasm 1.0 rather than the post-MVP features a newer rustc enables
#: for the older target, which is the safer thing to hand an analyzer.
WASM_TARGET = "wasm32v1-none"

#: What the link still needs after the target change, and it is one symbol: ``cvlr-soroban``'s
#: ``CERTORA_SOROBAN_is_auth`` carries no ``wasm_import_module`` on the SDK-22 and SDK-25 branches
#: (``main`` declares it unconditionally). Without the flag the link fails on that alone. The cost
#: is the old one: a genuinely missing symbol becomes an import the prover rejects rather than a
#: link error — so drop this as soon as those branches carry the attribute.
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
class WasmBuild:
    """One wasm build of a Soroban contract. The gate runs it, and the build script reruns it."""

    manifest_path: Path
    identity: WasmIdentity
    features: tuple[str, ...] = ()

    def cargo_argv(self) -> list[str]:
        """The command line up to ``--``: everything the prover's ``--cargo_features`` extends.

        ``cargo rustc --lib`` rather than ``cargo build`` so :data:`LINK_ARGS` reach only the
        contract's own link step, leaving dependencies and the project's ``RUSTFLAGS`` alone.
        """
        args = ["cargo", "rustc", "--lib", "--target", WASM_TARGET, "--release"]
        args += ["--manifest-path", str(self.manifest_path)]
        if self.features:
            args += ["--features", " ".join(self.features)]
        return args

    def argv(self) -> list[str]:
        """The full command line, program included."""
        return [*self.cargo_argv(), "--", *LINK_ARGS]


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


def wasm_artifact(target_directory: Path, artifact_stem: str) -> Path:
    """Where cargo puts the wasm for :meth:`WasmBuild.argv`'s flags."""
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
        success=True,
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


#: How rustc reports a target whose standard library is not installed. Two spellings, because
#: cargo's own message and rustc's differ and which one surfaces depends on where the build stops.
_MISSING_TARGET = ("may not be installed", "can't find crate for `core`")


def _with_target_hint(diagnostics: str) -> str:
    """The compiler's message, plus the one-line fix when the target itself is missing.

    Worth special-casing because of *when* it would otherwise be read: the host ``cargo check`` in
    preflight passes without the wasm target, so an uninstalled target first appears at a unit's
    first submission — minutes in, after an agent has authored a harness — as a compile failure the
    author is told to fix, and it has nothing to do with the harness.
    """
    if not any(phrase in diagnostics for phrase in _MISSING_TARGET):
        return diagnostics
    return (
        f"{diagnostics}\n\n"
        f"note: the {WASM_TARGET} standard library is not installed for this toolchain. This is a "
        f"host setup problem, not a problem with the harness: run `rustup target add {WASM_TARGET}`."
    )


async def wasm_build(session: CargoSession, build: WasmBuild) -> WasmRun:
    """Run the slow tier in ``session``'s workdir, confined."""
    program, *args = build.argv()
    started = time.perf_counter()
    built = await session.run_confined(program, args, timeout_s=resolved_build_timeout_s())
    elapsed = int((time.perf_counter() - started) * 1000)
    if built.exit_code != 0:
        return WasmRun(
            elapsed,
            CompileFailed(
                diagnostics=_with_target_hint(built.stderr.strip()),
                exit_code=built.exit_code,
            ),
            session.confined,
        )
    manifest = wasm_manifest(build.identity)
    if not manifest.artifact.is_file():
        raise WasmArtifactMissing(manifest.artifact)
    return WasmRun(elapsed, Built(manifest), session.confined)


async def write_build_script(
    session: CargoSession, build: WasmBuild, *, into: Path, name: str
) -> Path:
    """Write the ``build_script`` the conf points at into ``into``, and return its path.

    Same mechanics and the same trust rule as :func:`composer.cargo.sbf.write_build_script` — the
    prover runs the script unconfined, so ``into`` must be a directory no build in ``session`` can
    write — plus the composed manifest, which the script prints because a wasm build prints none.
    """
    if session.confined and session.build_can_write(into):
        raise ValueError(f"{into} is writable by the confined build, so it cannot hold the build script")
    spec = await session.backend_spec(timeout_s=resolved_build_timeout_s())
    manifest = wasm_manifest(build.identity)
    script = into.resolve() / f"{name}.py"
    build_command_path(script).write_text(
        json.dumps(
            {
                "cwd": str(session.workdir.resolve()),
                "argv_prefix": spec["argv_prefix"],
                # Split at `--` so the script can add the prover's features on the cargo side.
                "argv": build.cargo_argv(),
                "rustc_args": list(LINK_ARGS),
                "env": session.toolchain_env,
                "timeout_s": spec["timeout_s"],
                "manifest": {
                    "project_directory": str(manifest.project_directory),
                    "executables": manifest.executables,
                    "sources": list(manifest.sources),
                },
            },
            indent=2,
        )
    )
    script.write_text((files("composer.cargo") / "wasm_build_script.py").read_text())
    # certoraRun execs the script directly, so the shebang needs the execute bit.
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script
