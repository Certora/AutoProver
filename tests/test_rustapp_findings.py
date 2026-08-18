"""What a Rust backend run reports as its findings.

Two rules meet here. First, a check the author declared expected-to-fail must reach the report as a
finding: ``expect_check_failure`` is how an author says "the failure here IS the finding" — the
counterexample is real and the property is kept precisely to record it. The publish gate accepts
such a check as clean and nothing downstream ever asks the run to reproduce it, so on the klend run
of 2026-08-10 four declared findings reached ``report.html`` — one as a violation (its campaign
happened to hit it first) and three as **"No counterexample"**, including one the author had
reproduced at iteration 11619. The declaration is the author's and the outcome is the wheel's; they
meet on `RustFormalResult`.

Second, those rows become `Finding`s without a model: the crash and the declaration are the
write-up. What must survive the mapping is the distinction between a finding this run reproduced
and one resting on the author's reading alone.
"""

import pathlib
from dataclasses import dataclass
from typing import Any, cast

import pytest

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.pipeline.ptypes import ComponentOutcome
from composer.rustapp import adapter
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.findings import compose_findings
from composer.rustapp.result import RustFormalResult
from composer.rustapp.results import summarize_verdicts
from composer.rustapp.wire import Verdict
from composer.spec.source.report.schema import Outcome, RuleVerdict
from tests.conftest import wire_descriptor

#: The real one, trimmed: what Crucible reported for the finding its campaign did hit.
COUNTEREXAMPLE = (
    "crash crash_93d45d29a130d36f: [stored_price_timestamp_not_in_future] reserve 8J5W stored "
    "market_price_last_updated_ts=2 which is ahead of the current unix timestamp 0\n"
    "reproducing sequence (iteration 11, 3 action(s)):\n  1. refresh_reserves_batch -> OK"
)

#: …and the reason the author gave for the one it did not, verbatim in shape.
REASON = "UpdateBlockPriceUsage only mark_stale()s, so PRICE_USAGE_ALLOWED survives the kill switch"


def _verdict(outcome: Outcome, detail: str | None = None) -> Verdict:
    return Verdict(outcome=outcome, line=None, duration_seconds=None, unit_file=None, detail=detail)


def _result(
    verdicts: dict[str, Verdict],
    declared: dict[str, str],
    checks: list[tuple[str, list[str]]] | None = None,
) -> RustFormalResult:
    return RustFormalResult(
        checks=checks if checks is not None else [(f"{name}_prop", [name]) for name in verdicts],
        verdicts=verdicts,
        expected_failures=declared,
    )


# ---------------------------------------------------------------------------
# The declaration fold
# ---------------------------------------------------------------------------

