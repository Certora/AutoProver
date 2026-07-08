"""Auto-prove backend for the generic pipeline (``composer.pipeline.core``).

The shared driver owns system analysis, per-component property extraction, the
result-type-keyed cache, and the report. This module contributes only the
prover-specific pieces as the three phase objects:

* ``ProverBackend.prepare_system`` — harness creation, then the lift of the
  analyzed ``SourceApplication`` into a ``HarnessedApplication`` and the prover
  tool. Returns a ``ProverPrepared``.
* ``ProverPrepared.prepare_formalization`` — the AutoSetup ∥ custom-summaries ∥
  structural-invariant fan-out, then the staged structural-invariant CVL whose
  ``invariants.spec`` is folded into the resources every per-component spec then
  imports. Returns a ``ProverRunner``.
* ``ProverRunner`` — per-batch CVL generation (``batch_cvl_generation``), the
  report inputs (per component + the synthetic ``Structural Invariants``), and
  prover-run-backed verdicts (``make_prover_fetcher``).

``run_autoprove_pipeline`` is now a thin wrapper that builds the backend + run
context and hands them to ``run_pipeline``.
"""

import asyncio
from dataclasses import dataclass
from typing import override

from langchain_core.tools import BaseTool

from composer.io.multi_job import TaskInfo
from composer.spec.context import WorkflowContext, CacheKey, CVLGeneration
from composer.spec.types import PropertyFormulation
from composer.spec.gen_types import CVLResource, SPECS_DIR, certora_relative_to_project
from composer.spec.system_model import (
    ContractComponentInstance, SourceApplication, HarnessedApplication,
    SourceExplicitContract, HarnessedExplicitContract, SourceExternalActor,
    HarnessDefinition, SolidityIdentifier,
)
from composer.spec.cvl_generation import GeneratedCVL
from composer.spec.prop_inference import CERTORA_BACKEND_GUIDANCE
from composer.spec.source.harness import (
    run_harness_creation, run_autosetup_phase, ContractSetup, SystemDescriptionHarnessed,
)
from composer.spec.source.summarizer import setup_summaries
from composer.spec.source.struct_invariant import get_invariant_formulation
from composer.spec.source.autosetup import SetupSuccess
from composer.spec.source.prover import get_prover_tool
from composer.spec.source.author import batch_cvl_generation
from composer.spec.source.artifacts import ProverArtifactStore, ComponentSpec, InvariantSpec
from composer.spec.source.report_prover import make_prover_fetcher
from composer.spec.source.report.collect import ReportComponentInput, Verdict, VerdictFetcher
from composer.spec.source.report.schema import RuleName
from composer.spec.source.task_ids import (
    HARNESS_TASK_ID, AUTOSETUP_TASK_ID, SUMMARIES_TASK_ID,
    INVARIANTS_TASK_ID, INVARIANT_CVL_TASK_ID,
)
from composer.prover.core import ProverOptions
from composer.ui.autoprove_app import AutoProvePhase
from composer.pipeline.core import (
    Formalizer, PreparedSystem, PipelineRun, Delivered, GaveUp,
    CorePhases, SystemAnalysisSpec, ComponentOutcome, main_instance,
    COMMON_SYSTEM_CACHE_KEY
)


INV_CVL_KEY = CacheKey[None, GeneratedCVL]("invariant-cvl")


def _lift_harnessed(
    s: SourceApplication, sys_desc: SystemDescriptionHarnessed,
) -> HarnessedApplication:
    """Re-key harness definitions by harnessed contract and fold them into a
    ``HarnessedApplication`` — each ``SourceExplicitContract`` becomes a
    ``HarnessedExplicitContract`` carrying the harnesses generated for it."""
    contract_to_harness: dict[SolidityIdentifier, list[HarnessDefinition]] = {}
    for c in sys_desc.transitive_closure:
        if not c.harness_definition:
            continue
        contract_to_harness.setdefault(c.harness_definition.harness_of, []).append(
            HarnessDefinition(name=c.solidity_identifier, path=c.path)
        )

    comp: list[SourceExternalActor | HarnessedExplicitContract] = []
    for c in s.components:
        if not isinstance(c, SourceExplicitContract):
            comp.append(c)
            continue
        comp.append(HarnessedExplicitContract(
            sort=c.sort,
            name=c.name,
            solidity_identifier=c.solidity_identifier,
            components=c.components,
            description=c.description,
            path=c.path,
            harnesses=contract_to_harness.get(c.solidity_identifier, []),
        ))
    return HarnessedApplication(
        application_type=s.application_type, description=s.description, components=comp,
    )


