"""Assemble a Rust wheel into a runnable AutoProver application.

The declarative descriptor lets a single host synthesize what a hand-written
application spells out (phase enum, core-phase mapping, artifact store, labels,
section order) and hand the driver a ready :class:`PipelineBackend`.

* :func:`run_rust_pipeline` — the pipeline wrapper (build backend + ``PipelineRun``
  + call the shared driver). This is the piece a generic entry point calls.
* :func:`build_application` — bundle everything a frontend / ``main()`` needs
  (the synthesized phase enum, labels, section order, and a backend factory).

The imperative shell around this — the async entry point (Postgres pools, RAG,
``composer.bind``) and the Textual/console frontend — stays Python and reuses the
existing autoprove/foundry conventions (see ``docs/rust-applications.md`` §4.2,
§4.6). Those are intentionally *not* synthesized here; this module provides the
declarative inputs they consume.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
from dataclasses import dataclass
from typing import Any, Callable, cast

from composer.io.multi_job import HandlerFactory
from composer.pipeline.core import (
    CorePhases,
    CorePipelineResult,
    PipelineRun,
    run_pipeline,
)
from composer.pipeline.ecosystem import ECOSYSTEMS, Ecosystem
from composer.rustapp.adapter import FeedbackHook, ProverHook, RustBackend
from composer.rustapp.descriptor import AppDescriptor, CoreSlot
from composer.rustapp.result import RustFormalResult
from composer.rustapp.store import RustArtifactStore
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ServiceHost


def load_module(module_name: str) -> Any:
    """Import a Rust application's compiled module by name (e.g. ``"echoprover"``)."""
    return importlib.import_module(module_name)


def load_descriptor(module: Any) -> AppDescriptor:
    """Parse a module's ``descriptor()`` JSON into an :class:`AppDescriptor`."""
    return AppDescriptor.model_validate_json(module.descriptor())


def resolve_ecosystem(descriptor: AppDescriptor) -> Ecosystem[Any, Any, Any]:
    """Resolve the descriptor's declared ecosystem against the registry. Raises a clear
    error if the chain isn't registered yet (e.g. Solana/Soroban land in later phases)."""
    eco = ECOSYSTEMS.get(descriptor.ecosystem)
    if eco is None:
        raise ValueError(
            f"application {descriptor.name!r} selects ecosystem {descriptor.ecosystem!r}, "
            f"which is not registered. Available: {sorted(ECOSYSTEMS)}."
        )
    return eco


def build_phase_enum(descriptor: AppDescriptor) -> type[enum.Enum]:
    """Synthesize the pipeline's phase enum from the descriptor. Safe: the code
    only ever uses phase members for ``.name`` and as dict keys (no isinstance /
    identity checks against a static class)."""
    ordered = descriptor.ordered_phases()
    name = "".join(part.capitalize() for part in descriptor.name.split("_")) + "Phase"
    # enum.Enum's functional API is typed as returning an ``Enum`` instance, not the new
    # class; it does return a class at runtime.
    return cast(type[enum.Enum], enum.Enum(name, {p.key: p.key for p in ordered}))


def build_core_phases(
    descriptor: AppDescriptor, phase: type[enum.Enum]
) -> CorePhases:
    """Map the descriptor's core-slot declarations onto the synthesized enum. Every
    slot must be filled — the driver tags all four."""
    slot_to_key = descriptor.core_slot_map()
    missing = [s.value for s in CoreSlot if s not in slot_to_key]
    if missing:
        raise ValueError(
            f"descriptor {descriptor.name!r} is missing core phase(s): {missing}. "
            "Every application must map analysis/extraction/formalization/report."
        )
    return CorePhases(
        analysis=phase[slot_to_key[CoreSlot.ANALYSIS]],
        extraction=phase[slot_to_key[CoreSlot.EXTRACTION]],
        formalization=phase[slot_to_key[CoreSlot.FORMALIZATION]],
        report=phase[slot_to_key[CoreSlot.REPORT]],
    )


def build_backend(
    module: Any,
    descriptor: AppDescriptor,
    project_root: str,
    *,
    prover: ProverHook | None = None,
    feedback: FeedbackHook | None = None,
) -> RustBackend:
    """Construct the :class:`RustBackend` (synthesizes the phase enum + core phases
    + artifact store, and resolves the descriptor's ecosystem)."""
    phase = build_phase_enum(descriptor)
    core = build_core_phases(descriptor, phase)
    store = RustArtifactStore(project_root, descriptor.artifact_layout)
    return RustBackend(
        module=module,
        descriptor=descriptor,
        _phase=phase,
        _core_phases=core,
        artifact_store=store,
        ecosystem=resolve_ecosystem(descriptor),
        prover=prover,
        feedback=feedback,
    )