def test_a_declared_finding_the_run_reproduced_reports_bad_with_its_counterexample():
    reported = _result(
        {"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {"c_ts": REASON}
    ).reported_verdicts()

    assert reported["c_ts"].outcome is Outcome.BAD
    detail = reported["c_ts"].detail or ""
    # The declaration is what turns a bare violation into a *reported finding*, so it has to be
    # visible — a reader otherwise cannot tell a real bug from a check that needs fixing.
    assert REASON in detail
    # …and the evidence must survive intact. This is the case that already worked; the point here
    # is that folding the declaration in does not cost the counterexample.
    assert COUNTEREXAMPLE in detail


def test_a_declared_finding_the_run_did_not_reproduce_still_reports_as_a_finding():
    # The klend case. The campaign stopped at the first crash, ~11600 iterations before this check
    # would have fired, and the wheel — correctly, for what it observed — said GOOD.
    reported = _result(
        {"c_kill": _verdict(Outcome.GOOD)}, {"c_kill": REASON}
    ).reported_verdicts()

    assert reported["c_kill"].outcome is Outcome.BAD, "a documented finding must not read as a pass"
    detail = reported["c_kill"].detail or ""
    assert REASON in detail
    # It is a weaker claim than the reproduced case and must say so: no counterexample backs it.
    assert "NOT REPRODUCED" in detail
    assert "GOOD" in detail, "the run's own outcome is what makes the claim weaker; name it"


def test_an_undeclared_check_is_left_exactly_as_the_wheel_reported_it():
    # Attribution is the wheel's. This only ever adds the author's declaration on top; with none
    # made, every row is the wheel's verbatim — including a BAD it found on its own.
    verdicts = {
        "c_good": _verdict(Outcome.GOOD),
        "c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE),
        "c_err": _verdict(Outcome.ERROR, "linker died"),
    }
    assert _result(verdicts, {}).reported_verdicts() == verdicts


def test_a_declared_check_whose_run_errored_keeps_the_error_text():
    # ERROR/TIMEOUT is neither a reproduction nor a clean run. It reports as the declared finding
    # like any other, but throwing away what went wrong would leave the row unexplainable.
    reported = _result(
        {"c_kill": _verdict(Outcome.ERROR, "harness build timed out")}, {"c_kill": REASON}
    ).reported_verdicts()

    assert reported["c_kill"].outcome is Outcome.BAD
    detail = reported["c_kill"].detail or ""
    assert "NOT REPRODUCED" in detail and "ERROR" in detail
    assert "harness build timed out" in detail


def test_a_declaration_naming_a_check_that_did_not_run_adds_no_row():
    # An author can mark a check and then skip its property; the marking survives in session state.
    # Rows come from what ran, so the stale declaration must not conjure one.
    reported = _result({"c_good": _verdict(Outcome.GOOD)}, {"c_gone": REASON}).reported_verdicts()

    assert list(reported) == ["c_good"]
    assert reported["c_good"].outcome is Outcome.GOOD


# ---------------------------------------------------------------------------
# Both consumers of the fold
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Formalized:
    """The ``Formalized`` protocol the report's collector reads."""

    result: RustFormalResult
    unit_file: str = "main.rs"
    run_link: str | None = None


@pytest.mark.asyncio
async def test_the_report_gets_the_declared_finding():
    formalizer = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    result = _result(
        {"c_kill": _verdict(Outcome.GOOD), "c_plain": _verdict(Outcome.GOOD)}, {"c_kill": REASON}
    )
    verdicts = await formalizer.fetch_verdicts(cast(Any, _Formalized(result)))

    assert verdicts["c_kill"].outcome is Outcome.BAD
    assert REASON in (verdicts["c_kill"].message or "")
    # The neighbours of a declared check are untouched — this is not a component-wide verdict.
    assert verdicts["c_plain"].outcome is Outcome.GOOD


@dataclass
class _Feat:
    display_name: str
    slug: str = "oracle"


def test_the_console_rollup_agrees_with_the_report():
    # Two views of one run. A row that reads "Violated" in report.html and "Verified" in the
    # terminal is worse than either being wrong on its own.
    result = _result(
        {"c_kill": _verdict(Outcome.GOOD), "c_plain": _verdict(Outcome.GOOD)}, {"c_kill": REASON}
    )
    summary = summarize_verdicts(
        CorePipelineResult(
            n_components=1, n_properties=0, failures=[],
            outcomes=[
                ComponentOutcome(
                    cast(Any, _Feat("Oracle-Driven Refresh")), [],
                    Delivered(result, pathlib.Path("main.rs")),
                )
            ],
        ),
        "prover",
    )

    assert [(v.name, v.outcome) for v in summary.verdicts] == [
        ("c_kill_prop", Outcome.BAD),
        ("c_plain_prop", Outcome.GOOD),
    ]


@pytest.mark.asyncio
async def test_a_wheels_own_file_beats_the_components_fallback():
    """A verdict that names its own source file keeps it, and only a verdict that names none falls
    back to the component's artifact.

    The report identifies a rule row by ``(file, name)`` so that one definition seen through several
    runs collapses into a single row. A callout-mode wheel delivers ONE artifact, so every component
    shares that fallback name — and two components whose authors named an invariant the same way
    (they are given the same property title, so they do) would collapse into one row, silently
    dropping the second component's verdict. The wheel says which of its sections a check came from;
    this is the wiring that lets it.
    """
    formalizer = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    located = _verdict(Outcome.BAD)
    located.unit_file = "c_lamport_custody.rs"
    result = _result({"c_authority_immutable": located, "c_unplaced": _verdict(Outcome.GOOD)}, {})
    verdicts = await formalizer.fetch_verdicts(cast(Any, _Formalized(result)))
    assert verdicts["c_authority_immutable"].unit_file == "c_lamport_custody.rs"
    assert verdicts["c_unplaced"].unit_file == "main.rs"


# ---------------------------------------------------------------------------
# Report rows -> findings
# ---------------------------------------------------------------------------

def _outcome(result: RustFormalResult, unit_file: str = "main.rs") -> ComponentOutcome:
    return ComponentOutcome(
        cast(Any, _Feat("Oracle-Driven Refresh")), [], Delivered(result, pathlib.Path(unit_file))
    )


def _rows(result: RustFormalResult, unit_file: str = "main.rs") -> list[RuleVerdict]:
    """The report rows ``collect`` would build from one component's reported verdicts."""
    return [
        RuleVerdict(name=name, spec_file=v.unit_file or unit_file, outcome=v.outcome,
                    message=v.detail)
        for name, v in result.reported_verdicts().items()
    ]


def test_a_reproduced_declared_finding_carries_its_counterexample_and_its_reason():
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {"c_ts": REASON},
                     checks=[("Stored prices are never in the future", ["c_ts"])])
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])

    assert len(findings) == 1
    f = findings[0]
    # Named by the property it verifies, not by the check.
    assert f.title == "Stored prices are never in the future"
    # The run produced this counterexample, so it is the proof of concept.
    assert f.content.proof_of_concept == COUNTEREXAMPLE
    assert REASON in f.content.description
    assert f.provenance is not None and f.provenance.risk_reasoning == REASON
    assert f.provenance.rule_name == "c_ts" and f.provenance.outcome is Outcome.BAD


