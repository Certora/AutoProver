"""Backend-agnostic spec-generation spine.

Phase chain — each link is immutable and its existence proves the prior phase ran, so ordering is
a constructor dependency rather than a call-order convention; there is no half-initialized state:

    Backend ──prepare_system──▶ PreparedSystem ──prepare_formalization──▶ Formalizer
    (config, source)            (.main: structure)                        (formalize / persist / report)

The driver owns the genuinely-shared steps: system analysis, per-component property extraction, the
result-type-keyed cache, and (since the report is backend-agnostic) building + persisting the
property-keyed report. Everything backend-specific — the harnessed lift, autosetup/summaries/
invariant fan-out, the formalizer itself, per-unit verdicts — is contributed through the three
phase objects, and never inspected by the driver.
"""

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Protocol, cast, ContextManager
from abc import ABC, abstractmethod
from contextlib import nullcontext

from pydantic import BaseModel

from composer.io.multi_job import TaskInfo
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import (
    WorkflowContext, CacheKey, Properties, ComponentGroup, SourceCode
)
from composer.spec.system_model import (
    SourceApplication, ContractInstance, ContractComponentInstance, AnyApplication
)
from composer.spec.types import PropertyFormulation, ArtifactIdentifier
from composer.spec.system_analysis import run_component_analysis
from composer.spec.prop_inference import run_property_inference, AnyPropertyGenerationInput
from composer.spec.util import string_hash
from composer.input.files import Document
from composer.spec.source.report.build import build_report
from composer.spec.source.report.collect import Formalized, ReportComponentInput, Verdict
from composer.spec.source.report.schema import RuleName, ReportBackend
from composer.spec.source.report import build as report_build
from composer.spec.source.task_ids import SYSTEM_ANALYSIS_TASK_ID, REPORT_TASK_ID
from .ptypes import (
    BackendJob, BackendResult, ComponentOutcome, CorePhases, CorePipelineResult,
    Curtailed, Delivered, GaveUp, PipelineRun, SystemAnalysisSpec, RunBudget
)
from composer.diagnostics.budget import total_budget, named_budget_or_nop, time_budget
from .plugin_api import PrePropertyInference, PostPropertyInference
from .plugins import load_plugins, PluginManager, PluginPhaseManager, PluginPhaseRunner

COMMON_SYSTEM_CACHE_KEY = "system-analysis"

_log = logging.getLogger(__name__)

@dataclass
class Formalizer[FormT: BackendResult](ABC):
    """Immutable, fully constructed by prepare_formalization. Carries the prover's
    config/resources/prover_tool/invariant-results (or nothing, for foundry) as constructor
    state — never set post-hoc. `FormT: ReportableResult` is what makes the report a core step."""
    formalized_type: type[FormT]
    backend_tag: ReportBackend

    @abstractmethod
    async def formalize(
        self,
        label: str,
        feat: ContractComponentInstance,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[FormT],
        run: PipelineRun
    ) -> FormT | Curtailed[FormT] | GaveUp:
        """A full result, a ``Curtailed`` wrapper when the budget cut the author short (its
        ``partial`` is whatever was published under the lifted gates), or a ``GaveUp``."""
        ...

    def extra_report_inputs(self) -> list[ReportComponentInput[FormT]]:
        """Synthetic report inputs beyond the per-component outcomes — the prover folds in its
        'Structural Invariants' here. Default: none."""
        return []

    @abstractmethod
    async def fetch_verdicts(self, formalized: Formalized[FormT]) -> dict[RuleName, Verdict]:
        """Per-unit outcomes for one delivered result. Prover: query ProverOutputUtility via
        ``formalized.run_link`` off-thread. Foundry: read straight off ``formalized.result``.
        Never called for gave-up or budget-curtailed components."""
        ...

    async def finalize(self, outcomes: list[ComponentOutcome[FormT]], run: PipelineRun) -> None:
        """Emit any backend-specific run-level artifacts from the full outcome set (prover:
        components_to_prover_runs.json). Default: none."""
        return None

@dataclass
class PreparedSystem[FormT: BackendResult](ABC):
    main: ContractInstance

    @abstractmethod
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[FormT]: ...