async def run_rust_pipeline(
    module_name: str,
    source_input: SourceCode,
    ctx: WorkflowContext[None],
    handler_factory: HandlerFactory,
    env: ServiceHost,
    *,
    max_concurrent: int = 4,
    max_bug_rounds: int = 3,
    interactive: bool = False,
    prover: ProverHook | None = None,
    feedback: FeedbackHook | None = None,
) -> CorePipelineResult[RustFormalResult, Any]:
    """Build the backend from ``module_name`` and run the shared driver — the Rust
    analogue of ``run_autoprove_pipeline`` / ``run_foundry_pipeline``.

    This synthesizes a *fresh* phase enum internal to the backend. It is the right
    entry for headless callers whose handler ignores phases; for a TUI/console
    frontend, build a :class:`RustApplication` once and use :func:`run_application`
    so the frontend's labels and the backend's phases share one enum object."""
    module = load_module(module_name)
    descriptor = load_descriptor(module)
    ecosystem = resolve_ecosystem(descriptor)
    backend = build_backend(
        module, descriptor, source_input.project_root, prover=prover, feedback=feedback
    )
    run = PipelineRun(
        ctx, env, source_input, handler_factory, asyncio.Semaphore(max_concurrent)
    )
    return await run_pipeline(
        backend, run, ecosystem, interactive=interactive, threat_model=None, max_bug_rounds=max_bug_rounds
    )


async def run_application(
    app: "RustApplication",
    source_input: SourceCode,
    ctx: WorkflowContext[None],
    handler_factory: HandlerFactory,
    env: ServiceHost,
    *,
    max_concurrent: int = 4,
    max_bug_rounds: int = 3,
    interactive: bool = False,
) -> CorePipelineResult[RustFormalResult, Any]:
    """Run a pre-built :class:`RustApplication`. The backend is constructed from the
    app's already-synthesized phase enum, so the ``TaskInfo`` phases the driver emits
    are the *same* enum members the frontend's ``phase_labels`` are keyed by — the
    identity the frontend's label lookup relies on."""
    backend = app.make_backend(source_input.project_root)
    run = PipelineRun(
        ctx, env, source_input, handler_factory, asyncio.Semaphore(max_concurrent)
    )
    return await run_pipeline(
        backend, run, app.ecosystem, interactive=interactive, threat_model=None, max_bug_rounds=max_bug_rounds
    )


@dataclass
class RustApplication:
    """Everything a frontend / ``main()`` needs, synthesized from the descriptor.

    ``phase_labels`` is keyed by the synthesized enum members (member identity
    drives the frontend's label lookup), and ``section_order`` lists every phase
    label in declared order — the two inputs a ``MultiJobApp`` frontend consumes."""

    descriptor: AppDescriptor
    module: Any
    ecosystem: Ecosystem[Any, Any, Any]
    phase: type[enum.Enum]
    core_phases: CorePhases
    phase_labels: dict[Any, str]
    section_order: list[str]
    make_backend: Callable[[str], RustBackend]

    @property
    def name(self) -> str:
        return self.descriptor.name

    @property
    def header_text(self) -> str:
        return self.descriptor.header_text

    def validate_preconditions(self, args: dict) -> str | None:
        """Delegate to the Rust precondition hook; return an error string or None."""
        import json

        return self.module.validate_preconditions(json.dumps(args))


def build_application(
    module_name: str,
    *,
    prover: ProverHook | None = None,
    feedback: FeedbackHook | None = None,
) -> RustApplication:
    """Load a Rust wheel and synthesize a :class:`RustApplication`."""
    module = load_module(module_name)
    descriptor = load_descriptor(module)
    ecosystem = resolve_ecosystem(descriptor)
    phase = build_phase_enum(descriptor)
    core = build_core_phases(descriptor, phase)
    ordered = descriptor.ordered_phases()
    phase_labels = {phase[p.key]: p.label for p in ordered}
    section_order = [p.label for p in ordered]

    def make_backend(project_root: str) -> RustBackend:
        store = RustArtifactStore(project_root, descriptor.artifact_layout)
        return RustBackend(
            module=module,
            descriptor=descriptor,
            _phase=phase,
            _core_phases=core,
            artifact_store=store,
            ecosystem=ecosystem,
            prover=prover,
            feedback=feedback,
        )

    return RustApplication(
        descriptor=descriptor,
        module=module,
        ecosystem=ecosystem,
        phase=phase,
        core_phases=core,
        phase_labels=phase_labels,
        section_order=section_order,
        make_backend=make_backend,
    )
