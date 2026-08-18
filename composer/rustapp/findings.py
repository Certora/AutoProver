"""What this run found, as report `Finding`\\ s — no second model, no second write-up.

A Rust backend's findings are already written by the time the report asks for them. The wheel
reported a crash with its reproducing sequence, or the author declared a check expected to fail and
said why (``expect_check_failure``); :meth:`RustFormalResult.reported_verdicts` folded those two
together, and ``fetch_verdicts`` put the result on the report's rows. This maps that into the
report's audit-issue shape.

Deliberately not an assessment. Severity is ``informational`` and ``impact`` is empty on every
finding here, because nothing in this pipeline has judged a fuzzer crash's real-world consequence —
a fabricated ``high`` would be worse than an honest blank. The two fields exist for a later pass
that actually assesses risk.
"""

from dataclasses import dataclass

from composer.pipeline.ptypes import ComponentOutcome, Delivered
from composer.rustapp.result import RustFormalResult
from composer.spec.source.report.schema import (
    Finding, FindingProvenance, FormalizedProperty, IssueContent, Outcome, PropertyTitle, RuleRef,
    RuleVerdict,
)
from composer.spec.system_model import FeatureUnit

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]


@dataclass(frozen=True)
class _Observed:
    """This run's own record of one reported check, before ``fetch_verdicts`` flattened it into a
    single `RuleVerdict.message`. Kept structured so the mapper reads the declaration and the
    counterexample as fields rather than recovering them from rendered text."""

    #: What the wheel mechanically observed — BAD only when this run actually produced a
    #: counterexample, which a declared finding does not require.
    ran: Outcome
    #: The wheel's counterexample (BAD) or error text, verbatim.
    detail: str | None
    #: Why the author declared a failure here to be the finding, when they declared one.
    declared: str | None


def _observations(outcomes: RustOutcomes) -> dict[RuleRef, _Observed]:
    """Every delivered check, keyed the way the report identifies its rows.

    The key has to be rebuilt rather than looked up: ``collect`` derives ``(file, name)`` from the
    fetched verdict's own file with the component's artifact as the fallback, and a callout-mode
    wheel gives every component that same fallback — so the check name alone does not identify a
    row."""
    observed: dict[RuleRef, _Observed] = {}
    for o in outcomes:
        if not isinstance(o.result, Delivered):
            continue
        res = o.result.result
        for check, verdict in res.verdicts.items():
            ref = (verdict.unit_file or o.result.unit_file, check)
            observed[ref] = _Observed(
                ran=verdict.outcome,
                detail=verdict.detail,
                declared=res.expected_failures.get(check),
            )
    return observed


def _titles(properties: list[FormalizedProperty]) -> dict[RuleRef, PropertyTitle]:
    """The property title to name a row by: only where the check verifies exactly one. A check that
    discharges several has no single title to be called after, and one the author mapped to none has
    no title at all — both fall back to the check's own name."""
    by_ref: dict[RuleRef, list[PropertyTitle]] = {}
    for p in properties:
        for ref in p.rule_refs:
            by_ref.setdefault(ref, []).append(p.title)
    return {ref: titles[0] for ref, titles in by_ref.items() if len(titles) == 1}


def _summary(description: str) -> str:
    """The description's opening line. Both shapes lead with the sentence that says what happened —
    the declaration's reason, or the crash line above its reproducing sequence — so this is a real
    first sentence rather than an invented one."""
    return next((line.strip() for line in description.splitlines() if line.strip()), "")


def _finding(
    rule: RuleVerdict, title: PropertyTitle | None, observed: _Observed | None
) -> Finding:
    # ``rule.message`` is the description because it is already the reader-facing account: the
    # declaration's reason leads it, the NOT REPRODUCED caveat follows when this run reached no
    # counterexample, and the evidence comes last. The proof of concept is narrower — the wheel's
    # own text, and only where the run actually produced a violation, so an unreproduced finding
    # never displays a counterexample it does not have.
    description = rule.message or ""
    proof = observed.detail if observed is not None and observed.ran is Outcome.BAD else None
    declared = observed.declared if observed is not None else None
    return Finding(
        title=title or rule.name,
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
            # Carried as a field so a reader can tell a declared finding from one the run tripped
            # over — and a reproduced one from a NOT REPRODUCED one — without reading the evidence.
            risk_reasoning=declared,
        ),
    )


def compose_findings(
    *,
    rules: list[RuleVerdict],
    properties: list[FormalizedProperty],
    outcomes: RustOutcomes,
) -> list[Finding]:
    """One `Finding` per violated check, in report order.

    BAD only, which after the expected-failure fold already includes the checks an author declared
    and this run did not reproduce. ERROR and TIMEOUT stay verdict-table rows: a check that could
    not be run is a gap in coverage, not something the run found."""
    titles = _titles(properties)
    observed = _observations(outcomes)
    return [
        _finding(rule, titles.get(rule.ref), observed.get(rule.ref))
        for rule in rules
        if rule.outcome is Outcome.BAD
    ]