@dataclass
class ProverRunner(Formalizer[GeneratedCVL]):
    """Immutable formalizer: per-batch CVL generation against a fixed prover
    config + resource set (already including ``invariants.spec`` when there are
    structural invariants), plus the in-memory invariant result for the report."""
    _store: ProverArtifactStore
    _prover_tool: BaseTool
    _prover_config: dict
    _resources: list[CVLResource]
    _invariant: tuple[list[PropertyFormulation], Delivered[GeneratedCVL]] | None
    _fetch: VerdictFetcher[GeneratedCVL]

    @override
    async def formalize(
        self,
        label: str,
        feat: ContractComponentInstance,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[GeneratedCVL],
        run: PipelineRun,
    ) -> GeneratedCVL | GaveUp:
        return await batch_cvl_generation(
            ctx=ctx.abstract(CVLGeneration),
            init_config=self._prover_config,
            props=props,
            component=feat,
            resources=self._resources,
            prover_tool=self._prover_tool,
            env=run.env,
            description=label,
            source=run.source,
            spec_dir=SPECS_DIR,
            spec_stem=ComponentSpec(feat.slugified_name).stem
        )

    @override
    def extra_report_inputs(self) -> list[ReportComponentInput[GeneratedCVL]]:
        # The synthetic structural-invariant entry; per-component inputs are assembled by the driver.
        if self._invariant is None:
            return []
        inv_props, inv = self._invariant
        return [ReportComponentInput(
            name="Structural Invariants", props=inv_props, formalized=inv,
        )]

    @override
    async def fetch_verdicts(
        self, inp: ReportComponentInput[GeneratedCVL],
    ) -> dict[RuleName, Verdict]:
        return await self._fetch(inp)

    @override
    async def finalize(self, outcomes: list[ComponentOutcome[GeneratedCVL]], run: PipelineRun) -> None:
        # components_to_prover_runs.json: {run_key (slug): prover /output/ link}.
        runs: dict[str, str] = {
            ComponentSpec(o.feat.slugified_name).run_key: o.result.run_link
            for o in outcomes
            if isinstance(o.result, Delivered) and o.result.run_link
        }
        if self._invariant is not None:
            inv = self._invariant[1]
            if inv.run_link:
                runs[InvariantSpec().run_key] = inv.run_link
        self._store.write_component_runs(runs)


