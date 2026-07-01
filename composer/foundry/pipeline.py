"""Foundry test-generation pipeline.

Orchestrates the same component-analysis + per-component property
inference scaffolding the autoprove pipeline uses, but routes the
per-component generation step into ``batch_foundry_test_generation``
(foundry ``.t.sol`` output, ``forge test`` gating) instead of CVL+prover.

Reused as-is from existing infrastructure (NOT modified):

* ``composer.spec.system_analysis.run_component_analysis`` — produces an
  ``Application``-typed model of the system from the design doc + source.
* ``composer.spec.prop_inference.run_property_inference`` — per-component
  property extraction.
* ``composer.spec.context.WorkflowContext`` / ``SourceCode`` / cache keys.

Writes one ``.t.sol`` file per component under ``<project>/test/``
(named ``composer_<component>.t.sol``) on success. Skipped batches and
give-ups are reported as failures in the result.
"""

import asyncio
import enum
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable, override

from composer.foundry.author import (
    GeneratedFoundryTest, batch_foundry_test_generation,
)
from composer.foundry.artifacts import FoundryArtifactStore
from composer.foundry.report import _foundry_verdicts
from composer.pipeline.core import (
    Formalizer, PreparedSystem, PipelineRun,
    GaveUp, SystemAnalysisSpec,
    CorePhases, main_instance, Delivered,
    run_pipeline
)
from composer.foundry.artifacts import FoundryTestArtifact
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.context import (
    CacheKey, Properties, WorkflowContext, SourceCode, FoundryGeneration
)
from composer.spec.service_host import ServiceHost
from composer.spec.types import PropertyFormulation
from composer.spec.artifacts import ArtifactStore


# Backend-guidance string fed into the property-analysis prompt — describes
# what kinds of properties are / aren't a fit for foundry's verification
# surface so the extraction agent doesn't propose properties that are
# unrealistic to formalize as ``forge test`` runs.
FOUNDRY_BACKEND_GUIDANCE: str = """\
These properties will be checked using Foundry. Foundy,
as a unit testing/fuzzing framework, cannot *prove*
universally quantified properties or invariants.
However, it can approximate these properties (via fuzz tests
and the like) and *refutations* of these universal properties
(surfaced by failures of Foundry tests) are extremely valuable.

Accordingly, you *should* freely write universally quantified properties
without taking into considerations the fundamental limitations of Foundry
as a verification backend. Do *not* artificially restrict the
space of properties you write simply because Foundry cannot *definitively*
prove them to be true; as mentioned above, the approximation of the
property is still valuable.

However, a handful of categories are genuinely a poor fit for Foundry:

1. Properties that reference off-chain events (key compromise, phishing,
   social-engineering attacks, oracle manipulation outside the test's
   modeled actors).
2. Properties whose only meaningful content is hash-collision
   resistance — "no two inputs ever collide" is unprovable by
   sampling. (Note: signature validity, signer authorization, and
   similar crypto-adjacent properties are NOT in this category.)

In addition, due to the advent of checked arithmetic, properties that
assert no overflow are uninteresting. Properties implied by the type
system (a uint256 being non-negative, etc.) are also uninteresting.
"""
from composer.spec.system_model import (
    ContractComponentInstance, SourceApplication,
)

from composer.io.multi_job import HandlerFactory

_log = logging.getLogger(__name__)


@dataclass
class _ForgeRunConfig:
    forge_binary: str
    forge_timeout_s: int
    forge_sem: asyncio.Semaphore

# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


class FoundryPhase(enum.Enum):
    """Task-grouping phases of the foundry pipeline (the ``P`` of its
    ``HandlerFactory``)."""
    SYSTEM_ANALYSIS = "system_analysis"
    PROPERTY_EXTRACTION = "property_extraction"
    TEST_GENERATION = "test_generation"
    REPORT = "report"

