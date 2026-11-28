"""The review of a results file, schema ``certora-autoprover-review/1``: a verdict on every finding
and on every model of external contracts the file lists, bound to the file by the SHA-256 of its
bytes. The field descriptions are part of the format.
"""

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from composer.spec.source.report.schema import SeverityTier
from composer.verification_results.model import VerificationResults, _Strict

REVIEW_SCHEMA_ID = "certora-autoprover-review/1"

type FindingVerdictKind = Literal["valid", "minor", "not_an_issue"]
type ModelVerdictKind = Literal["faithful", "narrower", "wrong"]
type AssumptionVerdictKind = Literal["justified", "not_justified"]

type Reason = Annotated[str, Field(min_length=20, description="Why, in terms of the code and the run.")]


class FindingVerdict(_Strict):
    id: str = Field(description="The finding's id in the results file.")
    verdict: FindingVerdictKind
    severity: SeverityTier | None = Field(
        description="Required for valid, null for not_an_issue, optional for minor."
    )
    reason: Reason

    @model_validator(mode="after")
    def _severity_matches_verdict(self) -> "FindingVerdict":
        if self.verdict == "valid" and self.severity is None:
            raise ValueError("a valid finding needs a severity")
        if self.verdict == "not_an_issue" and self.severity is not None:
            raise ValueError("a finding that is not an issue has no severity")
        return self


class AssumptionVerdict(_Strict):
    text: str = Field(description="The assumption's text in the results file.")
    verdict: AssumptionVerdictKind
    reason: Reason


class ModelVerdict(_Strict):
    name: str = Field(description="The model's name in the results file.")
    verdict: ModelVerdictKind = Field(
        description="faithful: the model matches the real contract, or its documented interface "
        "when no code is available. narrower: it leaves out real behaviour, which reason names. "
        "wrong: it allows or forbids behaviour the real contract does not."
    )
    reason: Reason
    assumptions: list[AssumptionVerdict] = Field(
        description="One verdict per assumption of the model."
    )


class Review(_Strict):
    model_config = ConfigDict(
        title="Review of the results file of one verification run",
        json_schema_extra={"$id": REVIEW_SCHEMA_ID},
    )

    schema_: Literal["certora-autoprover-review/1"] = Field(alias="schema")
    tool: str = Field(min_length=1, description="The reviewer and its version.")
    generated_at: datetime
    results_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256, in hex, of the bytes of the results file the review was made from.",
    )
    findings: list[FindingVerdict] = Field(description="One verdict per finding of the file.")
    models: list[ModelVerdict] = Field(description="One verdict per model of the file.")


def _mismatch(kind: str, reviewed: Sequence[str], listed: Sequence[str]) -> list[str]:
    problems: list[str] = []
    if len(reviewed) != len(set(reviewed)):
        problems.append(f"a {kind} has more than one verdict")
    if missing := sorted(set(listed) - set(reviewed)):
        problems.append(f"no verdict for the {kind}s {', '.join(missing)}")
    if unknown := sorted(set(reviewed) - set(listed)):
        problems.append(f"verdicts for {kind}s the results file does not list: {', '.join(unknown)}")
    return problems


def review_problems(review: Review, results: VerificationResults, results_bytes: bytes) -> list[str]:
    """Why ``review`` is not a review of the results file whose bytes are ``results_bytes`` and
    whose content is ``results``: another file's hash, or not exactly one verdict per finding,
    model and assumption."""
    if review.results_sha256 != hashlib.sha256(results_bytes).hexdigest():
        return ["the review was made from another version of the results file"]
    models = {m.name: m for m in results.models or []}
    problems = [
        *_mismatch("finding", [v.id for v in review.findings], [f.id for f in results.findings]),
        *_mismatch("model", [v.name for v in review.models], list(models)),
    ]
    for v in review.models:
        if (model := models.get(v.name)) is not None:
            problems += [
                f"model {v.name}: {p}" for p in _mismatch(
                    "assumption", [a.text for a in v.assumptions], [a.text for a in model.assumptions],
                )
            ]
    return problems
