"""Backend-agnostic spec-generation spine.

Phase chain — each link is immutable and its existence proves the prior phase ran, so ordering is
a constructor dependency rather than a call-order convention; there is no half-initialized state:

    Backend ──prepare_system──▶ PreparedSystem ──prepare_formalization──▶ Formalizer
    (config, source)            (.main: structure)                        (formalize / persist / report)

A backend whose units all build on one *shared* artifact inserts a link: ``prepare_formalization`` returns 
a :class:`StagedFormalizer`, and its ``begin`` — handed every unit's properties — is what produces the 
:class:`Formalizer`. 

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
from typing import Protocol, Any, cast
from collections.abc import Sequence
from abc import ABC, abstractmethod


from composer.io.multi_job import TaskInfo
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import (
    WorkflowContext, CacheKey, Properties, ComponentGroup, SourceCode
)
from composer.spec.system_model import (
    BaseApplication, ContractComponentInstance, FeatureUnit
)
from composer.spec.types import PropertyFormulation, ArtifactIdentifier
from composer.spec.system_analysis import run_component_analysis
from composer.spec.prop_inference import run_property_inference, AnyPropertyGenerationInput
from composer.spec.util import string_hash
from composer.input.files import Document
from composer.spec.source.report.build import build_report
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import RuleName, ReportBackend
from composer.spec.source.report import build as report_build
from composer.spec.source.task_ids import SYSTEM_ANALYSIS_TASK_ID, REPORT_TASK_ID
from composer.pipeline.ecosystem import Ecosystem
from .ptypes import (
    BackendJob, BackendResult, ComponentOutcome, CorePhases, CorePipelineResult, Delivered, GaveUp,
    PipelineRun, SystemAnalysisSpec
)
from .plugin_api import PrePropertyInference, PostPropertyInference
from .plugins import load_plugins, PluginManager, PluginPhaseManager, PluginPhaseRunner

COMMON_SYSTEM_CACHE_KEY = "system-analysis"

_log = logging.getLogger(__name__)

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

    @abstractmethod
    async def formalize(
        self,
        label: str,
        feat: U,
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


class PipelineBackend[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier, U: FeatureUnit, Main, App: BaseApplication](Protocol):
    @property
    def backend_guidance(self) -> str: ...

    @property
    def analysis_spec(self) -> SystemAnalysisSpec: ...

    @property
    def core_phases(self) -> CorePhases[P]: ...

    @property
    def artifact_store(self) -> ArtifactStore[A, FormT]: ...

    async def prepare_system(
        self, analyzed: App,
        run: PipelineRun[P, H]
    ) -> PreparedSystem[FormT, U, Main]: ...

    def to_artifact_id(self, c: U) -> A: ...


# ---- shared helpers (the de-duplicated cache keys + batch) -------------------
def PROPERTIES_KEY(nm: str):
    return CacheKey[None, Properties](nm)


@dataclass
class _Batch[U: FeatureUnit](BackendJob[U]):
    feat_ctx: WorkflowContext[ComponentGroup]

def _component_digest(c: FeatureUnit) -> str:
    # ``cache_material`` is the ecosystem-agnostic view of what identifies a unit; EVM's
    # implementation reproduces the previous inline key (app JSON | ind | contract ind) exactly.
    return string_hash(c.cache_material())

def _component_cache_key(c: FeatureUnit, plugin_digest: str | None) -> CacheKey[Properties, ComponentGroup]:
    raw_digest = _component_digest(c)
    if plugin_digest is not None:
        raw_digest += f"-{plugin_digest}"
    return CacheKey(raw_digest)


def _batch_cache_key[FormT: BackendResult](
    props: list[PropertyFormulation],
) -> CacheKey[ComponentGroup, FormT]:  # pyright: ignore[reportInvalidTypeVarUse]
    return CacheKey(string_hash("|".join(p.model_dump_json() for p in props)))


def extract_task_id(idx: int) -> str:
    return f"extract-{idx}"


def formalize_task_id(idx: int) -> str:
    return f"formalize-{idx}"

async def run_pipeline[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier, U: FeatureUnit, Main, App: BaseApplication](
    backend: PipelineBackend[P, FormT, H, A, U, Main, App],
    run: PipelineRun[P, H],
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    max_bug_rounds: int = 3,
    ecosystem: Ecosystem[App, Main, U],
) -> CorePipelineResult[FormT]:
    # Only the plugins whose hooks accept this ecosystem's unit are loaded (and only those pay
    # their ``initialize`` cost); the driver below can hand them its units unconditionally.
    async with load_plugins(run, ecosystem.unit_type) as plugins:
        return await run_pipeline_inner(
            backend, run, plugins, interactive=interactive, threat_model=threat_model,
            max_bug_rounds=max_bug_rounds, ecosystem=ecosystem,
        )

# ---- the driver --------------------------------------------------------------
async def run_pipeline_inner[P: enum.Enum, FormT: BackendResult, H, A: ArtifactIdentifier, U: FeatureUnit, Main, App: BaseApplication](
    backend: PipelineBackend[P, FormT, H, A, U, Main, App],
    run: PipelineRun[P, H],
    plugin_manager: PluginManager[P, U],
    *,
    interactive: bool = False,
    threat_model: Document | None = None,
    max_bug_rounds: int = 3,
    ecosystem: Ecosystem[App, Main, U],
) -> CorePipelineResult[FormT]:
    spec, phases = backend.analysis_spec, backend.core_phases
    source = run.source

    assert ecosystem.supports_greenfield or run.env.sort != "greenfield", (
        f"ecosystem {ecosystem.name!r} has no greenfield prompts; got sort='greenfield'"
    )

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
    staged_task = asyncio.create_task(prepared.prepare_formalization(run))

    batches: list[_Batch[U]] = await _extract_all(
        backend.analysis_spec.properties_key,
        prepared.main,
        backend.backend_guidance,
        run,
        phases["extraction"],
        interactive,
        threat_model,
        max_bug_rounds,
        ecosystem,
        plugin_manager.bind_phase(
            phases.get("extraction_plugin") or phases["extraction"],
            "Property Extraction"
        )
    )
    staged = await staged_task
    if not batches:
        raise ValueError("No properties extracted from any component.")

    # 4. A backend whose units share an artifact handed back a ``StagedFormalizer`` instead of a
    #    formalizer: the artifact is authored HERE — once, from every unit's properties — and the
    #    formalizer it yields is the only one that exists (see :class:`StagedFormalizer`).
    formalizer = (
        await staged.begin(batches, run) if isinstance(staged, StagedFormalizer) else staged
    )

    # 5. Per-component formalization. Caching is core-owned, keyed by the backend's result type.
    async def _run(batch: _Batch[U]) -> ComponentOutcome[FormT, U]:
        result_key = backend.to_artifact_id(batch.feat)
        backend.artifact_store.write_properties(result_key, batch.props)
        child : WorkflowContext[FormT] = await batch.feat_ctx.child(
            _batch_cache_key(batch.props),
            {"properties": [p.model_dump() for p in batch.props]},
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

    # 6. Report (shared, backend-agnostic). The driver assembles the per-component inputs; backends
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

def _pre_property_cache_key(feat: FeatureUnit, plugin: str) -> CacheKey[Properties, PrePropertyInference]:
    key = f"{_component_digest(feat)}-{string_hash(plugin)}-pre"
    return CacheKey(key)

def _post_property_cache_key(feat: FeatureUnit, plugin: str, curr_props: list[PropertyFormulation]) -> CacheKey[Properties, PostPropertyInference]:
    props = string_hash("|".join(
        p.model_dump_json() for p in curr_props
    ))
    key = f"{_component_digest(feat)}-{string_hash(plugin)}-{props}"
    return CacheKey(key)

async def _extract_all[P: enum.Enum, H, Main, U: FeatureUnit](
    prop_key: str,
    main: Main, backend_guidance: str, run: PipelineRun[P, H],
    phase: P, interactive: bool, threat_model: Document | None, max_rounds: int,
    # ``App`` stays ``Any`` here: this helper never touches the analyzed-model axis, only
    # ``Main``/``U`` (matching the caller's), so there's nothing to tie it to.
    ecosystem: Ecosystem[Any, Main, U],
    plugins: PluginPhaseManager[P, U],
) -> list[_Batch[U]]:
    prop_ctx = run.ctx.child(PROPERTIES_KEY(prop_key))

    async def _pre_plugin_inputs(feat: U) -> list[AnyPropertyGenerationInput]:
        async def run_one(runner: PluginPhaseRunner[P, U]) -> AnyPropertyGenerationInput | None:
            ctxt = await prop_ctx.child(
                _pre_property_cache_key(feat, runner.plugin_id), {"plugin-name": runner.plugin_id}
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
                _post_property_cache_key(feat, runner.plugin_id, accum),
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

        feat_ctx = await prop_ctx.child(
            _component_cache_key(feat, plugins.plugin_digest),
            {**feat.context_tag(), "plugins": plugins.plugin_manifest},
        )
        props = await run.runner(
            TaskInfo(extract_task_id(feat.unit_index), feat.display_name, phase),
            lambda conv: run_property_inference(
                feat_ctx, run.env, feat, refinement=conv if interactive else None,
                threat_model=threat_model, max_rounds=max_rounds, backend_guidance=backend_guidance,
                extra_input=pre_input,
                system_template=ecosystem.property_prompts.system,
                render_initial=ecosystem.property_prompts.render_initial,
            ),
        )
        if not props:
            return None

        accum = await _post_plugin_props(feat, props)
        return _Batch(feat, accum, feat_ctx) if accum else None

    got = await asyncio.gather(*[_one(u) for u in ecosystem.units(main)])
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
    # The rollup is unit-agnostic; widen the concrete-unit outcomes to the protocol for storage.
    return CorePipelineResult(
        len(outcomes), sum(len(o.props) for o in outcomes),
        cast(list[ComponentOutcome[FormT, FeatureUnit]], outcomes), failures,
    )
