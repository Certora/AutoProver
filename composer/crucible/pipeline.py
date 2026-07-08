"""Assemble + run the Crucible application's pipeline.

The generic rust-app host builds a single-file :class:`RustArtifactStore`; Crucible
needs the crate-assembling :class:`CrucibleArtifactStore` (docs §7.1) plus its
host-resolved dependency (`CrucibleDep`, §6.1) and build/fuzz timeouts. So the
Crucible application supplies this thin pipeline wrapper — the small bespoke Python
the doc's §7.1 carve-out allows — while everything else (the driver, the SOLANA
ecosystem front half, the setup/per-component IoC loops) is reused unchanged.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from composer.crucible.harness import CrucibleDep
from composer.crucible.store import CrucibleArtifactStore
from composer.io.multi_job import HandlerFactory
from composer.pipeline.core import CorePipelineResult, PipelineRun, run_pipeline
from composer.rustapp.adapter import RustBackend
from composer.rustapp.host import (
    build_core_phases,
    build_phase_enum,
    load_descriptor,
    load_module,
    resolve_ecosystem,
)
from composer.rustapp.result import RustFormalResult
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ServiceHost

CRUCIBLE_MODULE = "crucible_app"


def resolve_crucible_repo(explicit: str | None = None) -> Path:
    """Locate the crucible checkout (source of the harness crate deps, §6.1): an
    explicit path, else ``$CRUCIBLE_REPO``, else ``~/src/crucible``. Errors if the
    crates aren't there (the harness would fail to build)."""
    candidate = Path(explicit or os.environ.get("CRUCIBLE_REPO", str(Path.home() / "src" / "crucible")))
    if not (candidate / "crates" / "crucible-fuzzer").is_dir():
        raise FileNotFoundError(
            f"crucible checkout not found at {candidate} (no crates/crucible-fuzzer). "
            "Set --crucible-repo / $CRUCIBLE_REPO to a local crucible clone."
        )
    return candidate


def build_crucible_backend(
    source_input: SourceCode,
    *,
    crucible_repo: Path,
    fuzz_timeout_s: int,
    command_timeout_s: int,
    module_name: str = CRUCIBLE_MODULE,
) -> RustBackend:
    """Build the Crucible :class:`RustBackend` with the crate store + resolved deps."""
    module = load_module(module_name)
    descriptor = load_descriptor(module)
    ecosystem = resolve_ecosystem(descriptor)
    program = str(source_input.contract_name)
    dep = CrucibleDep(
        crucible_repo=crucible_repo,
        program_crate=program,
        program_rel=f"../../programs/{program}",
    )
    store = CrucibleArtifactStore(source_input.project_root, program=program, dep=dep)
    phase = build_phase_enum(descriptor)
    return RustBackend(
        module=module,
        descriptor=descriptor,
        _phase=phase,
        _core_phases=build_core_phases(descriptor, phase),
        artifact_store=store,
        ecosystem=ecosystem,
        command_timeout_s=command_timeout_s,
        fuzz_timeout_s=fuzz_timeout_s,
    )


async def run_crucible_pipeline(
    source_input: SourceCode,
    ctx: WorkflowContext[None],
    handler_factory: HandlerFactory,
    env: ServiceHost,
    *,
    crucible_repo: str | Path | None = None,
    fuzz_timeout_s: int = 30,
    command_timeout_s: int = 1800,
    max_concurrent: int = 4,
    max_bug_rounds: int = 3,
    interactive: bool = False,
) -> CorePipelineResult[RustFormalResult, Any]:
    """Run the whole Crucible vertical: the SOLANA ecosystem front half (analysis +
    property extraction) → the Crucible backend (shared fixture via the setup
    session, then per-component test authoring + fuzzing) → report."""
    # Build the program to sBPF up front (docs §5.1) — the harness loads the `.so`,
    # and it's the shared Solana build step, not per-component work.
    from composer.spec.solana.build import build_program

    await build_program(source_input.project_root, str(source_input.contract_name), timeout_s=command_timeout_s)

    backend = build_crucible_backend(
        source_input,
        crucible_repo=resolve_crucible_repo(str(crucible_repo) if crucible_repo else None),
        fuzz_timeout_s=fuzz_timeout_s,
        command_timeout_s=command_timeout_s,
    )
    run = PipelineRun(ctx, env, source_input, handler_factory, asyncio.Semaphore(max_concurrent))
    return await run_pipeline(
        backend, run, backend.ecosystem,
        interactive=interactive, threat_model=None, max_bug_rounds=max_bug_rounds,
    )