class PipelineBackend[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier](Protocol):
    @property
    def backend_guidance(self) -> str: ...

    @property
    def analysis_spec(self) -> SystemAnalysisSpec: ...

    @property
    def core_phases(self) -> CorePhases[P]: ...

    @property
    def artifact_store(self) -> ArtifactStore[A, FormT]: ...

    async def prepare_system(
        self, analyzed: SourceApplication,
        run: PipelineRun[P, H]
    ) -> PreparedSystem[FormT]: ...

    def to_artifact_id(self, c: ContractComponentInstance) -> A: ...


# ---- shared helpers (the de-duplicated cache keys + batch) -------------------
def PROPERTIES_KEY(nm: str):
    return CacheKey[None, Properties](nm)


def main_instance(app: AnyApplication, source: SourceCode) -> ContractInstance:
    """Locate the application's main contract — the one whose solidity identifier matches
    ``source.contract_name`` — and return a ``ContractInstance`` pointing at it. Backends call this
    from ``prepare_system`` to seed the per-component loop; component analysis should already have
    guaranteed the contract is present (via ``expected_main_id``)."""
    for i, c in enumerate(app.contract_components):
        if c.solidity_identifier == source.contract_name:
            return ContractInstance(i, app)
    raise ValueError(f"main contract {source.contract_name!r} not found in analyzed application")


@dataclass
class _Batch(BackendJob):
    feat_ctx: WorkflowContext[ComponentGroup]

def _component_digest(c: ContractComponentInstance) -> str:
    return string_hash("|".join([c.app.model_dump_json(), str(c.ind), str(c._contract.ind)]))

def _component_cache_key(c: ContractComponentInstance, plugin_digest: str | None) -> CacheKey[Properties, ComponentGroup]:
    raw_digest = _component_digest(c)
    if plugin_digest is not None:
        raw_digest += f"-{plugin_digest}"
    return CacheKey(raw_digest)


def _batch_cache_key[FormT: BaseModel](props: list[PropertyFormulation]) -> CacheKey[ComponentGroup, FormT]:
    return CacheKey(string_hash("|".join(p.model_dump_json() for p in props)))


def extract_task_id(idx: int) -> str:
    return f"extract-{idx}"


def formalize_task_id(idx: int) -> str:
    return f"formalize-{idx}"

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

async def run_pipeline[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier](
    backend: PipelineBackend[P, FormT, H, A],
    run: PipelineRun[P, H],
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    max_bug_rounds: int = 3,
    budget: RunBudget | None = None,
    time_budget_s : float | None = None
) -> CorePipelineResult[FormT]:
    with (
        _budget_context(budget),
        _time_context(time_budget_s)
    ):
        return await _run_pipeline_inner(
            backend, run, interactive=interactive, 
            max_bug_rounds=max_bug_rounds, threat_model=threat_model
        )

async def _run_pipeline_inner[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier](
    backend: PipelineBackend[P, FormT, H, A],
    run: PipelineRun[P, H],
    *,
    interactive: bool,
    threat_model: Document | None,
    max_bug_rounds: int
) -> CorePipelineResult[FormT]:
    async with load_plugins(run) as plugins:
        return await run_pipeline_inner(
            backend, run, plugins, interactive=interactive, threat_model=threat_model, max_bug_rounds=max_bug_rounds
        )

