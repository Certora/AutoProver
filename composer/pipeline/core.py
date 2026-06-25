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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Callable, Awaitable, TypedDict
from abc import ABC, abstractmethod

from pydantic import BaseModel

from composer.io.multi_job import HandlerFactory, TaskInfo, run_task, ConversationContextProvider
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import (
    WorkflowContext, CacheKey, Properties, ComponentGroup, SourceCode,
)
from composer.spec.service_host import ServiceHost
from composer.spec.system_model import (
    SourceApplication, ContractInstance, ContractComponentInstance
)
from composer.spec.types import PropertyFormulation, FormalResult, ArtifactIdentifier
from composer.spec.system_analysis import run_component_analysis
from composer.spec.prop_inference import run_property_inference
from composer.spec.util import string_hash
from composer.input.files import Document
from composer.spec.source.report.build import build_report
from composer.spec.source.report.collect import ReportableResult, ReportComponentInput, Verdict, Formalized
from composer.spec.source.report.schema import RuleName, ReportBackend

_log = logging.getLogger(__name__)

class BackendResult(FormalResult, ReportableResult, Protocol):
    ...


class GaveUp(BaseModel):
    """The single, unified give-up signal (replaces the two structurally-identical copies in
    spec.source.author and foundry.author)."""
    reason: str


# ---- run-scoped shared infra, handed to every hook ---------------------------
@dataclass
class PipelineRun[P: enum.Enum, H]:
    ctx: WorkflowContext[None]
    env: ServiceHost
    source: SourceCode
    _handler_factory: HandlerFactory[P, H]
    _semaphore: asyncio.Semaphore

    async def runner[T](
        self,
        task_info: TaskInfo[P],
        job: Callable[[], Awaitable[T]] | Callable[[ConversationContextProvider], Awaitable[T]],
    ) -> T:
        return await run_task(
            factory=self._handler_factory,
            fn=job,
            info=task_info,
            semaphore=self._semaphore
        )


class CorePhases[P: enum.Enum](TypedDict):
    """The backend maps its own phase enum onto the three core phases the driver tags."""
    analysis: P
    extraction: P
    formalization: P


@dataclass(frozen=True)
class SystemAnalysisSpec:
    """The backend's contribution to the shared analysis call. The analyzed type is always
    SourceApplication (the prover's harnessed lift is its prepare_system, not analysis)."""
    analysis_key: str
    extra_input: list[str | dict] = field(default_factory=list)


@dataclass
class BackendJob:
    feat: ContractComponentInstance
    props: list[PropertyFormulation]

@dataclass(frozen=True)
class Delivered[FormT: BackendResult]:
    """A successful formalization and the project-relative path it was persisted to. The path exists
    only because the result does, so the two travel together rather than as independent fields."""
    result: FormT
    deliverable: Path

    def to_formalized(self, with_link: str | None = None):
        return Formalized(
            result=self.result,
            unit_file=str(self.deliverable),
            run_link=with_link
        )

@dataclass
class ComponentOutcome[FormT: BackendResult](BackendJob):
    result: Delivered[FormT] | GaveUp | BaseException

@dataclass
class CorePipelineResult[FormT: BackendResult]:
    n_components: int
    n_properties: int
    outcomes: list[ComponentOutcome[FormT]]
    failures: list[str]

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
    ) -> FormT | GaveUp: ...

    @abstractmethod
    def report_inputs(self, outcomes: list[ComponentOutcome[FormT]]) -> list[ReportComponentInput[FormT]]:
        """Map outcomes → report inputs: derive unit_file + run_link per component and fold in any
        synthetic components (prover: 'Structural Invariants'; foundry: none). Gave-up / crashed
        outcomes map to formalized=None."""
        ...
    
    @abstractmethod
    async def fetch_verdicts(self, inp: ReportComponentInput[FormT]) -> dict[RuleName, Verdict]:
        """Per-unit outcomes. Prover: query ProverOutputUtility via inp.formalized.run_link
        off-thread. Foundry: read straight off inp.formalized.result."""
        ...

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
PROPERTIES_KEY = CacheKey[None, Properties]("properties")


def main_instance(app: SourceApplication, source: SourceCode) -> ContractInstance:
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

def _component_cache_key(c: ContractComponentInstance) -> CacheKey[Properties, ComponentGroup]:
    return CacheKey(string_hash("|".join([c.app.model_dump_json(), str(c.ind), str(c._contract.ind)])))


