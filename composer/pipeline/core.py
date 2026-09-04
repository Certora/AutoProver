"""Backend-agnostic spec-generation spine.

Phase chain — each link is immutable and its existence proves the prior phase ran, so ordering is
a constructor dependency rather than a call-order convention; there is no half-initialized state:

    Backend ──preflight──▶ Pre ──prepare_system──▶ PreparedSystem ──prepare_formalization──▶ Formalizer
    (config, source)       (built    (.main: structure)                                      (formalize /
                            workspace)                                                        persist / report)

Two links are *overlapped* with the LLM steps they don't depend on, since both are usually builds:
``preflight`` runs alongside system analysis, and ``prepare_formalization`` alongside property
extraction. ``preflight`` is additionally a *gate*: it and the analysis run in a task group, so a
failure on either side cancels the other rather than letting it spend on a run that can no longer
complete — a broken toolchain does not wait out the analysis agent, and a failed analysis does not
wait out the workspace build. The second pair is simply awaited in turn.

A backend whose units all build on one *shared* artifact inserts a link rather than a call-order
convention: ``prepare_formalization`` returns a :class:`StagedFormalizer`, and its ``begin`` — handed
every unit's properties — is what produces the :class:`Formalizer`. Same rule as the rest of the
chain, so it needs no new rule: the artifact is a constructor argument to the only object that uses
it, and no formalizer ever exists without it.

The driver owns the genuinely-shared steps: system analysis, per-component property extraction, the
result-type-keyed cache, and (since the report is backend-agnostic) building + persisting the
property-keyed report. Everything backend-specific — the harnessed lift, autosetup/summaries/
invariant fan-out, the formalizer itself, per-unit verdicts — is contributed through the three
phase objects, and never inspected by the driver.
"""

import asyncio
import enum
import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Protocol, Any, ClassVar, Concatenate, cast, Awaitable, Sequence, Callable, ContextManager, overload
)
from abc import ABC, abstractmethod
from contextlib import nullcontext


from composer.io.multi_job import TaskInfo
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import (
    WorkflowContext, ComponentGroup
)
from composer.spec.system_model import (
    BaseApplication, FeatureUnit
)
from composer.spec.util import combine_digests
from composer.spec.types import PropertyFormulation, ArtifactIdentifier, VerificationArtifact
from composer.spec.system_analysis import run_component_analysis
from composer.spec.prop_inference import (
    run_property_inference, AnyPropertyGenerationInput, CacheablePropertyGenerationInput,
)
from composer.llm.types import CacheLevel
from composer.input.files import Document
from composer.spec.source.report.build import build_report
from composer.spec.source.report.collect import ReportComponentInput, Verdict, EvidenceFetcher, Formalized
from composer.spec.source.report.schema import (
    AutoProverReport, RuleName, ReportBackend, SourceEditRecord, VerificationArtifactRecord,
)
from composer.spec.source.report import build as report_build
from composer.spec.source.task_ids import SYSTEM_ANALYSIS_TASK_ID, REPORT_TASK_ID
from composer.pipeline.ecosystem import Ecosystem
from .keys import (
    COMPONENT_KEY, FINAL_PROPERTIES_KEY, FORMALIZATION_KEY, PLUGIN_ARTIFACTS_KEY,
    POST_PROPERTY_KEY, PRE_PROPERTY_KEY, PROPERTIES_KEY, SYSTEM_ANALYSIS_KEY,
    PLUGIN_FORMALIZATION_KEY
)
from composer.diagnostics.budget import total_budget, named_budget_or_nop, time_budget

from .ptypes import (
    DEFAULT_MAX_CPU_TASKS,
    BackendJob, BackendResult, ComponentOutcome, CorePhases, CorePipelineResult,
    Curtailed,  Delivered, RunBudget,
    FinalProperties, GaveUp, PersistedPluginArtifact, PipelineRun, PluginArtifact,
    RegisteredArtifacts, SystemAnalysisSpec
)
from .plugin_api import (
    AnyBackendTools, ArtifactRegistrar, FormalizationTool, PipelinePlugin,
    PluginToolContext, ProvidedTools,
)
from .plugins import load_plugins, PluginManager, PluginPhaseManager, PluginPhaseRunner

_log = logging.getLogger(__name__)


