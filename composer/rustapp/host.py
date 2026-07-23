"""Assemble a Rust wheel into a runnable AutoProver application.

The declarative descriptor lets a single host synthesize what a hand-written
application spells out (phase enum, core-phase mapping, artifact store, labels,
section order) and hand the driver a ready :class:`PipelineBackend`.

* :func:`run_rust_pipeline` — the pipeline wrapper (build backend + ``PipelineRun``
  + call the shared driver). This is the piece a generic entry point calls.
* :func:`build_application` — bundle everything a frontend / ``main()`` needs
  (the synthesized phase enum, labels, section order, and a backend factory).

Applications that need a non-default store or backend class (e.g. Crucible's crate
store) pass ``store_factory`` / ``backend_cls``; the **same** phase enum is shared
by the frontend and the pipeline.
"""

import asyncio
import enum
import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from composer.io.multi_job import HandlerFactory
from composer.pipeline.core import (
    CorePhases,
    CorePipelineResult,
    PipelineRun,
    run_pipeline,
)
from composer.pipeline.ecosystem import ECOSYSTEMS, Ecosystem
from composer.rustapp.adapter import RustBackend
from composer.rustapp.descriptor import AppDescriptor, CoreSlot
from composer.rustapp.result import RustFormalResult
from composer.rustapp.store import RustArtifactStore
from composer.sandbox.command import DEFAULT_TIMEOUT_S
from composer.sandbox.config import SandboxConfig
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ServiceHost

#: Build an artifact store for a run from the source + descriptor.
StoreFactory = Callable[[SourceCode, AppDescriptor], ArtifactStore[Any, RustFormalResult]]


@dataclass
class BackendOptions:
    """Mutable run options closed over by :meth:`RustApplication.make_backend`.

    The CLI can adjust these (e.g. the sandbox) after building the application but
    before :func:`run_application`, keeping one phase enum. Backend-specific tuning knobs
    (e.g. a fuzz budget) travel as descriptor-declared args in :attr:`declared_args`.
    """

    command_timeout_s: int = DEFAULT_TIMEOUT_S
    sandbox: SandboxConfig | None = None
    #: Parsed values of the descriptor's declared CLI args, threaded into the backend and
    #: injected into every component's ``AuthorInput.context``. Set by the entry point.
    declared_args: dict[str, Any] = field(default_factory=dict)


def load_module(module_name: str) -> Any:
    """Import a Rust application's compiled module by name (e.g. ``"echoprover"``)."""
    return importlib.import_module(module_name)


def load_descriptor(module: Any) -> AppDescriptor:
    """Parse a module's ``descriptor()`` JSON into an :class:`AppDescriptor`."""
    return AppDescriptor.model_validate_json(module.descriptor())


def resolve_ecosystem(descriptor: AppDescriptor) -> Ecosystem[Any, Any, Any]:
    """Resolve the descriptor's declared ecosystem against the registry."""
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


def _default_store(source: SourceCode, descriptor: AppDescriptor) -> RustArtifactStore:
    return RustArtifactStore(
        source.project_root,
        descriptor.artifact_layout,
        deliverable_mode=descriptor.deliverable_mode,
        program=str(source.contract_name),
    )