def test_an_unreproduced_declared_finding_claims_no_counterexample():
    result = _result({"c_kill": _verdict(Outcome.GOOD)}, {"c_kill": REASON},
                     checks=[("The kill switch stops price usage", ["c_kill"])])
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])

    assert len(findings) == 1
    f = findings[0]
    # The whole point of the row: it IS a finding, and it says the evidence is the author's.
    assert "NOT REPRODUCED" in f.content.description
    assert f.content.proof_of_concept is None
    assert f.provenance is not None and f.provenance.risk_reasoning == REASON


def test_a_crash_the_run_found_needs_no_declaration():
    result = _result({"c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept == COUNTEREXAMPLE
    # Nothing here was declared, so there is no author's reason to record.
    assert findings[0].provenance is not None
    assert findings[0].provenance.risk_reasoning is None


def test_findings_assess_no_risk():
    """Severity and impact stay blank: nothing in this pipeline has judged what a crash is worth,
    and a fabricated 'high' would be worse than an honest blank."""
    result = _result({"c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    f = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])[0]
    assert f.severity == "informational"
    assert f.content.impact == ""


def test_a_check_verifying_several_properties_is_named_after_itself():
    result = _result({"c_multi": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {},
                     checks=[("Prices are fresh", ["c_multi"]), ("Prices are signed", ["c_multi"])])
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])
    # No single property title names this row, so the check's own name is the only unambiguous one.
    assert findings[0].title == "c_multi"


def test_only_violations_become_findings():
    """ERROR and TIMEOUT stay verdict rows — a check that could not be run is a coverage gap, not
    something the run found."""
    result = _result(
        {
            "c_good": _verdict(Outcome.GOOD),
            "c_err": _verdict(Outcome.ERROR, "linker died"),
            "c_slow": _verdict(Outcome.TIMEOUT, "gave up after 30m"),
            "c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE),
        },
        {},
    )
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])
    assert [f.provenance.rule_name for f in findings if f.provenance] == ["c_bad"]


def test_a_clean_campaign_produces_no_findings():
    """Not a "no findings" placeholder — the empty list is what makes the report omit the section."""
    result = _result({"c_good": _verdict(Outcome.GOOD)}, {})
    assert compose_findings(rules=_rows(result), outcomes=[_outcome(result)]) == []


def test_two_components_sharing_one_artifact_keep_their_own_evidence():
    """A callout-mode wheel delivers one file, so both components fall back to the same unit file
    and only the check name separates their rows. Each finding must still get its own component's
    declaration and counterexample."""
    left = _result({"c_a": _verdict(Outcome.BAD, "crash A")}, {})
    right = _result({"c_b": _verdict(Outcome.GOOD)}, {"c_b": REASON})
    findings = compose_findings(
        rules=_rows(left) + _rows(right), outcomes=[_outcome(left), _outcome(right)],
    )
    by_rule = {f.provenance.rule_name: f for f in findings if f.provenance}
    assert by_rule["c_a"].content.proof_of_concept == "crash A"
    assert by_rule["c_a"].provenance is not None and by_rule["c_a"].provenance.risk_reasoning is None
    assert by_rule["c_b"].content.proof_of_concept is None
    assert by_rule["c_b"].provenance is not None
    assert by_rule["c_b"].provenance.risk_reasoning == REASON


@pytest.mark.asyncio
async def test_the_formalizer_submits_them_through_the_report_hook():
    """The seam itself: the report asks the backend for findings and gets ready ones — no evidence
    protocol, no model, nothing for the report layer to synthesize."""
    formalizer = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {"c_ts": REASON},
                     checks=[("Stored prices are never in the future", ["c_ts"])])
    findings = await formalizer.findings(
        contract_name="klend", rules=_rows(result), properties=[], groups=[],
        outcomes=[_outcome(result)], run=cast(Any, object()),
    )

    assert [f.title for f in findings] == ["Stored prices are never in the future"]
    assert findings[0].content.proof_of_concept == COUNTEREXAMPLE