@dataclass
class ToolExtension[TP: PipelinePlugin[Any], U: FeatureUnit, **P]:
    """A backend's tool-extension point, handed to the :class:`ToolBinder` at
    dispatch time: the provider class whose derivers contribute (``provider``) and
    the selection of their hook method (``project`` — canonically
    ``lambda p: p.<backend>_tools``, threading the dispatch leg itself, not a
    wrapper). ``P`` captures the hook's backend-specific tail (its ``st`` /
    ``state_reader``), which the binder forwards from the dispatch call. The bound
    ``TP <: PipelinePlugin[U]`` is inexpressible, so unit agreement rides
    ``project``'s signature statically — and, at runtime, the provider classes
    deriving ``PipelinePlugin[U]`` plus the load-time scope check."""
    provider: type[TP]
    project: Callable[
        [TP],
        Callable[
            Concatenate[U, Sequence[PropertyFormulation], PluginToolContext[FormalizationTool], P],
            Awaitable[ProvidedTools | None],
        ],
    ]

@dataclass
class InjectingToolExtension[
    TP: PipelinePlugin[Any],
    U: FeatureUnit,
    **P,
    Ext
]:
    """A :class:`ToolExtension` whose hook additionally receives a backend-supplied
    callable. The backend passes the binder a ``Callable[Concatenate[str, I], R]``;
    the binder curries in the asking plugin's id, so the hook sees a
    ``Callable[I, R]`` that already knows who it is serving — attribution is
    stamped by the driver, never self-reported (the registrar in
    :class:`_ArtifactCollector` works the same way)."""
    provider: type[TP]
    project: Callable[
        [TP],
        Callable[
            Concatenate[U, Sequence[PropertyFormulation], PluginToolContext[FormalizationTool], Ext, P],
            Awaitable[ProvidedTools | None]
        ]
    ]



class ToolBinder[U: FeatureUnit](Protocol):
    """What ``formalize`` receives — the core ↔ backend tool seam, naming no
    backend. The backend calls it exactly once, with its own
    :class:`ToolExtension` and the extension's tail arguments (its authoring
    graph's state type and staged-state reader); the binder applies every
    contributing plugin's hook — always including :class:`AnyBackendTools`
    derivers — and returns the yielded bundles. Empty when no plugin contributes,
    so backends need no special-casing."""

    @overload
    async def __call__[TP: PipelinePlugin[Any], **P](
        self, ext: ToolExtension[TP, U, P], *args: P.args, **kwargs: P.kwargs
    ) -> Sequence[ProvidedTools]:
        ...

    @overload
    async def __call__[TP: PipelinePlugin[Any], **P, **I, R](
        self, ext: InjectingToolExtension[TP, U, P, Callable[I, R]], inj: Callable[Concatenate[str, I], R], /, *args: P.args, **kwargs: P.kwargs
    ) -> Sequence[ProvidedTools]:
        ...

@dataclass
class Formalizer[FormT: BackendResult, U: FeatureUnit](ABC):
    """Immutable, fully constructed by whatever produced it — ``prepare_formalization``, or
    :meth:`StagedFormalizer.begin` for a backend with a shared artifact. Carries the prover's
    config/resources/prover_tool/invariant-results (or nothing, for foundry) as constructor
    state — never set post-hoc. `FormT: ReportableResult` is what makes the report a core step.

    Generic over ``U``, the *formalized unit* type it consumes (EVM's ``ContractComponentInstance``,
    a Rust backend's ``FeatureUnit``): the backend works with its concrete unit — reading its
    members without casts — while the driver stays unit-agnostic."""
    formalized_type: type[FormT]
    backend_tag: ReportBackend

    #: The provider class whose derivers contribute tools to this backend — the
    #: driver matches it for both the dispatch gate and the formalization cache
    #: key. None (the default) for a backend with no tool extension;
    #: :class:`AnyBackendTools` derivers count regardless.
    tool_provider_type: ClassVar[type[PipelinePlugin[Any]] | None] = None

    @abstractmethod
    async def formalize(
        self,
        label: str,
        feat: U,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[FormT],
        run: PipelineRun,
        extra_tools: ToolBinder[U]
    ) -> FormT | Curtailed[FormT] | GaveUp:
        """``extra_tools`` is the run's tool binder: call it exactly once with this
        backend's :class:`ToolExtension` and the extension's tail (the authoring
        graph's state type and staged-state reader)."""
        ...

    def extra_report_inputs(self) -> list[ReportComponentInput[FormT]]:
        """Synthetic report inputs beyond the per-component outcomes — the prover folds in its
        'Structural Invariants' here. Default: none."""
        return []

    async def source_edits(
        self, outcomes: list[ComponentOutcome[FormT, U]], run: PipelineRun
    ) -> list[SourceEditRecord]:
        """Source-modification records for components whose verification ran against edited
        source. Default: none (foundry; prover runs without editing)."""
        return []

    @abstractmethod
    async def fetch_verdicts(self, formalized: Formalized[FormT]) -> dict[RuleName, Verdict]:
        """Per-unit outcomes for one delivered result. Prover: query ProverOutputUtility via
        ``formalized.run_link`` off-thread. Foundry: read straight off ``formalized.result``.
        Never called for gave-up or budget-curtailed components."""
        ...

    def findings_evidence(self) -> EvidenceFetcher | None:
        """The per-rule evidence source for findings synthesis, or None if this backend produces no
        findings. Returning None is how a backend opts out — the report then builds no findings for
        it, with no backend-specific branching in the report layer. Default: None."""
        return None

    async def finalize(self, outcomes: list[ComponentOutcome[FormT, U]], run: PipelineRun) -> None:
        """Emit any backend-specific run-level artifacts from the full outcome set (prover:
        components_to_prover_runs.json). Default: none."""
        return None