def build_backend(
    module: Any,
    descriptor: AppDescriptor,
    source: SourceCode,
    *,
    phase: type[enum.Enum] | None = None,
    core_phases: CorePhases | None = None,
    store_factory: StoreFactory | None = None,
    backend_cls: type[RustBackend] = RustBackend,
    options: BackendOptions | None = None,
) -> RustBackend:
    """Construct a :class:`RustBackend` (phase enum + core phases + store + ecosystem).

    Prefer :func:`build_application` + :meth:`RustApplication.make_backend` so the
    frontend and pipeline share one phase enum. This is the headless path.
    """
    opts = options or BackendOptions()
    ph = phase if phase is not None else build_phase_enum(descriptor)
    core = core_phases if core_phases is not None else build_core_phases(descriptor, ph)
    sf = store_factory or _default_store
    return backend_cls(
        module=module,
        descriptor=descriptor,
        _phase=ph,
        _core_phases=core,
        artifact_store=sf(source, descriptor),
        ecosystem=resolve_ecosystem(descriptor),
        command_timeout_s=opts.command_timeout_s,
        sandbox=opts.sandbox,
        declared_args=opts.declared_args,
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
) -> CorePipelineResult[RustFormalResult]:
    """Build the backend from ``module_name`` and run the shared driver — the Rust
    analogue of ``run_autoprove_pipeline`` / ``run_foundry_pipeline``.

    This synthesizes a *fresh* phase enum internal to the backend. It is the right
    entry for headless callers whose handler ignores phases; for a TUI/console
    frontend, build a :class:`RustApplication` once and use :func:`run_application`
    so the frontend's labels and the backend's phases share one enum object."""
    app = build_application(module_name)
    return await run_application(
        app,
        source_input,
        ctx,
        handler_factory,
        env,
        max_concurrent=max_concurrent,
        max_bug_rounds=max_bug_rounds,
        interactive=interactive,
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
) -> CorePipelineResult[RustFormalResult]:
    """Run a pre-built :class:`RustApplication`. The backend is constructed from the
    app's already-synthesized phase enum, so the ``TaskInfo`` phases the driver emits
    are the *same* enum members the frontend's ``phase_labels`` are keyed by — the
    identity the frontend's label lookup relies on."""
    backend = app.make_backend(source_input)
    run = PipelineRun(
        ctx=ctx, source=source_input, _handler_factory=handler_factory,
        _semaphore=asyncio.Semaphore(max_concurrent), env=env,
    )
    return await run_pipeline(
        backend, run, ecosystem=app.ecosystem, interactive=interactive, threat_model=None, max_bug_rounds=max_bug_rounds
    )


@dataclass
class RustApplication:
    """Everything a frontend / ``main()`` needs, synthesized from the descriptor.

    ``phase_labels`` is keyed by the synthesized enum members (member identity
    drives the frontend's label lookup), and ``section_order`` lists every phase
    label in declared order — the two inputs a ``MultiJobApp`` frontend consumes.

    ``options`` is mutable so the CLI can apply parsed flags (timeouts, sandbox)
    before :func:`run_application` without rebuilding the phase enum.
    """

    descriptor: AppDescriptor
    module: Any
    ecosystem: Ecosystem[Any, Any, Any]
    phase: type[enum.Enum]
    core_phases: CorePhases
    phase_labels: dict[Any, str]
    section_order: list[str]
    options: BackendOptions = field(default_factory=BackendOptions)
    store_factory: StoreFactory = field(default=_default_store)
    backend_cls: type[RustBackend] = RustBackend

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

    def make_backend(self, source: SourceCode) -> RustBackend:
        """Build the backend for this run — same phase enum as :attr:`phase_labels`."""
        return build_backend(
            self.module,
            self.descriptor,
            source,
            phase=self.phase,
            core_phases=self.core_phases,
            store_factory=self.store_factory,
            backend_cls=self.backend_cls,
            options=self.options,
        )


def build_application(
    module_name: str,
    *,
    store_factory: StoreFactory | None = None,
    backend_cls: type[RustBackend] = RustBackend,
    command_timeout_s: int = DEFAULT_TIMEOUT_S,
    sandbox: SandboxConfig | None = None,
) -> RustApplication:
    """Load a Rust wheel and synthesize a :class:`RustApplication`.

    ``store_factory`` / ``backend_cls`` let an application supply a specialized
    store or prepared-system path (Crucible) while keeping one phase enum for the
    frontend and the pipeline.
    """
    module = load_module(module_name)
    descriptor = load_descriptor(module)
    ecosystem = resolve_ecosystem(descriptor)
    phase = build_phase_enum(descriptor)
    core = build_core_phases(descriptor, phase)
    ordered = descriptor.ordered_phases()
    phase_labels = {phase[p.key]: p.label for p in ordered}
    section_order = [p.label for p in ordered]

    return RustApplication(
        descriptor=descriptor,
        module=module,
        ecosystem=ecosystem,
        phase=phase,
        core_phases=core,
        phase_labels=phase_labels,
        section_order=section_order,
        options=BackendOptions(
            command_timeout_s=command_timeout_s,
            sandbox=sandbox,
        ),
        store_factory=store_factory or _default_store,
        backend_cls=backend_cls,
    )