@dataclass
class ProverPrepared(PreparedSystem[GeneratedCVL]):
    """Post-harness system: holds the harnessed app + prover tool, and runs the
    prover-only pre-formalization fan-out in ``prepare_formalization``."""
    _store: ProverArtifactStore
    _sys_desc: SystemDescriptionHarnessed
    _harnessed: HarnessedApplication
    _prover_tool: BaseTool
    _prover_opts: ProverOptions
    _analyzed: SourceApplication

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[GeneratedCVL]:
        # AutoSetup (+ custom summaries) ∥ structural-invariant formulation; both
        # depend only on the harnessed app, so they run concurrently.
        (setup_config, resources), invariants = await asyncio.gather(
            self._autosetup(run), self._invariants(run),
        )

        invariant: tuple[list[PropertyFormulation], Delivered[GeneratedCVL]] | None = None
        if invariants.inv:
            inv_props = [
                PropertyFormulation(title=inv.name, description=inv.description, sort="invariant")
                for inv in invariants.inv
            ]
            self._store.write_properties(InvariantSpec(), inv_props)

            inv_cvl_ctx = run.ctx.child(INV_CVL_KEY)
            cached = await inv_cvl_ctx.cache_get(GeneratedCVL)
            if cached is not None:
                inv_cvl = cached
            else:
                inv_result = await run.runner(
                    TaskInfo(INVARIANT_CVL_TASK_ID, "Invariant CVL", AutoProvePhase.CVL_GEN),
                    lambda: batch_cvl_generation(
                        ctx=inv_cvl_ctx.abstract(CVLGeneration),
                        init_config=setup_config.prover_config,
                        props=inv_props,
                        component=None,
                        resources=resources,
                        prover_tool=self._prover_tool,
                        env=run.env,
                        description="Structural invariant CVL",
                        source=run.source,
                        spec_dir=SPECS_DIR,
                        spec_stem=InvariantSpec().stem
                    ),
                )
                if isinstance(inv_result, GaveUp):
                    raise RuntimeError(
                        f"Structural invariant CVL generation gave up: {inv_result.reason}"
                    )
                inv_cvl = inv_result
                await inv_cvl_ctx.cache_put(inv_cvl)

            # Writes invariants.spec + bundle, returns its project-root-relative path.
            inv_path = self._store.write_artifact(InvariantSpec(), inv_cvl)
            # All pre-formalization work has joined, so appending here is race-free;
            # the per-component CVLs (run after this returns) will see invariants.spec.
            resources = [*resources, CVLResource(
                path=inv_path,
                required=False,
                description="Structural invariants that may be assumed as preconditions",
                sort="import",
            )]
            invariant = (inv_props, Delivered(inv_cvl, inv_path))

        return ProverRunner(
            GeneratedCVL, "prover",
            self._store, self._prover_tool, setup_config.prover_config, resources, invariant,
            make_prover_fetcher(),
        )

    async def _autosetup(self, run: PipelineRun) -> tuple[SetupSuccess, list[CVLResource]]:
        setup_config = await run.runner(
            TaskInfo(AUTOSETUP_TASK_ID, "AutoSetup", AutoProvePhase.AUTOSETUP),
            lambda: run_autosetup_phase(
                run.ctx, run.source, self._sys_desc, self._analyzed, self._prover_opts,
            ),
        )
        resources: list[CVLResource] = [CVLResource(
            path=certora_relative_to_project(setup_config.summaries_path),
            required=True,
            description="AutoSetup-generated summaries",
            sort="import",
        )]
        if self._sys_desc.erc20_contracts or self._sys_desc.external_interfaces:
            summary_resource = await run.runner(
                TaskInfo(SUMMARIES_TASK_ID, "Custom Summaries", AutoProvePhase.SUMMARIES),
                lambda: setup_summaries(
                    ctx=run.ctx,
                    app=self._harnessed,
                    config=ContractSetup(system_description=self._sys_desc, config=setup_config),
                    env=run.env,
                    source=run.source,
                ),
            )
            resources.append(summary_resource)
        return setup_config, resources

    async def _invariants(self, run: PipelineRun):
        return await run.runner(
            TaskInfo(INVARIANTS_TASK_ID, "Structural Invariants", AutoProvePhase.INVARIANTS),
            lambda: get_invariant_formulation(run.ctx, run.source, run.env, self._harnessed),
        )


@dataclass
class ProverBackend:
    """PipelineBackend[AutoProvePhase, GeneratedCVL, None, ComponentSpec]."""
    backend_guidance = CERTORA_BACKEND_GUIDANCE
    core_phases = CorePhases({
        "analysis": AutoProvePhase.COMPONENT_ANALYSIS,
        "extraction": AutoProvePhase.BUG_ANALYSIS,
        "formalization": AutoProvePhase.CVL_GEN,
        "report": AutoProvePhase.REPORT
    })
    analysis_spec = SystemAnalysisSpec(COMMON_SYSTEM_CACHE_KEY, "ap-properties")

    artifact_store: ProverArtifactStore
    _prover_opts: ProverOptions

    async def prepare_system(
        self, analyzed: SourceApplication, run: PipelineRun[AutoProvePhase, None],
    ) -> PreparedSystem[GeneratedCVL]:
        sys_desc = await run.runner(
            TaskInfo(HARNESS_TASK_ID, "Harness Creation", AutoProvePhase.HARNESS),
            lambda: run_harness_creation(run.ctx, run.source, run.env, analyzed),
        )
        harnessed = _lift_harnessed(analyzed, sys_desc)
        prover_tool = get_prover_tool(
            run.env.llm_heavy(), run.source.contract_name, run.source.project_root,
            prover_opts=self._prover_opts,
        )
        return ProverPrepared(
            main_instance(harnessed, run.source),
            self.artifact_store, sys_desc, harnessed, prover_tool,
            self._prover_opts, analyzed,
        )

    def to_artifact_id(self, c: ContractComponentInstance) -> ComponentSpec:
        return ComponentSpec(c.slugified_name)