class StagedFormalizer[FormT: BackendResult, U: FeatureUnit](ABC):
    """A formalizer that cannot exist yet: returned from ``prepare_formalization`` in place of a
    :class:`Formalizer` by backends whose units all build on one *shared* artifact — Crucible's
    fixture, whatever setup module a CVLR backend needs.

    Such an artifact must be authored from the union of every unit's properties (that union is what
    makes them checkable), which pins it to exactly one point in the run. Not
    ``prepare_formalization``: that overlaps extraction, so no properties exist there yet. Not lazily
    on first ``formalize``: whichever unit won the race would decide the artifact all the others are
    then told to work within — harmless at one unit, silently wrong at several. ``begin`` sits
    between the two, where extraction is done and no unit has been formalized.

    Some backends need none of this and return a ``Formalizer`` directly; the prover's shared peer
    (``invariants.spec``) is staged in ``prepare_formalization``."""

    @abstractmethod
    async def begin(
        self, jobs: Sequence[BackendJob[U]], run: PipelineRun
    ) -> Formalizer[FormT, U]:
        """Author the shared artifact from **every** unit's properties, and return the formalizer
        built around it. Called once, after extraction, before the per-unit fan-out."""
        ...


@dataclass
class PreparedSystem[FormT: BackendResult, U: FeatureUnit, Main](ABC):
    #: The located "main" of the analyzed program — the ecosystem's ``Main`` type (EVM's
    #: :class:`~composer.spec.system_model.ContractInstance`, Solana's
    #: :class:`~composer.spec.solana.model.SolanaProgramInstance`). This is a *different* axis
    #: from :class:`FeatureUnit` (the per-unit items ``units()`` iterates): EVM's main is not a
    #: unit. The driver treats it opaquely — it only hands it to ``ecosystem.units(main)`` —
    #: so each backend binds ``Main`` to its ecosystem's type.
    main: Main

    @abstractmethod
    async def prepare_formalization(
        self, run: PipelineRun
    ) -> Formalizer[FormT, U] | StagedFormalizer[FormT, U]:
        """The formalizer — or, for a backend whose units share an artifact, the
        :class:`StagedFormalizer` that becomes one once every unit's properties are known. Which of
        the two a backend returns is its own declared signature, so a backend with no shared artifact
        never mentions staging at all."""
        ...


class PipelineBackend[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier, U: FeatureUnit, Main, App: BaseApplication, Pre](Protocol):
    @property
    def backend_guidance(self) -> str: ...

    @property
    def analysis_spec(self) -> SystemAnalysisSpec: ...

    @property
    def core_phases(self) -> CorePhases[P]: ...

    @property
    def artifact_store(self) -> ArtifactStore[A, FormT]: ...


    async def preflight(self, run: PipelineRun[P, H]) -> Pre:
        """Whatever the backend can do before it knows anything about the program — run
        *concurrently with system analysis*, and awaited before :meth:`prepare_system`.

        This is where a backend that must build something puts the build. Crucible prepares its
        harness crate, compiles the program to sBPF, and gates a wheel-authored skeleton harness
        through the real toolchain; none of that reads the analyzed model, so serializing it behind
        analysis buys nothing, while running it alongside means a broken dependency graph or an
        unbuildable program surfaces before the run has spent meaningfully on the model.

        ``Pre`` is opaque to the driver — it only carries the result to ``prepare_system``, so a
        backend can hand its own prep forward as immutable state rather than stashing it on itself.
        ``None`` for a backend with nothing to do ahead of time."""
        ...

    async def prepare_system(
        self, analyzed: App,
        run: PipelineRun[P, H],
        preflight: Pre,
    ) -> PreparedSystem[FormT, U, Main]: ...

    def to_artifact_id(self, c: U) -> A: ...


