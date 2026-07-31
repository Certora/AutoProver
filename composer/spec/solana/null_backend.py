"""A null Solana backend — records extracted properties without verifying them.

It satisfies the full ``PipelineBackend`` contract over the Solana ecosystem's
``(SolanaApplication, SolanaProgramInstance, SolanaComponentInstance)`` triple, but its
``formalize`` just echoes the extracted properties into a trivial result and its
``fetch_verdicts`` returns nothing.

**Role:** a **test double** for the Solana front half (analysis + property extraction)
without a real verifier — see ``tests/test_solana_gate.py``. Production Solana
verification is the Crucible fuzzer backend.
"""

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import override

from pydantic import BaseModel, Field

from composer.pipeline.core import (
    CorePhases,
    Formalizer,
    GaveUp,
    PipelineRun,
    PreparedSystem,
    SystemAnalysisSpec,
)
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import WorkflowContext
from composer.spec.cvl_generation import SkippedProperty
from composer.spec.solana.model import (
    SolanaApplication,
    SolanaComponentInstance,
    SolanaProgramInstance,
)
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import RuleName
from composer.spec.types import PropertyFormulation
from composer.spec.util import ensure_dir

SOLANA_NULL_GUIDANCE: str = """\
These properties are recorded by a null backend (no verification is performed). Extract
properties a Solana verification tool could plausibly check: account/state invariants, access
control (signer/owner/authority), PDA-derivation correctness, and arithmetic safety. Freely
state universally-quantified properties.
"""


class SolanaPhase(enum.Enum):
    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    FORMALIZATION = "formalization"
    REPORT = "report"


class NullResult(BaseModel):
    """A trivial formalization result: it just carries the properties back out."""

    commentary: str = ""
    property_rules: list[tuple[str, list[str]]] = Field(default_factory=list)
    skipped: list[SkippedProperty] = Field(default_factory=list)

    def property_units(self) -> list[tuple[str, list[str]]]:
        return [(t, list(u)) for t, u in self.property_rules]

    @property
    def artifact_text(self) -> str:
        return json.dumps(
            {"commentary": self.commentary, "properties": self.property_units()}, indent=2
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


class NullSolanaArtifactStore(ArtifactStore[NullArtifact, NullResult]):
    def __init__(self, project_root: str):
        super().__init__(
            project_root,
            "property_units",
            deliverable_dir="certora/solana_null",
            internal_dir=".certora_internal/solana_null",
            report_dir="certora/solana_null/reports",
        )

    @override
    def _artifact_dir(self) -> Path:
        return ensure_dir(Path(self._project_root) / "certora/solana_null/artifacts")


class NullSolanaFormalizer(Formalizer[NullResult, SolanaComponentInstance]):
    def __init__(self) -> None:
        # The tag is provenance only — it picks the report's outcome labels, and this backend's
        # results are all-UNKNOWN either way. So borrow ``"prover"``, an existing member of
        # ``ReportBackend``: the real Solana verifier's own tag is added to that literal by the
        # backend that introduces it, and until then a value outside the literal is not merely
        # untyped but unusable — ``AutoProverReport`` is a pydantic model, so it would fail
        # validation in ``build_report`` and lose the report phase to a swallowed exception.
        super().__init__(NullResult, "prover")

    @override
    async def formalize(
        self,
        label: str,
        feat: SolanaComponentInstance,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[NullResult],
        run: PipelineRun,
    ) -> NullResult | GaveUp:
        return NullResult(
            commentary=f"Null formalization of instruction {feat.display_name} "
            f"({len(props)} properties recorded, unverified).",
            property_rules=[(p.title, [p.title]) for p in props],
        )

    @override
    async def fetch_verdicts(
        self, inp: ReportComponentInput[NullResult]
    ) -> dict[RuleName, Verdict]:
        return {}


@dataclass
class NullSolanaPrepared(PreparedSystem[NullResult, SolanaComponentInstance, SolanaProgramInstance]):
    form: NullSolanaFormalizer

    @override
    async def prepare_formalization(
        self, run: PipelineRun
    ) -> Formalizer[NullResult, SolanaComponentInstance]:
        return self.form


@dataclass
class NullSolanaBackend:
    """``PipelineBackend[SolanaPhase, NullResult, None, NullArtifact, SolanaComponentInstance,
    SolanaProgramInstance, SolanaApplication]`` (P, FormT, H, A, Unit, Main, App) — structural."""

    artifact_store: NullSolanaArtifactStore
    backend_guidance = SOLANA_NULL_GUIDANCE
    analysis_spec = SystemAnalysisSpec("solana-analysis", "solana-properties")
    core_phases = CorePhases(
        {
            "analysis": SolanaPhase.ANALYSIS,
            "extraction": SolanaPhase.EXTRACTION,
            "formalization": SolanaPhase.FORMALIZATION,
            "report": SolanaPhase.REPORT,
        }
    )

    async def prepare_system(
        self, analyzed: SolanaApplication, run: PipelineRun[SolanaPhase, None]
    ) -> PreparedSystem[NullResult, SolanaComponentInstance, SolanaProgramInstance]:
        # Use the Solana ecosystem's locate_main so the backend and ecosystem agree on the
        # target program (imported lazily to avoid an import cycle with pipeline.ecosystem).
        from composer.pipeline.ecosystem import SOLANA

        return NullSolanaPrepared(SOLANA.locate_main(analyzed, run.source), NullSolanaFormalizer())

    def to_artifact_id(self, c: SolanaComponentInstance) -> NullArtifact:
        return NullArtifact(c.slug)
