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
import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from composer.io.multi_job import HandlerFactory
from composer.pipeline.core import (
    DEFAULT_MAX_CPU_TASKS,
    CorePipelineResult,
    PipelineRun,
    run_pipeline,
)
from composer.pipeline.ecosystem import ECOSYSTEMS, Ecosystem
from composer.rustapp.adapter import RustBackend
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.phases import PhaseModel, build_phase_model
from composer.rustapp.result import RustFormalResult
from composer.rustapp.store import RustArtifactStore
from composer.rustapp.wire import CALLOUTS, AppArgs, RustAppModule, expect_payload, expect_text
from composer.sandbox.command import DEFAULT_TIMEOUT_S
from composer.sandbox.config import SandboxConfig
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ServiceHost
from composer.tools.rag_env import validate_rag_db

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
    #: Parsed values of the descriptor's declared CLI args, threaded into the backend and put on
    #: every ``AuthorInput.args``. Set by the entry point.
    declared_args: dict[str, Any] = field(default_factory=dict)


def load_module(module_name: str) -> RustAppModule:
    """Import a Rust application's compiled module by name (e.g. ``"echoprover"``).

    The cast is the one honest dynamic boundary in the host — nothing about ``import_module`` can be
    checked statically. So every callout the host will call is verified *here* instead: a module that
    isn't an AutoProver wheel, or is one built against an older SDK, fails at load with the missing
    names listed rather than with an ``AttributeError`` several phases into a run."""
    module = importlib.import_module(module_name)
    missing = [name for name in CALLOUTS if not callable(getattr(module, name, None))]
    if missing:
        raise TypeError(
            f"{module_name!r} is not a complete AutoProver application module: it exports no "
            f"{', '.join(missing)}. Rebuild the wheel against the current autoprover-sdk "
            "(`export_app!` exports every callout)."
        )
    return cast(RustAppModule, module)


def load_descriptor(module: RustAppModule) -> AppDescriptor:
    """Parse a module's ``descriptor()`` JSON into an :class:`AppDescriptor`."""
    return AppDescriptor.model_validate_json(expect_payload(module.descriptor()))


def resolve_ecosystem(descriptor: AppDescriptor) -> Ecosystem[Any, Any, Any]:
    """Resolve the descriptor's declared ecosystem against the registry."""
    eco = ECOSYSTEMS.get(descriptor.ecosystem)
    if eco is None:
        raise ValueError(
            f"application {descriptor.name!r} selects ecosystem {descriptor.ecosystem!r}, "
            f"which is not registered. Available: {sorted(ECOSYSTEMS)}."
        )
    return eco


def _default_store(source: SourceCode, descriptor: AppDescriptor) -> RustArtifactStore:
    return RustArtifactStore(
        source.project_root,
        descriptor.artifact_layout,
        deliverable_mode=descriptor.deliverable_mode,
        program=str(source.contract_name),
    )


def build_backend(
    module: RustAppModule,
    descriptor: AppDescriptor,
    source: SourceCode,
    *,
    phases: PhaseModel,
    store_factory: StoreFactory | None = None,
    backend_cls: type[RustBackend] = RustBackend,
    options: BackendOptions | None = None,
) -> RustBackend:
    """Construct a :class:`RustBackend` around an already-built :class:`PhaseModel`.

    The model is required, not defaulted: a backend that synthesized its own would tag tasks with
    enum members no frontend's labels are keyed by. :meth:`RustApplication.make_backend` is the
    usual caller; this is the headless path.
    """
    opts = options or BackendOptions()
    sf = store_factory or _default_store
    return backend_cls(
        module=module,
        descriptor=descriptor,
        phases=phases,
        store=sf(source, descriptor),
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
    max_cpu_tasks: int = DEFAULT_MAX_CPU_TASKS,
    max_bug_rounds: int = 3,
    interactive: bool = False,
) -> CorePipelineResult[RustFormalResult]:
    """Build the backend from ``module_name`` and run the shared driver — the Rust
    analogue of ``run_autoprove_pipeline`` / ``run_foundry_pipeline``.

    This builds the application (and so its phase model) per call. It is the right entry for
    headless callers whose handler ignores phases; for a TUI/console frontend, build a
    :class:`RustApplication` once and use :func:`run_application`, so the frontend's labels and the
    backend's phases come from the one model."""
    app = build_application(module_name)
    return await run_application(
        app,
        source_input,
        ctx,
        handler_factory,
        env,
        max_concurrent=max_concurrent,
        max_cpu_tasks=max_cpu_tasks,
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
    max_cpu_tasks: int = DEFAULT_MAX_CPU_TASKS,
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
        _agent_semaphore=asyncio.Semaphore(max_concurrent),
        _cpu_semaphore=asyncio.Semaphore(max_cpu_tasks), env=env,
    )
    return await run_pipeline(
        backend, run, ecosystem=app.ecosystem, interactive=interactive, threat_model=None, max_bug_rounds=max_bug_rounds
    )


@dataclass
class RustApplication:
    """Everything a frontend / ``main()`` needs, synthesized from the descriptor.

    ``phases`` is the single :class:`PhaseModel` this application runs on — the backend's phases and
    the frontend's labels both come from it, which is what keeps their members identical.

    ``options`` is mutable so the CLI can apply parsed flags (timeouts, sandbox)
    before :func:`run_application` without rebuilding the phase model.
    """

    descriptor: AppDescriptor
    module: RustAppModule
    ecosystem: Ecosystem[Any, Any, Any]
    phases: PhaseModel
    options: BackendOptions = field(default_factory=BackendOptions)
    store_factory: StoreFactory = field(default=_default_store)
    backend_cls: type[RustBackend] = RustBackend

    @property
    def name(self) -> str:
        return self.descriptor.name

    @property
    def header_text(self) -> str:
        return self.descriptor.header_text

    def validate_preconditions(self, args: AppArgs) -> str | None:
        """Delegate to the Rust precondition hook; return an error string or None."""
        return expect_text(self.module.validate_preconditions(args.model_dump_json()))

    def make_backend(self, source: SourceCode) -> RustBackend:
        """Build the backend for this run — on this application's own :attr:`phases`."""
        return build_backend(
            self.module,
            self.descriptor,
            source,
            phases=self.phases,
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
    # Both of the descriptor's registry references are resolved up-front, before the run spends
    # anything: an unknown ecosystem or an unregistered RAG corpus is a wheel bug, not something to
    # discover mid-run (an unavailable corpus, in contrast, degrades — see ``rag_env``).
    validate_rag_db(descriptor.rag_db_default)

    return RustApplication(
        descriptor=descriptor,
        module=module,
        ecosystem=ecosystem,
        phases=build_phase_model(descriptor),
        options=BackendOptions(
            command_timeout_s=command_timeout_s,
            sandbox=sandbox,
        ),
        store_factory=store_factory or _default_store,
        backend_cls=backend_cls,
    )
