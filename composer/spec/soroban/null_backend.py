"""A null Solana backend — records extracted properties without verifying them.

It implements the full ``PipelineBackend`` contract over the Solana ecosystem's
``(SolanaApplication, SolanaProgramInstance, SolanaComponentInstance)`` triple, but its
``formalize`` just echoes the extracted properties into a trivial result and its
``fetch_verdicts`` returns nothing.

**Role:** a **test double** for the Soroban front half (analysis + property extraction)
without a real verifier — see ``tests/test_soroban_gate.py``. Production Soroban
verification is the Certora Prover.
"""

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import override, Sequence, Any

from pydantic import BaseModel, Field

from composer.pipeline.core import (
    CorePhases,
    Formalizer,
    GaveUp,
    PipelineRun,
    PreparedSystem,
    SystemAnalysisSpec,
    ToolBinder,
)
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import WorkflowContext
from composer.authoring.state import SkippedProperty
from composer.spec.soroban.model import (
    SorobanApplication,
    SorobanComponentInstance,
    SorobanContractInstance,
)
from composer.spec.source.report.collect import Formalized, Verdict
from composer.spec.source.report.schema import RuleName
from composer.spec.types import PropertyFormulation, PropertyTitle
from composer.spec.util import ensure_dir

SOROBAN_NULL_GUIDANCE: str = """\
These properties are recorded by a null backend (no verification is performed). Extract
properties a Soroban verification tool could plausibly check: account/state invariants, access
control (signer/owner/authority), and arithmetic safety. Freely
state universally-quantified properties.
"""


class SorobanPhase(enum.Enum):
    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    FORMALIZATION = "formalization"
    REPORT = "report"


class NullResult(BaseModel):
    """A trivial formalization result: it just carries the properties back out."""

    commentary: str = ""
    property_rules: list[tuple[PropertyTitle, list[RuleName]]] = Field(default_factory=list)
    skipped: list[SkippedProperty] = Field(default_factory=list)

    def property_checks(self) -> list[tuple[PropertyTitle, list[RuleName]]]:
        return [(t, list(u)) for t, u in self.property_rules]

    @property
    def artifact_text(self) -> str:
        return json.dumps(
            {"commentary": self.commentary, "properties": self.property_checks()}, indent=2
        )

    @property
    def output_link(self) -> str | None:
        return None


@dataclass(frozen=True)
class NullArtifact:
    slug: str

    @property
    def stem(self) -> str:
        return f"null_{self.slug}"

    @property
    def artifact_file(self) -> str:
        return f"{self.stem}.json"


class NullSorobanArtifactStore(ArtifactStore[NullArtifact, NullResult]):
    def __init__(self, project_root: str):
        super().__init__(
            project_root,
            "property_checks",
            deliverable_dir="certora/soroban_null",
            internal_dir=".certora_internal/soroban_null",
            report_dir="certora/soroban_null/reports",
        )

    @override
    def _artifact_dir(self) -> Path:
        return ensure_dir(Path(self._project_root) / "certora/soroban_null/artifacts")


class NullSorobanFormalizer(Formalizer[NullResult, SorobanComponentInstance]):
    def __init__(self) -> None:
        # ``"none"``: this backend verifies nothing, and its report should say so rather than
        # borrow a real verifier's vocabulary — every unit comes out UNKNOWN, which that tag's
        # wording renders as "Unverified".
        super().__init__(NullResult, "none")

    @override
    async def formalize(
        self,
        label: str,
        feat: SorobanComponentInstance,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[NullResult],
        run: PipelineRun,
        extra_tools: ToolBinder[SorobanComponentInstance]
    ) -> NullResult | GaveUp:
        return NullResult(
            commentary=f"Null formalization of instruction {feat.display_name} "
            f"({len(props)} properties recorded, unverified).",
            # The pseudo-check is named after the title itself: nothing runs, so the property's
            # own words are the only name its report row could have.
            property_rules=[(p.title, [RuleName(p.title)]) for p in props],
        )

    @override
    async def fetch_verdicts(
        self, formalized: Formalized[NullResult]
    ) -> dict[RuleName, Verdict]:
        return {}


@dataclass
class NullSorobanPrepared(PreparedSystem[NullResult, SorobanComponentInstance, SorobanContractInstance]):
    form: NullSorobanFormalizer

    @override
    async def prepare_formalization(
        self, run: PipelineRun
    ) -> Formalizer[NullResult, SorobanComponentInstance]:
        return self.form


@dataclass
class NullSorobanBackend:
    """``PipelineBackend[SorobanPhase, NullResult, None, NullArtifact, SorobanComponentInstance,
    SorobanContractInstance, SorobanApplication, None]`` (P, FormT, H, A, Unit, Main, App, Pre) — structural."""

    artifact_store: NullSorobanArtifactStore
    backend_guidance = SOROBAN_NULL_GUIDANCE
    analysis_spec = SystemAnalysisSpec("soroban-analysis", "soroban-properties")
    core_phases = CorePhases(
        {
            "analysis": SorobanPhase.ANALYSIS,
            "extraction": SorobanPhase.EXTRACTION,
            "formalization": SorobanPhase.FORMALIZATION,
            "report": SorobanPhase.REPORT,
        }
    )

    async def preflight(self, run: PipelineRun[SorobanPhase, None]) -> None:
        """Nothing to prepare — this backend builds nothing and only records properties."""
        return None

    async def prepare_system(
        self, analyzed: SorobanApplication, run: PipelineRun[SorobanPhase, None], preflight: None
    ) -> PreparedSystem[NullResult, SorobanComponentInstance, SorobanContractInstance]:
        # Use the Solana ecosystem's locate_main so the backend and ecosystem agree on the
        # target program (imported lazily to avoid an import cycle with pipeline.ecosystem).
        from composer.pipeline.ecosystem import SOROBAN

        return NullSorobanPrepared(SOROBAN.locate_main(analyzed, run.source), NullSorobanFormalizer())

    def to_artifact_id(self, c: SorobanComponentInstance) -> NullArtifact:
        return NullArtifact(c.slug)