# ---- the per-unit batch -------------------------------------------------------
@dataclass
class _Batch[U: FeatureUnit](BackendJob[U]):
    feat_ctx: WorkflowContext[ComponentGroup]


def extract_task_id(idx: int) -> str:
    return f"extract-{idx}"


def formalize_task_id(idx: int) -> str:
    return f"formalize-{idx}"


# ---- plugin tool contribution -------------------------------------------------

class _ArtifactCollector:
    """Per-batch sink for the verification artifacts plugin tools register at
    dispatch time. ``for_plugin`` stamps the attribution, so a plugin never
    handles its own id; the driver snapshots the collected set into the
    formalization namespace (cache replays skip the tools) and persists it via
    the run's artifact store."""

    def __init__(self) -> None:
        self.items: list[PluginArtifact] = []

    def for_plugin(self, plugin_id: str) -> ArtifactRegistrar:
        collector = self

        class _Registrar:
            def register(self, artifact: VerificationArtifact) -> None:
                collector.items.append(
                    PluginArtifact(plugin=plugin_id, artifact=artifact)
                )

        return _Registrar()


def _contributes_tools(
    plugin: PipelinePlugin[Any], provider_type: type[PipelinePlugin[Any]] | None
) -> bool:
    """Whether ``plugin`` counts for the running backend's tool dispatch — and so
    for its ``FORMALIZATION_KEY``: a deriver of the formalizer's declared provider
    class, or of the always-projected :class:`AnyBackendTools`. One predicate for
    the gate and the key, so what the binder can reach and what the key says
    cannot drift."""
    if isinstance(plugin, AnyBackendTools):
        return True
    return provider_type is not None and isinstance(plugin, provider_type)


