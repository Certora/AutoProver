"""JSON output schema of certora-fv-amenability (single source of truth).

The report is the tool's whole external contract: clients (SaaS, CI, humans)
consume this JSON and nothing else. Every evidence item carries file:line so a
reviewer can jump straight to the code that moved the score.
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

LEVEL_SEMANTICS = {
    "low": "needs a full reference implementation; a small rewrite will not suffice",
    "medium": "scoped configuration/customization needed to get the automatic proof going",
    "high": "expected to pass autosetup as-is",
}


class Level(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Evidence(BaseModel):
    signal: str
    severity: Severity
    file: str
    line: int
    function: Optional[str] = None
    detail: str


class SubScore(BaseModel):
    score: float = Field(ge=0.0, le=1.0, description="1.0 = fully amenable")
    weight: float
    raw: dict[str, Any] = Field(default_factory=dict)


class StaticReport(BaseModel):
    provisional_level: Level
    weighted_score: float
    sub_scores: dict[str, SubScore]


class Recommendation(BaseModel):
    kind: str  # summary | harness | munge | reference-impl
    detail: str


class AmenabilityReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    tool_version: str
    project: str
    contracts_analyzed: list[str]
    mode: str = "ast"
    level: Level
    confidence: float = Field(ge=0.0, le=1.0)
    level_semantics: dict[str, str] = Field(default_factory=lambda: dict(LEVEL_SEMANTICS))
    static: StaticReport
    evidence: list[Evidence]
    judge: Optional[dict[str, Any]] = None  # populated by the phase-2 LLM judge
    recommendations: list[Recommendation] = Field(default_factory=list)


class ScoringErrorReport(BaseModel):
    """Emitted (with exit code 1) when the project cannot be scored at all —
    most importantly when it does not compile: compiling is the amenability floor,
    so there is no degraded scoring path."""

    schema_version: str = SCHEMA_VERSION
    project: str
    error: str  # e.g. "does-not-compile", "no-ast-dump"
    detail: str
