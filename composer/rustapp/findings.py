"""Map a Rust run's BAD rows to report ``Finding``s.

No second write-up: the wheel's crash (and reproducing sequence) and the author's
``expect_check_failure`` reason already are the finding. Severity is ``informational``
and impact is empty — nothing here has judged what a fuzzer crash is worth.
"""

from dataclasses import dataclass

from composer.pipeline.ptypes import ComponentOutcome, Delivered
from composer.rustapp.result import RustFormalResult
from composer.spec.source.report.schema import (
    Finding, FindingProvenance, IssueContent, Outcome, RuleRef, RuleVerdict,
)
from composer.spec.system_model import FeatureUnit

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]


@dataclass(frozen=True)
class _Observed:
    """One delivered check, still split into the run's outcome and the author's declaration."""

    ran: Outcome           # wheel's outcome; BAD only if this run produced a counterexample
    detail: str | None     # wheel's counterexample or error text
    declared: str | None   # author's expect_check_failure reason, if any
    title: str             # same name as the console / report verdict row


def _observations(outcomes: RustOutcomes) -> dict[RuleRef, _Observed]:
    """Every delivered check, keyed as the report keys its rows: ``(file, name)``.

    Rebuilt rather than looked up: ``collect`` uses the verdict's file, falling back
    to the component artifact, and a callout-mode wheel gives every component that
    same fallback — so the check name alone does not identify a row.

    First component naming a key wins, as in ``collect``: where two of them do collapse
    onto one row, the evidence here has to be the same run's as the message there."""
    observed: dict[RuleRef, _Observed] = {}
    for o in outcomes:
        if not isinstance(o.result, Delivered):
            continue
        res = o.result.result
        for check, verdict in res.verdicts.items():
            ref = (verdict.unit_file or o.result.unit_file, check)
            observed.setdefault(ref, _Observed(
                ran=verdict.outcome,
                detail=verdict.detail,
                declared=res.expected_failures.get(check),
                title=res.display_name(check),
            ))
    return observed


def _summary(description: str) -> str:
    """First non-empty line of the description."""
    return next((line.strip() for line in description.splitlines() if line.strip()), "")


def _finding(rule: RuleVerdict, observed: _Observed | None) -> Finding:
    # Description is the row message. Proof of concept is only the wheel's text, and
    # only when this run produced a violation.
    description = rule.message or ""
    proof = observed.detail if observed is not None and observed.ran is Outcome.BAD else None
    declared = observed.declared if observed is not None else None
    return Finding(
        title=observed.title if observed is not None else rule.name,
        severity="informational",
        content=IssueContent(
            summary=_summary(description),
            description=description,
            impact="",
            proof_of_concept=proof,
            references=[rule.prover_link] if rule.prover_link else None,
        ),
        provenance=FindingProvenance(
            rule_name=rule.name,
            spec_file=rule.spec_file,
            outcome=rule.outcome,
            prover_link=rule.prover_link,
            risk_reasoning=declared,
        ),
    )


def compose_findings(*, rules: list[RuleVerdict], outcomes: RustOutcomes) -> list[Finding]:
    """One ``Finding`` per BAD row, in report order.

    Includes a declared check this run did not reproduce. ERROR and TIMEOUT stay
    in the verdict table: a check that never ran is a coverage gap, not a finding."""
    observed = _observations(outcomes)
    return [
        _finding(rule, observed.get(rule.ref)) for rule in rules if rule.outcome is Outcome.BAD
    ]
