"""Auto-prove backend for the generic pipeline (``composer.pipeline.core``).

The shared driver owns system analysis, per-component property extraction, the
result-type-keyed cache, and the report. This module contributes only the
prover-specific pieces as the three phase objects:

* ``ProverBackend.prepare_system`` — harness creation, then the lift of the
  analyzed ``SourceApplication`` into a ``HarnessedApplication`` and the prover
  tool. Returns a ``ProverPrepared``.
* ``ProverPrepared.prepare_formalization`` — AutoSetup, then the custom summaries
  that build on its config. Returns a ``ProverRunner``.
* ``ProverRunner`` — per-batch CVL generation (``batch_cvl_generation``) and
  prover-run-backed verdicts (``make_prover_fetcher``).

``run_autoprove_pipeline`` is now a thin wrapper that builds the backend + run
context and hands them to ``run_pipeline``.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import override, Sequence

from langchain_core.tools import BaseTool

from composer.io.multi_job import TaskInfo
from composer.spec.context import WorkflowContext, CVLGeneration
from composer.spec.types import PropertyFormulation
from composer.spec.gen_types import CVLResource, SPECS_DIR, certora_relative_to_project
from composer.spec.system_model import (
    ContractComponentInstance, ContractInstance, SourceApplication, HarnessedApplication,
    SourceExplicitContract, HarnessedExplicitContract, SourceExternalActor,
    HarnessDefinition, SolidityIdentifier,
)
from composer.spec.cvl_generation import GeneratedCVL
from composer.spec.prop_inference import CERTORA_BACKEND_GUIDANCE
from composer.spec.source.harness import (
    run_harness_creation, run_autosetup_phase, ContractSetup, SystemDescriptionHarnessed,
    lift_harnessed,
)
from composer.spec.source.summarizer import setup_summaries
from composer.spec.source.prover import get_prover_tool, materializing_project
from composer.spec.source.plugin import CertoraProverTools
from composer.spec.source.author import (
    batch_cvl_generation, EditingTools, FocusPolicy, SourceEditing, ProverTool,
)
from composer.spec.source.artifacts import ProverArtifactStore, ComponentSpec
from composer.spec.source.report_prover import make_prover_fetcher
from composer.spec.source.report.collect import (
    Formalized, EvidenceFetcher, RuleEvidence, Verdict, VerdictFetcher,
)
from composer.spec.source.report.schema import (
    AppliedEditRecord, RuleName, SourceEditRecord,
)
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.source.munge.vfs_diff import diff_against_baseline

from composer.spec.source.task_ids import (
    HARNESS_TASK_ID, AUTOSETUP_TASK_ID, SUMMARIES_TASK_ID,
)
from composer.prover.core import ProverOptions
from composer.ui.autoprove_app import AutoProvePhase
from composer.pipeline.core import (
    Formalizer, PreparedSystem, PipelineRun, Delivered, GaveUp,
    CorePhases, SystemAnalysisSpec, ComponentOutcome, ToolBinder,
    Curtailed
)
from composer.pipeline.ecosystem import main_instance
from composer.pipeline.run_mode import RunMode
from composer.pipeline.keys import COMMON_SYSTEM_CACHE_KEY
from composer.spec.source.keys import AP_PROPERTIES_KEY_NAME

@dataclass
class _ProverPipelineDeps:
    prover_options: ProverOptions
    store: ProverArtifactStore
    analysis_store: CexAnalysisStore
    editing: SourceEditing

    def to_prover_tool(self, tool: BaseTool) -> ProverTool:
        return ProverTool(lg_tool=tool, options=self.prover_options)

@dataclass
class ProverRunner(Formalizer[GeneratedCVL, ContractComponentInstance]):
    """Immutable formalizer: per-batch CVL generation against a fixed prover
    config + resource set."""
    _prover_tool: BaseTool
    _prover_config: dict
    _resources: list[CVLResource]
    _fetch: VerdictFetcher[GeneratedCVL]
    _deps: _ProverPipelineDeps

    tool_provider_type = CertoraProverTools

    @override
    async def formalize(
        self,
        label: str,
        feat: ContractComponentInstance,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[GeneratedCVL],
        run: PipelineRun,
        extra_tools: ToolBinder[ContractComponentInstance]
    ) -> GeneratedCVL | Curtailed[GeneratedCVL] | GaveUp:
        return await batch_cvl_generation(
            ctx=ctx.abstract(CVLGeneration),
            init_config=self._prover_config,
            props=props,
            component=feat,
            resources=self._resources,
            prover_tool=self._deps.to_prover_tool(self._prover_tool),
            env=run.env,
            description=label,
            source=run.source,
            spec_dir=SPECS_DIR,
            spec_stem=ComponentSpec(feat.slugified_name).stem,
            editing_tools=EditingTools(
                editing=self._deps.editing, tool_provider=extra_tools
            ),
            # A prioritized run staked itself on these properties, so the author's exits
            # tighten around them: they cannot be skipped, and an exhausted give-up is
            # refused until the prover has actually been run.
            focus=(
                FocusPolicy(protected=tuple(p.title for p in props))
                if run.run_mode is RunMode.PRIORITIZED else None
            ),
        )

    @override
    async def fetch_verdicts(
        self, formalized: Formalized[GeneratedCVL],
    ) -> dict[RuleName, Verdict]:
        return await self._fetch(formalized)

    @override
    async def source_edits(
        self, outcomes: list[ComponentOutcome[GeneratedCVL, ContractComponentInstance]], run: PipelineRun
    ) -> list[SourceEditRecord]:
        records: list[SourceEditRecord] = []
        for o in outcomes:
            if not isinstance(o.result, Delivered) or not o.result.result.applied_edits:
                continue
            res = o.result.result
            records.append(SourceEditRecord(
                component=o.feat.component.name,
                applied_edits=[AppliedEditRecord(**e.model_dump()) for e in res.applied_edits],
                cumulative_diff=await asyncio.to_thread(
                    diff_against_baseline, res.vfs, Path(run.source.project_root)
                ),
            ))
        return records

    @override
    def findings_evidence(self) -> EvidenceFetcher | None:
        # Prover runs capture per-rule counterexample analysis, so this backend opts into findings.
        return self._fetch_evidence

    async def _fetch_evidence(self, rule_name: str) -> list[RuleEvidence]:
        # Every instantiation the run analyzed, not just one: a parametric rule can fail differently
        # per binding while the report shows a single row for the whole rule.
        return [
            RuleEvidence(label=r.label, analysis=r.analysis, counterexample=r.counterexample)
            for r in await self._deps.analysis_store.for_rule(rule_name)
        ]

    @override
    async def finalize(self, outcomes: list[ComponentOutcome[GeneratedCVL, ContractComponentInstance]], run: PipelineRun) -> None:
        # components_to_prover_runs.json: {run_key (slug): prover /output/ link}. Deliveries
        # only — a curtailed partial's last run says nothing about its final content.
        runs: dict[str, str] = {
            ComponentSpec(o.feat.slugified_name).run_key: o.result.run_link
            for o in outcomes
            if isinstance(o.result, Delivered) and o.result.run_link
        }
        self._deps.store.write_component_runs(runs)


@dataclass
class ProverPrepared(PreparedSystem[GeneratedCVL, ContractComponentInstance, ContractInstance]):
    """Post-harness system: holds the harnessed app + prover tool, and runs the
    prover-only pre-formalization work (AutoSetup, then custom summaries) in
    ``prepare_formalization``."""
    _sys_desc: SystemDescriptionHarnessed
    _harnessed: HarnessedApplication
    _prover_tool: BaseTool
    _analyzed: SourceApplication

    _deps: _ProverPipelineDeps

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[GeneratedCVL, ContractComponentInstance]:
        setup_config = await run.runner(
            TaskInfo(AUTOSETUP_TASK_ID, "AutoSetup", AutoProvePhase.AUTOSETUP),
            lambda: run_autosetup_phase(
                run.ctx, run.source, self._sys_desc, self._analyzed, self._deps.prover_options,
            ),
        )
        resources: list[CVLResource] = [CVLResource(
            path=certora_relative_to_project(setup_config.summaries_path),
            required=True,
            description="AutoSetup-generated summaries",
            sort="import",
        )]
        # The custom summaries build on AutoSetup's config, so they follow it rather than
        # running alongside it.
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

        return ProverRunner(
            GeneratedCVL, "prover",
            self._prover_tool, setup_config.prover_config, resources,
            make_prover_fetcher(), self._deps
        )

@dataclass
class ProverBackend:
    """``PipelineBackend[AutoProvePhase, GeneratedCVL, None, SpecIdentity, ContractComponentInstance,
    ContractInstance, SourceApplication, None]`` (P, FormT, H, A, Unit, Main, App, Pre) — structural."""
    backend_guidance = CERTORA_BACKEND_GUIDANCE
    core_phases = CorePhases({
        "analysis": AutoProvePhase.COMPONENT_ANALYSIS,
        "extraction": AutoProvePhase.BUG_ANALYSIS,
        "formalization": AutoProvePhase.CVL_GEN,
        "report": AutoProvePhase.REPORT
    })
    analysis_spec = SystemAnalysisSpec(COMMON_SYSTEM_CACHE_KEY, AP_PROPERTIES_KEY_NAME)

    artifact_store: ProverArtifactStore
    _prover_opts: ProverOptions
    editing: SourceEditing
    analysis_store: CexAnalysisStore

    async def preflight(self, run: PipelineRun[AutoProvePhase, None]) -> None:
        """Nothing to do ahead of analysis. The prover's pre-work (AutoSetup, then the custom
        summaries built on its config) needs the *harnessed* model, so it stays in
        ``prepare_formalization``, where it already overlaps property extraction."""
        return None

    async def prepare_system(
        self, analyzed: SourceApplication, run: PipelineRun[AutoProvePhase, None],
        preflight: None,
    ) -> PreparedSystem[GeneratedCVL, ContractComponentInstance, ContractInstance]:
        sys_desc = await run.runner(
            TaskInfo(HARNESS_TASK_ID, "Harness Creation", AutoProvePhase.HARNESS),
            lambda: run_harness_creation(run.ctx, run.source, run.env, analyzed),
        )
        harnessed = lift_harnessed(analyzed, sys_desc)
        # The materializing strategy covers every phase with one tool: an empty
        # VFS (invariants, or an author that never edited) runs in-situ; a
        # non-empty one runs in a temp materialization of the working copy.
        prover_tool = get_prover_tool(
            run.env.llm_heavy(), run.source.contract_name, materializing_project(run.source.project_root, self.editing.live.mat),
            prover_opts=self._prover_opts, analysis_store=self.analysis_store,
        )
        return ProverPrepared(
            main=main_instance(harnessed, run.source),
            _sys_desc=sys_desc, _harnessed=harnessed, _prover_tool=prover_tool,
            _analyzed=analyzed,
            _deps=_ProverPipelineDeps(self._prover_opts, self.artifact_store, self.analysis_store, self.editing)
        )

    def to_artifact_id(self, c: ContractComponentInstance) -> ComponentSpec:
        return ComponentSpec(c.slugified_name)
