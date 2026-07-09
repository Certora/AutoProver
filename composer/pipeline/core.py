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
from typing import Protocol, Any
from abc import ABC, abstractmethod

from pydantic import BaseModel

from composer.io.multi_job import TaskInfo
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import (
    WorkflowContext, CacheKey, Properties, ComponentGroup, SourceCode
)
from composer.spec.system_model import (
    SourceApplication, ContractInstance, ContractComponentInstance, AnyApplication, FeatureUnit
)
from composer.spec.types import PropertyFormulation, ArtifactIdentifier
from composer.spec.system_analysis import run_component_analysis
from composer.spec.prop_inference import run_property_inference
from composer.spec.util import string_hash
from composer.input.files import Document
from composer.spec.source.report.build import build_report
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import RuleName, ReportBackend
from composer.spec.source.report import build as report_build
from composer.spec.source.task_ids import SYSTEM_ANALYSIS_TASK_ID, REPORT_TASK_ID
# The ecosystem seam supplies the domain-specific front half (analyzed model type, prompts,
# analysis validation, unit enumeration). ``main_instance`` moved here too and is re-exported
# so existing EVM backends keep doing ``from composer.pipeline.core import main_instance``.
from composer.pipeline.ecosystem import Ecosystem, EVM, main_instance
from .ptypes import (
    BackendJob, BackendResult, ComponentOutcome, CorePhases, CorePipelineResult, Delivered, GaveUp, PipelineRun, SystemAnalysisSpec
)

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
    ) -> FormT | GaveUp: ...

    def extra_report_inputs(self) -> list[ReportComponentInput[FormT]]:
        """Synthetic report inputs beyond the per-component outcomes — the prover folds in its
        'Structural Invariants' here. Default: none."""
        return []

    @abstractmethod
    async def fetch_verdicts(self, inp: ReportComponentInput[FormT]) -> dict[RuleName, Verdict]:
        """Per-unit outcomes. Prover: query ProverOutputUtility via inp.formalized.run_link
        off-thread. Foundry: read straight off inp.formalized.result."""
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
PROPERTIES_KEY = CacheKey[None, Properties]("properties")


@dataclass
class _Batch(BackendJob):
    feat_ctx: WorkflowContext[ComponentGroup]

def _component_cache_key(c: FeatureUnit) -> CacheKey[Properties, ComponentGroup]:
    # ``cache_material`` is the ecosystem-agnostic view of what identifies a unit; EVM's
    # implementation reproduces the previous inline key (app JSON | ind | contract ind) exactly.
    return CacheKey(string_hash(c.cache_material()))


def _batch_cache_key[FormT: BaseModel](props: list[PropertyFormulation]) -> CacheKey[ComponentGroup, FormT]:
    return CacheKey(string_hash("|".join(p.model_dump_json() for p in props)))


def extract_task_id(idx: int) -> str:
    return f"extract-{idx}"


def formalize_task_id(idx: int) -> str:
    return f"formalize-{idx}"

# ---- the driver --------------------------------------------------------------
async def run_pipeline[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier](
    backend: PipelineBackend[P, FormT, H, A],
    run: PipelineRun[P, H],
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    max_bug_rounds: int = 3,
    ecosystem: Ecosystem = EVM,
) -> CorePipelineResult[FormT]:
    # ``ecosystem`` supplies the domain-specific front half; it defaults to ``EVM``, which
    # reproduces the previous hardcoded Solidity behavior exactly, so EVM callers (and cli.py)
    # need pass nothing. Non-EVM backends (e.g. the Rust/Crucible backend) pass ``ecosystem=SOLANA``.
    spec, phases = backend.analysis_spec, backend.core_phases
    source = run.source

    # 1. System analysis (shared primitive; the ecosystem supplies the analyzed model type,
    #    prompts, validation, and front-matter — EVM reproduces prior behavior exactly).
    analyzed = await run.runner(
        TaskInfo(SYSTEM_ANALYSIS_TASK_ID, "System Analysis", phases["analysis"]),
        lambda: run_component_analysis(
            ty=ecosystem.system_model, child_ctxt=run.ctx.child(CacheKey(spec.analysis_key)),
            input=source, env=run.env,
            extra_input=[*ecosystem.analysis_extra_input(source), *spec.extra_input],
            expected_main_id=source.contract_name,
            system_template=ecosystem.analysis_prompts.system,
            initial_template=ecosystem.analysis_prompts.initial,
            validate=ecosystem.validate_analysis,
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
                                phases["extraction"], interactive, threat_model, max_bug_rounds,
                                ecosystem)
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
            label = f"{batch.feat.display_name} ({len(batch.props)} properties)"
            result : FormT | GaveUp = await run.runner(
                TaskInfo(
                    formalize_task_id(batch.feat.unit_index),
                    label,
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

    await formalizer.finalize(outcomes, run)

    # 5. Report (shared, backend-agnostic). The driver assembles the per-component inputs; backends
    # contribute only synthetic extras (prover: structural invariants). Best-effort: a failure here
    # never fails the run.
    inputs = [
        ReportComponentInput(
            name=o.feat.display_name,
            props=o.props,
            formalized=o.result if isinstance(o.result, Delivered) else None,
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

async def _extract_all[P: enum.Enum, H](
    main: Any, backend_guidance: str, run: PipelineRun[P, H],
    phase: P, interactive: bool, threat_model: Document | None, max_rounds: int,
    ecosystem: Ecosystem,
) -> list[_Batch]:
    prop_ctx = run.ctx.child(PROPERTIES_KEY)

    async def _one(feat: FeatureUnit) -> _Batch | None:
        feat_ctx = await prop_ctx.child(_component_cache_key(feat), feat.context_tag())
        props = await run.runner(
            TaskInfo(extract_task_id(feat.unit_index), feat.display_name, phase),
            lambda conv: run_property_inference(
                feat_ctx, run.env, feat, refinement=conv if interactive else None,
                threat_model=threat_model, max_rounds=max_rounds, backend_guidance=backend_guidance,
                system_template=ecosystem.property_prompts.system,
                initial_template=ecosystem.property_prompts.initial),
        )
        return _Batch(feat, props, feat_ctx) if props else None

    got = await asyncio.gather(*[_one(u) for u in ecosystem.units(main)])
    return [b for b in got if b is not None]


def _tally[FormT: BackendResult](outcomes: list[ComponentOutcome[FormT]]) -> CorePipelineResult[FormT]:
    failures: list[str] = []
    for o in outcomes:
        if isinstance(o.result, BaseException):
            failures.append(f"{o.feat.display_name}: {o.result}")
        elif isinstance(o.result, GaveUp):
            failures.append(f"{o.feat.display_name}: GAVE_UP: {o.result.reason}")
    return CorePipelineResult(len(outcomes), sum(len(o.props) for o in outcomes), outcomes, failures)