class FoundryFormalizer(Formalizer[GeneratedFoundryTest]):
    def __init__(self, conf: _ForgeRunConfig):
        super().__init__(GeneratedFoundryTest, "foundry")
        self.conf = conf

    @override
    async def formalize(
        self,
        label: str,
        feat: ContractComponentInstance,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[GeneratedFoundryTest],
        run: PipelineRun
    ) -> GeneratedFoundryTest | GaveUp:
        return await batch_foundry_test_generation(
            ctx=ctx.abstract(FoundryGeneration),
            project_root=run.source.project_root,
            contract_name=run.source.contract_name,
            component=feat,
            description=label,
            env=run.env,
            forge_binary=self.conf.forge_binary,
            forge_sem=self.conf.forge_sem,
            forge_timeout_s=self.conf.forge_timeout_s,
            props=props
        )
    
    @override
    async def fetch_verdicts(self, inp: ReportComponentInput[GeneratedFoundryTest]) -> dict[str, Verdict]:
        return await _foundry_verdicts(inp)

@dataclass
class FoundrySystem(PreparedSystem[GeneratedFoundryTest]):
    form: FoundryFormalizer

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[GeneratedFoundryTest]:
        return self.form

@dataclass
class FoundryBackend:
    backend_guidance = FOUNDRY_BACKEND_GUIDANCE

    core_phases = CorePhases({
        "analysis": FoundryPhase.SYSTEM_ANALYSIS,
        "extraction": FoundryPhase.PROPERTY_EXTRACTION,
        "formalization": FoundryPhase.TEST_GENERATION,
        "report": FoundryPhase.REPORT
    })

    analysis_spec = SystemAnalysisSpec("foundry-application")

    artifact_store: ArtifactStore[FoundryTestArtifact, GeneratedFoundryTest]

    foundry_conf: _ForgeRunConfig

    async def prepare_system(
        self,
        analyzed: SourceApplication,
        run: PipelineRun[FoundryPhase, None]
    ) -> PreparedSystem[GeneratedFoundryTest]:
        return FoundrySystem(
            main_instance(
                analyzed, run.source
            ),
            FoundryFormalizer(self.foundry_conf)
        )

    def to_artifact_id(self, c: ContractComponentInstance) -> FoundryTestArtifact:
        return FoundryTestArtifact(c.slugified_name)

# ---------------------------------------------------------------------------
# Cache keys (parallel to common_pipeline's)
# ---------------------------------------------------------------------------

SOURCE_ANALYSIS_KEY = CacheKey[None, SourceApplication]("foundry-source-analysis")
PROPERTIES_KEY = CacheKey[None, Properties]("foundry-properties")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class FoundryPipelineResult:
    n_components: int
    n_properties: int
    written: list[pathlib.Path] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_foundry_pipeline(
    source_input: SourceCode,
    ctx: WorkflowContext[None],
    handler_factory: HandlerFactory[FoundryPhase, None],
    env: ServiceHost,
    *,
    max_concurrent: int = 4,
    max_bug_rounds: int = 3,
    interactive: bool = False,
    forge_binary: str = "forge",
    forge_timeout_s: int = 600,
    forge_concurrency: int = 1
) -> FoundryPipelineResult:
    """Run the foundry test-generation pipeline against an existing project.

    ``source_input.project_root`` must point at a configured foundry project
    (``foundry.toml`` + ``lib/forge-std`` + the contracts under test). The
    pipeline does NOT modify ``foundry.toml`` / ``lib/`` / ``src/`` — only
    writes generated ``.t.sol`` files under ``<project>/test/``.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    artifacts = FoundryArtifactStore(
        source_input.project_root
    )
    foundry_sem = asyncio.Semaphore(forge_concurrency)

    forge_conf =_ForgeRunConfig(
        forge_binary=forge_binary,
        forge_timeout_s=forge_timeout_s,
        forge_sem=foundry_sem
    )

    backend = FoundryBackend(artifacts, forge_conf)
    run_conf = PipelineRun(
        ctx, env, source_input, handler_factory, semaphore
    )

    to_ret = await run_pipeline(
        backend, run_conf, interactive=interactive, threat_model=None, max_bug_rounds=max_bug_rounds
    )

    return FoundryPipelineResult(
        to_ret.n_components, to_ret.n_properties, written=[
            i.result.deliverable for i in to_ret.outcomes if isinstance(i.result, Delivered)
        ], failures=to_ret.failures
    )

type FoundryPipelineExecutor = Callable[
    [HandlerFactory[FoundryPhase, None]], Awaitable[FoundryPipelineResult],
]