def _batch_cache_key[FormT: BaseModel](props: list[PropertyFormulation]) -> CacheKey[ComponentGroup, FormT]:
    return CacheKey(string_hash("|".join(p.model_dump_json() for p in props)))

# ---- the driver --------------------------------------------------------------
async def run_pipeline[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier](
    backend: PipelineBackend[P, FormT, H, A],
    run: PipelineRun[P, H],
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    max_bug_rounds: int = 3,
) -> CorePipelineResult[FormT]:
    spec, phases = backend.analysis_spec, backend.core_phases
    source = run.source

    # 1. System analysis (shared primitive, backend-parameterized; always yields SourceApplication).
    analyzed = await run.runner(
        TaskInfo("system-analysis", "System Analysis", phases["analysis"]),
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
    prepared = await backend.prepare_system(analyzed, run)

    # 3. Pre-formalization setup runs CONCURRENTLY with extraction (neither needs the other) —
    #    this preserves the prover's autosetup ∥ bug-analysis overlap, generically.
    formalizer_task = asyncio.create_task(prepared.prepare_formalization(run))

    batches = await _extract_all(prepared.main, backend.backend_guidance, run,
                                phases["extraction"], interactive, threat_model, max_bug_rounds)
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
        result : FormT | GaveUp
        if cached_result is None:
            label = f"{batch.feat.component.name} ({len(batch.props)} properties)"
            result : FormT | GaveUp = await run.runner(
                TaskInfo(
                    f"formalize-{batch.feat.ind}",
                    f"{batch.feat.component.name} ({len(batch.props)} properties)",
                    phases["formalization"]
                ),
                lambda: formalizer.formalize(label, batch.feat, batch.props, child, run),
            )
            if not isinstance(result, GaveUp):
                await child.cache_put(result)
        else:
            result = cached_result
        
        outcome: Delivered[FormT] | GaveUp = (
            result if isinstance(result, GaveUp)
            else Delivered(result, backend.artifact_store.write_artifact(result_key, result))
        )
        return ComponentOutcome(batch.feat, batch.props, outcome)

    settled = await asyncio.gather(*[_run(b) for b in batches], return_exceptions=True)
    outcomes = [o if isinstance(o, ComponentOutcome)
                else ComponentOutcome(b.feat, b.props, o)
                for b, o in zip(batches, settled)]

    # 5. Report (shared, backend-agnostic). Best-effort: a failure here never fails the run.
    inputs = formalizer.report_inputs(outcomes)
    try:
        report = await build_report(
            contract_name=source.contract_name, backend=formalizer.backend_tag,
            components=inputs, llm=run.env.llm_lite(), fetch_verdicts=formalizer.fetch_verdicts,
        )
        backend.artifact_store.write_report(report)
    except Exception:
        _log.warning("report phase failed (continuing)", exc_info=True)

    return _tally(outcomes)

async def _extract_all[P: enum.Enum, H](
    main: ContractInstance, backend_guidance: str, run: PipelineRun[P, H],
    phase: P, interactive: bool, threat_model: Document | None, max_rounds: int,
) -> list[_Batch]:
    prop_ctx = run.ctx.child(PROPERTIES_KEY)

    async def _one(idx: int) -> _Batch | None:
        feat = ContractComponentInstance(_contract=main, ind=idx)
        feat_ctx = await prop_ctx.child(_component_cache_key(feat),
                                        {"component": feat.component.model_dump()})
        props = await run.runner(
            TaskInfo(f"extract-{idx}", feat.component.name, phase),
            lambda conv: run_property_inference(
                feat_ctx, run.env, feat, refinement=conv if interactive else None,
                threat_model=threat_model, max_rounds=max_rounds, backend_guidance=backend_guidance),
        )
        return _Batch(feat, props, feat_ctx) if props else None

    got = await asyncio.gather(*[_one(i) for i in range(len(main.contract.components))])
    return [b for b in got if b is not None]


def _tally[FormT: BackendResult](outcomes: list[ComponentOutcome[FormT]]) -> CorePipelineResult[FormT]:
    failures: list[str] = []
    for o in outcomes:
        if isinstance(o.result, BaseException):
            failures.append(f"{o.feat.component.name}: {o.result}")
        elif isinstance(o.result, GaveUp):
            failures.append(f"{o.feat.component.name}: GAVE_UP: {o.result.reason}")
    return CorePipelineResult(len(outcomes), sum(len(o.props) for o in outcomes), outcomes, failures)