# ---- the driver --------------------------------------------------------------
async def run_pipeline_inner[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier](
    backend: PipelineBackend[P, FormT, H, A],
    run: PipelineRun[P, H],
    plugin_manager: PluginManager,
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    max_bug_rounds: int = 3,
) -> CorePipelineResult[FormT]:
    spec, phases = backend.analysis_spec, backend.core_phases
    source = run.source

    # 1. System analysis (shared primitive, backend-parameterized; always yields SourceApplication).
    with named_budget_or_nop("system_analysis"):
        analyzed = await run.runner(
            TaskInfo(SYSTEM_ANALYSIS_TASK_ID, "System Analysis", phases["analysis"]),
            lambda: run_component_analysis(
                ty=SourceApplication, child_ctxt=run.ctx.child(CacheKey(spec.analysis_key)),
                input=source, env=run.env, extra_input=[
                    f"The main entry point of this application has been explicitly identified as {source.contract_name} at relative path {source.relative_path}. "
                    "Your output MUST contain an explicit contract instance with this solidity identifier.",
                    *spec.extra_input
                ],
                expected_main_id=source.contract_name,
            ),
        )
    if analyzed is None:
        raise ValueError("System analysis produced no result.")
    
    # 2. Backend transform + main-contract location (prover: harness lift; foundry: identity).
    with named_budget_or_nop("system_preparation"):
        prepared = await backend.prepare_system(analyzed, run)

    # 3. Pre-formalization setup runs CONCURRENTLY with extraction (neither needs the other) —
    #    this preserves the prover's autosetup ∥ bug-analysis overlap, generically.
    #    The budget scope is entered inside the coroutine (not around create_task) so the
    #    cost-center binding lives in the spawned task's own context.
    async def _prepare_formalization() -> Formalizer[FormT]:
        with named_budget_or_nop("formalization_preparation"):
            return await prepared.prepare_formalization(run)
    formalizer_task = asyncio.create_task(_prepare_formalization())

    batches = await _extract_all(
        backend.analysis_spec.properties_key,
        prepared.main,
        backend.backend_guidance,
        run,
        phases["extraction"],
        interactive,
        threat_model,
        max_bug_rounds,
        plugin_manager.bind_phase(
            backend.core_phases.get("extraction_plugin") or backend.core_phases["extraction"],
            "Property Extraction"
        )
    )
    formalizer = await formalizer_task
    if not batches:
        raise ValueError("No properties extracted from any component.")

    # 4. Per-component formalization. Caching is core-owned, keyed by the backend's result type.
    async def _run(batch: _Batch) -> ComponentOutcome[FormT]:
        result_key = backend.to_artifact_id(batch.feat)
        backend.artifact_store.write_properties(result_key, batch.props)
        child : WorkflowContext[FormT] = await batch.feat_ctx.child(
            _batch_cache_key(batch.props), {"properties": [p.model_dump() for p in batch.props]},
        )
        cached_result: FormT | None = await child.cache_get(formalizer.formalized_type)
        result : FormT | Curtailed[FormT] | GaveUp
        if cached_result is None:
            label = f"{batch.feat.component.name} ({len(batch.props)} properties)"
            with named_budget_or_nop("formalization"):
                result = await run.runner(
                    TaskInfo(
                        formalize_task_id(batch.feat.ind),
                        f"{batch.feat.component.name} ({len(batch.props)} properties)",
                        phases["formalization"]
                    ),
                    lambda: formalizer.formalize(label, batch.feat, batch.props, child, run),
                )
            # Only a full result is cacheable: a curtailed partial is exactly what a future,
            # better-funded run must redo.
            if isinstance(result, formalizer.formalized_type):
                await child.cache_put(result)
        else:
            result = cached_result

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
        return ComponentOutcome(batch.feat, batch.props, outcome)

    settled = await asyncio.gather(*[_run(b) for b in batches], return_exceptions=True)
    outcomes = [o if isinstance(o, ComponentOutcome)
                else ComponentOutcome(b.feat, b.props, o)
                for b, o in zip(batches, settled)]

    await formalizer.finalize(outcomes, run)

    # 5. Report (shared, backend-agnostic). The driver assembles the per-component inputs; backends
    # contribute only synthetic extras (prover: structural invariants). Delivered and curtailed
    # results both flow through — the report grounds the former and appendixes the latter — while
    # give-ups and crashes are handed over as None. Best-effort: a failure here never fails the run.
    inputs = [
        ReportComponentInput(
            name=o.feat.component.name,
            props=o.props,
            formalized=o.result if isinstance(o.result, (Delivered, Curtailed)) else None,
        )
        for o in outcomes
    ] + formalizer.extra_report_inputs()
    try:
        report = await run.runner(
            job=lambda: build_report(
                contract_name=source.contract_name, backend=formalizer.backend_tag,
                components=inputs, llm=run.env.llm_lite(), fetch_verdicts=formalizer.fetch_verdicts,
            ),
            task_info=TaskInfo(REPORT_TASK_ID, label="Report Extraction", phase=backend.core_phases["report"])
        )
        backend.artifact_store.write_report(report)
    except Exception:
        if report_build.RERAISE_REPORT_FAILURES:
            raise
        _log.warning("report phase failed (continuing)", exc_info=True)

    return _tally(outcomes)