@dataclass
class _Binder[U: FeatureUnit]:
    """Core's :class:`ToolBinder` over the batch's contributing plugins, each
    pre-paired with its id and plugin context. Plugins fire in the deterministic
    plugin-id order the gate collected them (injection order is prompt content);
    per plugin, the extension's hook fires before the any-backend hook. Plugin
    hook code runs only here — at the backend's dispatch — never as a top-level
    tool-setup task. On an :class:`InjectingToolExtension` dispatch, the
    backend's injectable gets the asking plugin's id curried in before the hook
    ever sees it."""
    _plugins: Sequence[tuple[str, PipelinePlugin[U], PluginToolContext[FormalizationTool]]]
    _comp: U
    _props: list[PropertyFormulation]

    @overload
    async def __call__[TP: PipelinePlugin[Any], **P](
        self, ext: ToolExtension[TP, U, P], *args: P.args, **kwargs: P.kwargs
    ) -> Sequence[ProvidedTools]:
        ...

    @overload
    async def __call__[TP: PipelinePlugin[Any], **P, **I, R](
        self, ext: InjectingToolExtension[TP, U, P, Callable[I, R]], inj: Callable[Concatenate[str, I], R], /, *args: P.args, **kwargs: P.kwargs
    ) -> Sequence[ProvidedTools]:
        ...

    async def __call__( #type: ignore[trust me bro]
        self,
        ext: ToolExtension[Any, U, ...] | InjectingToolExtension[Any, U, ..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Sequence[ProvidedTools]:
        got: list[ProvidedTools] = []
        for plugin_id, p, ctxt in self._plugins:
            if isinstance(p, ext.provider):
                if isinstance(ext, InjectingToolExtension):
                    # args[0] is the backend's injectable (positional-only in
                    # the overload); the hook receives it with this plugin's
                    # id already bound.
                    t = await ext.project(p)(
                        self._comp, self._props, ctxt,
                        functools.partial(args[0], plugin_id),
                        *args[1:], **kwargs,
                    )
                else:
                    t = await ext.project(p)(self._comp, self._props, ctxt, *args, **kwargs)
                if t is not None:
                    got.append(t)
            if isinstance(p, AnyBackendTools):
                t = await p.backend_tools(self._comp, self._props, ctxt)
                if t is not None:
                    got.append(t)
        return got



def _budget_context(budget: RunBudget | None) -> ContextManager[None]:
    if budget is not None:
        return total_budget(budget.total, cast(dict[str, float], budget.caps))
    else:
        return nullcontext()

def _time_context(time_budget_s: float | None) -> ContextManager[None]:
    if time_budget_s is not None:
        return time_budget(time_budget_s)
    else:
        return nullcontext()

async def run_pipeline[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier, U: FeatureUnit, Main, App: BaseApplication, Pre](
    backend: PipelineBackend[P, FormT, H, A, U, Main, App, Pre],
    run: PipelineRun[P, H],
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    extra_context: Sequence[Document] = (),
    max_bug_rounds: int = 3,
    ecosystem: Ecosystem[App, Main, U],
    budget: RunBudget | None = None,
    time_budget_s : float | None = None
) -> CorePipelineResult[FormT]:
    with (
        _budget_context(budget),
        _time_context(time_budget_s)
    ):
        return await _run_pipeline_inner(
            backend, run, interactive=interactive, 
            max_bug_rounds=max_bug_rounds, threat_model=threat_model,
            extra_context=extra_context, ecosystem=ecosystem
        )

async def _run_pipeline_inner[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier, U: FeatureUnit, Main, App: BaseApplication, Pre](
    backend: PipelineBackend[P, FormT, H, A, U, Main, App, Pre],
    run: PipelineRun[P, H],
    *,
    interactive: bool,
    threat_model: Document | None,
    extra_context: Sequence[Document],
    max_bug_rounds: int,
    ecosystem: Ecosystem[App, Main, U],
) -> CorePipelineResult[FormT]:
    # Only the plugins whose hooks accept this ecosystem's unit are loaded (and only those pay
    # their ``initialize`` cost); the driver below can hand them its units unconditionally.
    async with load_plugins(run, ecosystem.unit_type) as plugins:
        return await run_pipeline_inner(
            backend, run, plugins, interactive=interactive, threat_model=threat_model,
            extra_context=extra_context, max_bug_rounds=max_bug_rounds, ecosystem=ecosystem,
        )

# ---- the driver --------------------------------------------------------------
async def run_pipeline_inner[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier, U: FeatureUnit, Main, App: BaseApplication, Pre](
    backend: PipelineBackend[P, FormT, H, A, U, Main, App, Pre],
    run: PipelineRun[P, H],
    plugin_manager: PluginManager[P, U],
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    extra_context: Sequence[Document] = (),
    max_bug_rounds: int = 3,
    ecosystem: Ecosystem[App, Main, U],
) -> CorePipelineResult[FormT]:
    spec, phases = backend.analysis_spec, backend.core_phases
    source = run.source

    assert ecosystem.supports_greenfield or run.env.sort != "greenfield", (
        f"ecosystem {ecosystem.name!r} has no greenfield prompts; got sort='greenfield'"
    )

    # 1. Backend preflight ∥ system analysis. The preflight (Crucible: build the program and gate a
    #    skeleton harness through the real toolchain) reads nothing analysis produces, so it starts
    #    at t=0. Neither side outlives the other's failure: whichever breaks first, the group cancels
    #    the one still running rather than let it spend — money on the analysis agent, minutes on the
    #    workspace build — on a run that can no longer complete. The analysis itself is the shared
    #    primitive; the ecosystem supplies the analyzed model type, prompts, validation, front-matter.
    #    The budget scope is entered inside the coroutine (not around create_task) so the
    #    cost-center binding lives in the spawned task's own context.
    async def _run_analysis():
        with named_budget_or_nop("system_analysis"):
            return await run.runner(
                TaskInfo(SYSTEM_ANALYSIS_TASK_ID, "System Analysis", phases["analysis"]),
                lambda: run_component_analysis(
                    ty=ecosystem.system_model,
                    child_ctxt=run.ctx.child(SYSTEM_ANALYSIS_KEY(ecosystem.system_model, spec.analysis_key)),
                    input=source, env=run.env,
                    extra_input=[*ecosystem.analysis_extra_input(source), *spec.extra_input],
                    expected_main_id=source.contract_name,
                    project_root=Path(source.project_root),
                    forbidden_read=source.forbidden_read,
                    system_template=ecosystem.analysis_prompts.system,
                    initial_template=ecosystem.analysis_prompts.initial,
                    validate=ecosystem.validate_analysis,
                ),
            )

    try:
        async with asyncio.TaskGroup() as overlap:
            preflight_task = overlap.create_task(backend.preflight(run))
            analysis_task = overlap.create_task(_run_analysis())
    except BaseExceptionGroup as eg:
        # Callers expect the failure itself, not a wrapper, so unwrap the usual case: one side
        # failed and the other was cancelled, and a cancelled task adds nothing to the group.
        # Both failing at once is the only case with two real errors; keep the group there.
        if len(eg.exceptions) == 1:
            raise eg.exceptions[0] from None
        raise
    preflight, analyzed = preflight_task.result(), analysis_task.result()
    if analyzed is None:
        raise ValueError("System analysis produced no result.")

    # 2. Backend transform + main-contract location (prover: harness lift; foundry: identity).
    with named_budget_or_nop("system_preparation"):
        prepared = await backend.prepare_system(analyzed, run, preflight)

    # 3. Pre-formalization setup runs CONCURRENTLY with extraction (neither needs the other) —
    #    this preserves the prover's autosetup ∥ bug-analysis overlap, generically.
    #    The budget scope is entered inside the coroutine (not around create_task) so the
    #    cost-center binding lives in the spawned task's own context.
    async def _prepare_formalization() -> Formalizer[FormT, U] | StagedFormalizer[FormT, U]:
        with named_budget_or_nop("formalization_preparation"):
            return await prepared.prepare_formalization(run)
    staged_task = asyncio.create_task(_prepare_formalization())

    batches: list[_Batch[U]] = await _extract_all(
        backend.analysis_spec.properties_key,
        prepared.main,
        backend.backend_guidance,
        run,
        phases["extraction"],
        interactive,
        threat_model,
        extra_context,
        max_bug_rounds,
        ecosystem,
        plugin_manager.bind_phase(
            phases.get("extraction_plugin") or phases["extraction"],
        )
    )
    staged = await staged_task
    if not batches:
        raise ValueError("No properties extracted from any component.")

    # 4. A backend whose units share an artifact handed back a ``StagedFormalizer`` instead of a
    #    formalizer: the artifact is authored HERE — once, from every unit's properties — and the
    #    formalizer it yields is the only one that exists (see :class:`StagedFormalizer`).
    with named_budget_or_nop("formalization_preparation"):
        formalizer = (
            await staged.begin(batches, run) if isinstance(staged, StagedFormalizer) else staged
        )

    # 5. Per-component formalization. Caching is core-owned, keyed by the backend's result type.
    async def _run(batch: _Batch[U]) -> ComponentOutcome[FormT, U]:
        result_key = backend.to_artifact_id(batch.feat)
        backend.artifact_store.write_properties(result_key, batch.props)

        # Deriving the backend's declared provider class gates the dispatch AND
        # keys the cache: whether a hook would actually yield tools is only
        # knowable inside ``formalize`` (dispatch needs backend state that
        # doesn't exist yet), so both run off what the plugin derives. A plugin
        # outside this backend's gate is invisible to its binder, and so must
        # not perturb its key either. No task machinery: tool hooks are
        # tool-BINDING hooks, so they get the runner-less context and cannot
        # run top-level tasks.
        contributing_plugins : list[str] = []
        bound_plugins : list[tuple[str, PipelinePlugin[U], PluginToolContext[FormalizationTool]]] = []
        collected = _ArtifactCollector()

        for plugin_id, plugin in plugin_manager.sorted_plugins():
            if not _contributes_tools(plugin, formalizer.tool_provider_type):
                continue
            k = batch.feat_ctx.child(PLUGIN_FORMALIZATION_KEY(plugin_id))
            bound_plugins.append((plugin_id, plugin, plugin_manager.tool_context(
                plugin, k, collected.for_plugin(plugin_id),
            )))
            contributing_plugins.append(plugin_id)

        # Record the formalization edge's exact derivation inputs — the final
        # (post-plugin) batch and the gated plugin ids — as a first-class cache
        # entry, keyed like the bug analysis (the component namespace is shared
        # across runs). Offline walkers reconstruct the edge from this instead
        # of sniffing the store.
        await batch.feat_ctx.child(
            FINAL_PROPERTIES_KEY(
                threat_model.to_digest() if threat_model is not None else None,
                interactive,
                combine_digests([d.to_digest() for d in extra_context]),
            )
        ).cache_put(FinalProperties(items=batch.props, tool_plugins=contributing_plugins))

        child : WorkflowContext[FormT] = await batch.feat_ctx.child(
            FORMALIZATION_KEY(formalizer.formalized_type, batch.props, contributing_plugins),
            {"properties": [p.model_dump() for p in batch.props]},
        )
        artifacts_ctx = child.child(PLUGIN_ARTIFACTS_KEY)
        cached_result: FormT | None = await child.cache_get(formalizer.formalized_type)
        result : FormT | Curtailed[FormT] | GaveUp
        if cached_result is None:
            label = f"{batch.feat.display_name} ({len(batch.props)} properties)"
            with named_budget_or_nop("formalization"):
                result : FormT | GaveUp | Curtailed[FormT] = await run.runner(
                    TaskInfo(
                        formalize_task_id(batch.feat.unit_index),
                        label,
                        phases["formalization"]
                    ),
                    lambda: formalizer.formalize(
                        label, batch.feat, batch.props, child, run,
                        _Binder(bound_plugins, batch.feat, batch.props)
                    ),
                )
            # Snapshot what the tools registered: a cache replay of this
            # formalization never runs the tools, so the artifacts must ride
            # the cache alongside the result they accompany.
            if not isinstance(result, GaveUp):
                await artifacts_ctx.cache_put(RegisteredArtifacts(items=collected.items))
                # Envelope before result: a crash between the two puts replays
                # as a miss (re-run, envelope rewritten) rather than a hit with
                # silently absent artifacts. A curtailed partial is never the
                # component's cached result — a later run redoes the
                # formalization under a fresh budget.
                if not isinstance(result, Curtailed):
                    await child.cache_put(result)
        else:
            result = cached_result
            restored = await artifacts_ctx.cache_get(RegisteredArtifacts)
            collected.items = restored.items if restored is not None else []

        outcome: Delivered[FormT] | Curtailed[Delivered[FormT]] | GaveUp
        if isinstance(result, GaveUp):
            outcome = result
        elif isinstance(result, Curtailed):
            # Persist the partial for inspection under a quarantined name — never as the
            # component's deliverable.
            outcome = Curtailed(
                Delivered(
                    result.partial,
                    backend.artifact_store.write_quarantined(result_key, result.partial),
                ) if result.partial is not None else None,
                result.detail,
            )
        else:
            outcome = Delivered(result, backend.artifact_store.write_artifact(result_key, result))

        # Persist registered artifacts on every path (fresh run or cache
        # replay), like ``write_artifact`` above — deliverables are rebuilt
        # from cache content, never assumed to survive on disk. A gave-up
        # component keeps whatever its tools registered before the surrender.
        persisted = [
            PersistedPluginArtifact(
                plugin=pa.plugin,
                artifact=pa.artifact,
                path=backend.artifact_store.write_plugin_artifact(
                    result_key, pa.plugin, pa.artifact
                ),
            )
            for pa in collected.items
        ]
        return ComponentOutcome(batch.feat, batch.props, outcome, persisted)

    settled = await asyncio.gather(*[_run(b) for b in batches], return_exceptions=True)
    outcomes = [o if isinstance(o, ComponentOutcome)
                else ComponentOutcome(b.feat, b.props, o)
                for b, o in zip(batches, settled)]

    await formalizer.finalize(outcomes, run)

    # 6. Report (shared, backend-agnostic). The driver assembles the per-component inputs; backends
    # contribute only synthetic extras (prover: structural invariants). Best-effort: a failure here
    # never fails the run.
    inputs = [
        ReportComponentInput(
            name=o.feat.display_name,
            props=o.props,
            formalized=o.result if isinstance(o.result, (Delivered, Curtailed)) else None,
        )
        for o in outcomes
    ] + formalizer.extra_report_inputs()
    artifact_records = [
        VerificationArtifactRecord(
            component=o.feat.display_name,
            plugin=pa.plugin,
            name=pa.artifact.name,
            kind=pa.artifact.kind,
            description=pa.artifact.description,
            path=str(pa.path),
        )
        for o in outcomes
        for pa in o.artifacts
    ]
    findings_evidence = formalizer.findings_evidence()
    try:
        async def _report() -> AutoProverReport:
            return await build_report(
                contract_name=source.contract_name, backend=formalizer.backend_tag,
                components=inputs, llm=run.env.llm_lite(), fetch_verdicts=formalizer.fetch_verdicts,
                source_edits=await formalizer.source_edits(outcomes, run),
                verification_artifacts=artifact_records,
                # Findings only when the backend supplies evidence — skip the heavy model otherwise.
                findings_llm=run.env.llm_heavy() if findings_evidence else None,
                fetch_evidence=findings_evidence,

            )
        report = await run.runner(
            job=_report,
            task_info=TaskInfo(REPORT_TASK_ID, label="Report Extraction", phase=backend.core_phases["report"])
        )
        backend.artifact_store.write_report(report)
    except Exception:
        if report_build.RERAISE_REPORT_FAILURES:
            raise
        _log.warning("report phase failed (continuing)", exc_info=True)

    return _tally(outcomes)

async def _extract_all[P: enum.Enum, H, Main, U: FeatureUnit](
    prop_key: str,
    main: Main, backend_guidance: str, run: PipelineRun[P, H],
    phase: P, interactive: bool, threat_model: Document | None,
    extra_context: Sequence[Document], max_rounds: int,
    # ``App`` stays ``Any`` here: this helper never touches the analyzed-model axis, only
    # ``Main``/``U`` (matching the caller's), so there's nothing to tie it to.
    ecosystem: Ecosystem[Any, Main, U],
    plugins: PluginPhaseManager[P, U],
) -> list[_Batch[U]]:
    prop_ctx = run.ctx.child(PROPERTIES_KEY(prop_key))

    async def _pre_plugin_inputs(feat: U) -> list[AnyPropertyGenerationInput]:
        async def run_one(runner: PluginPhaseRunner[P, U]) -> AnyPropertyGenerationInput | None:
            ctxt = await prop_ctx.child(
                PRE_PROPERTY_KEY(feat, runner.plugin_id), {"plugin-name": runner.plugin_id}
            )
            return await runner.plugin.property_inference_input_hook(
                feat, runner.bind(str(feat.unit_index), ctxt)
            )

        got = await asyncio.gather(*[
            run_one(r) for r in plugins.runners(
                sub_phase_id="pre-inference", sub_phase_label="Property Pre-Inference"
            )
        ])
        return [t for t in got if t is not None]

    async def _post_plugin_props(
        feat: U, props: list[PropertyFormulation]
    ) -> list[PropertyFormulation]:
        accum = props
        for runner in plugins.runners(
            sub_phase_id="post-inference", sub_phase_label="Property Post-Process", sorted_run=True
        ):
            ctxt = await prop_ctx.child(
                POST_PROPERTY_KEY(feat, runner.plugin_id, accum),
                {
                    "plugin-name": runner.plugin_id,
                    "props": [p.model_dump() for p in accum],
                },
            )
            accum = await runner.plugin.post_process_property_inference(
                feat, runner.bind(str(feat.unit_index), ctxt), accum
            )
        return accum

    async def _one(feat: U) -> _Batch[U] | None:
        pre_input = await _pre_plugin_inputs(feat)

        # Mirrors the natspec path's `system_doc` injection (composer/spec/natspec/pipeline.py):
        # the design doc, when supplied on the source/prover path too, should inform property
        # inference the same way it informs component analysis, not just the latter.
        design_doc = run.source.content
        if design_doc is not None:
            pre_input = [
                *pre_input,
                CacheablePropertyGenerationInput(
                    "certora:system-doc", "generic", "always",
                    lambda cache, doc=design_doc: [
                        "For reference, the system document describing the entire application is as follows.\n\n",
                        doc.to_dict(CacheLevel.SHORT if cache else CacheLevel.NONE)
                    ]
                ),
            ]

        feat_ctx = await prop_ctx.child(
            COMPONENT_KEY(feat, plugins.plugin_digest),
            {**feat.context_tag(), "plugins": plugins.plugin_manifest},
        )
        props = await run.runner(
            TaskInfo(extract_task_id(feat.unit_index), feat.display_name, phase),
            lambda conv: run_property_inference(
                feat_ctx, run.env, feat, refinement=conv if interactive else None,
                threat_model=threat_model, extra_context=extra_context,
                max_rounds=max_rounds, backend_guidance=backend_guidance,
                extra_input=pre_input,
                system_template=ecosystem.property_prompts.system,
                render_initial=ecosystem.property_prompts.render_initial,
            ),
        )
        if not props:
            return None

        accum = await _post_plugin_props(feat, props)
        return _Batch(feat, accum, feat_ctx) if accum else None

    async def budgeted_task(u: U) -> _Batch | None:
        with named_budget_or_nop("property_extraction"):
            return await _one(u)

    got = await asyncio.gather(*[budgeted_task(u) for u in ecosystem.units(main)])
    return [b for b in got if b is not None]


def _tally[FormT: BackendResult, U: FeatureUnit](
    outcomes: list[ComponentOutcome[FormT, U]]
) -> CorePipelineResult[FormT]:
    failures: list[str] = []
    for o in outcomes:
        if isinstance(o.result, BaseException):
            failures.append(f"{o.feat.display_name}: {o.result}")
        elif isinstance(o.result, GaveUp):
            failures.append(f"{o.feat.display_name}: GAVE_UP: {o.result.reason}")
        elif isinstance(o.result, Curtailed):
            what = (
                f"unvalidated partial kept at {o.result.partial.deliverable}"
                if o.result.partial is not None else "nothing published"
            )
            failures.append(
                f"{o.feat.display_name}: BUDGET: formalization cut short ({what})"
            )
    # The rollup is unit-agnostic; widen the concrete-unit outcomes to the protocol for storage.
    return CorePipelineResult(
        len(outcomes), sum(len(o.props) for o in outcomes),
        cast(list[ComponentOutcome[FormT, FeatureUnit]], outcomes), failures,
    )
