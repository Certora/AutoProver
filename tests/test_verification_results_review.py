import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from composer.verification_results.model import VerificationResults
from composer.verification_results.review import Review, review_problems

RESULTS: dict[str, Any] = {
    "schema": "certora-autoprover-results/1",
    "run": {
        "source": {"repository": "https://github.com/acme/vault", "commit": "a" * 40},
        "results": {"repository": "https://github.com/acme/vault-fv", "commit": "b" * 40,
                    "certora_dir": "certora/"},
        "main_contract": "src/Vault.sol:Vault",
        "property_suite": "certora/properties/full.property-suite.json",
        "skill_version": "0.8.0",
        "generated_at": "2026-11-26T00:00:00Z",
    },
    "findings": [{
        "id": "F1",
        "title": "Withdraw reverts after a fee change",
        "severity": "medium",
        "trust_class": "unprivileged",
        "description": "`withdraw` reverts when the fee rounds up.",
        "justification": {"pre_state": "x", "steps": [], "reachability": "x", "not_a_model_artefact": "x"},
        "severity_justification": {"impact": "medium", "likelihood": "low", "reasoning": "x"},
        "evidence": [{"property": "p", "how": "x", "rules": [{
            "rule": "r", "spec": "certora/s.spec", "conf": "certora/c.conf", "result": "violated",
            "job_url": "https://prover.certora.com/output/1/abc",
        }]}],
    }],
    "properties": [],
    "models": [{"name": "Oracle", "source": "interface and documentation only", "assumptions": [
        {"text": "The price is never zero.", "justified": True, "justification": "Documented."},
    ]}],
}
BYTES = json.dumps(RESULTS).encode()
REASON = "Because the code says so, at Vault.sol:42."


def review(**changes: Any) -> dict[str, Any]:
    return {
        "schema": "certora-autoprover-review/1",
        "tool": "reviewer 1.0",
        "generated_at": "2026-11-27T00:00:00Z",
        "results_sha256": hashlib.sha256(BYTES).hexdigest(),
        "findings": [{"id": "F1", "verdict": "valid", "severity": "medium", "reason": REASON}],
        "models": [{"name": "Oracle", "verdict": "faithful", "reason": REASON, "assumptions": [
            {"text": "The price is never zero.", "verdict": "justified", "reason": REASON},
        ]}],
        **changes,
    }


def problems(raw: dict[str, Any]) -> list[str]:
    return review_problems(Review.model_validate(raw), VerificationResults.model_validate(RESULTS), BYTES)


def test_a_review_of_the_file_has_no_problems() -> None:
    assert problems(review()) == []


@pytest.mark.parametrize(("verdict", "severity", "ok"), [
    ("valid", "medium", True), ("valid", None, False),
    ("minor", "low", True), ("minor", None, True),
    ("not_an_issue", None, True), ("not_an_issue", "low", False),
])
def test_the_severity_follows_the_verdict(verdict: str, severity: str | None, ok: bool) -> None:
    raw = review(findings=[{"id": "F1", "verdict": verdict, "severity": severity, "reason": REASON}])
    if ok:
        Review.model_validate(raw)
    else:
        with pytest.raises(ValidationError):
            Review.model_validate(raw)


def test_a_reason_has_at_least_20_characters() -> None:
    with pytest.raises(ValidationError, match="at least 20 characters"):
        Review.model_validate(review(findings=[{"id": "F1", "verdict": "minor", "severity": None,
                                                "reason": "too short"}]))


def test_a_review_of_another_version_of_the_file_is_rejected() -> None:
    assert problems(review(results_sha256="0" * 64)) == [
        "the review was made from another version of the results file"
    ]


def test_every_finding_model_and_assumption_has_exactly_one_verdict() -> None:
    verdict = {"id": "F2", "verdict": "minor", "severity": None, "reason": REASON}
    assert problems(review(findings=[verdict])) == [
        "no verdict for the findings F1", "verdicts for findings the results file does not list: F2",
    ]
    twice = review()["findings"] * 2
    assert problems(review(findings=twice)) == ["a finding has more than one verdict"]
    assert problems(review(models=[])) == ["no verdict for the models Oracle"]
    no_assumptions = [{"name": "Oracle", "verdict": "narrower", "reason": REASON, "assumptions": []}]
    assert problems(review(models=no_assumptions)) == [
        "model Oracle: no verdict for the assumptions The price is never zero."
    ]