def _pre_property_cache_key(feat: ContractComponentInstance, plugin: str) -> CacheKey[Properties, PrePropertyInference]:
    key = f"{_component_digest(feat)}-{string_hash(plugin)}-pre"
    return CacheKey(key)

def _post_property_cache_key(feat: ContractComponentInstance, plugin: str, curr_props: list[PropertyFormulation]) -> CacheKey[Properties, PostPropertyInference]:
    props = string_hash("|".join(
        p.model_dump_json() for p in curr_props
    ))
    key = f"{_component_digest(feat)}-{string_hash(plugin)}-{props}"
    return CacheKey(key)

async def _extract_all[P: enum.Enum, H](
    prop_key: str,
    main: ContractInstance, backend_guidance: str, run: PipelineRun[P, H],
    phase: P, interactive: bool, threat_model: Document | None, max_rounds: int,
    plugins: PluginPhaseManager[P]
) -> list[_Batch]:
    prop_ctx = run.ctx.child(PROPERTIES_KEY(prop_key))

    async def _one(idx: int) -> _Batch | None:
        feat = ContractComponentInstance(_contract=main, ind=idx)
        async def run_plugin_pre(runner: PluginPhaseRunner[P]) -> AnyPropertyGenerationInput | None:
            p = runner.plugin_id
            ctxt = await prop_ctx.child(_pre_property_cache_key(feat, p), {
                "plugin-name": p
            })
            run_ctxt = runner.bind(str(idx), ctxt)
            return await runner.plugin.property_inference_input_hook(
                feat, run_ctxt
            )

        pre_process = await asyncio.gather(*[
            run_plugin_pre(plug_runner) for plug_runner in plugins.runners(
                sub_phase_id="pre-inference", sub_phase_label="Property Pre-Inference"
            )
        ])

        feat_ctx = await prop_ctx.child(_component_cache_key(feat, plugins.plugin_digest),
                                {"component": feat.component.model_dump(), "plugins": plugins.plugin_manifest})

        props = await run.runner(
            TaskInfo(extract_task_id(idx), feat.component.name, phase),
            lambda conv: run_property_inference(
                feat_ctx, run.env, feat, refinement=conv if interactive else None,
                threat_model=threat_model, max_rounds=max_rounds, backend_guidance=backend_guidance,
                extra_input=[ t for t in pre_process if t ]
            ),
        )
        if not props:
            return None
        
        async def run_plugin_post(
            runner: PluginPhaseRunner, props: list[PropertyFormulation]
        ) -> list[PropertyFormulation]:
            p = runner.plugin_id
            ctxt = await prop_ctx.child(
                _post_property_cache_key(feat, p, props),
                {
                    "plugin-name": p,
                    "props": [p.model_dump() for p in props]
                }
            )
            run_ctxt = runner.bind(str(idx), ctxt)
            return await runner.plugin.post_process_property_inference(
                feat, run_ctxt, props
            )
        accum = props
        for runner in plugins.runners(
            sub_phase_id="post-inference", sub_phase_label="Property Post-Process", sorted_run=True
        ):
            accum = await run_plugin_post(
                runner, accum
            )

        return _Batch(feat, accum, feat_ctx) if accum else None

    async def budgeted_task(idx: int) -> _Batch | None:
        with named_budget_or_nop("property_extraction"):
            return await _one(idx)

    got = await asyncio.gather(*[budgeted_task(i) for i in range(len(main.contract.components))])
    return [b for b in got if b is not None]


def _tally[FormT: BackendResult](outcomes: list[ComponentOutcome[FormT]]) -> CorePipelineResult[FormT]:
    failures: list[str] = []
    for o in outcomes:
        if isinstance(o.result, BaseException):
            failures.append(f"{o.feat.component.name}: {o.result}")
        elif isinstance(o.result, GaveUp):
            failures.append(f"{o.feat.component.name}: GAVE_UP: {o.result.reason}")
        elif isinstance(o.result, Curtailed):
            what = (
                f"unvalidated partial kept at {o.result.partial.deliverable}"
                if o.result.partial is not None else "nothing published"
            )
            failures.append(
                f"{o.feat.component.name}: BUDGET: formalization cut short ({what})"
            )
    return CorePipelineResult(len(outcomes), sum(len(o.props) for o in outcomes), outcomes, failures)
