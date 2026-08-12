"""A check the author declared expected-to-fail must reach the report as a finding.

``expect_check_failure`` is how an author says "the failure here IS the finding" — klend makes no
such guarantee, the counterexample is real, and the property is being kept precisely to record that.
The publish gate accepts such a check as clean, and nothing downstream ever asked the run to
reproduce it. So on the klend run of 2026-08-10 four declared findings reached ``report.html``:
one as a violation (its campaign happened to hit it first) and three as **"No counterexample"** —
including ``block_price_usage_kill_switch_effective``, which the author had reproduced at iteration
11619 and whose commentary documents the reproducing sequence.

The declaration is the author's and the outcome is the wheel's; they meet on ``RustFormalResult``,
and these pin down that meeting for both consumers of it — the HTML report and the console rollup,
which must not disagree about whether a run found something.
"""

import pathlib
from dataclasses import dataclass
from typing import Any, cast

import pytest

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.pipeline.ptypes import ComponentOutcome
from composer.rustapp import adapter
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.result import RustFormalResult
from composer.rustapp.results import summarize_verdicts
from composer.rustapp.wire import Verdict
from composer.spec.source.report.collect import ReportComponentInput
from composer.spec.source.report.schema import Outcome
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


def _result(verdicts: dict[str, Verdict], declared: dict[str, str]) -> RustFormalResult:
    return RustFormalResult(
        checks=[(f"{name}_prop", [name]) for name in verdicts],
        verdicts=verdicts,
        expected_failures=declared,
    )


# ---------------------------------------------------------------------------
# The rule
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
# Both consumers
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
    verdicts = await formalizer.fetch_verdicts(
        cast(
            ReportComponentInput[RustFormalResult],
            ReportComponentInput(name="Oracle-Driven Refresh", props=[],
                                 formalized=cast(Any, _Formalized(result))),
        )
    )

    assert verdicts["c_kill"].outcome is Outcome.BAD
    assert REASON in (verdicts["c_kill"].message or "")
    # The neighbours of a declared check are untouched — this is not a component-wide verdict.
    assert verdicts["c_plain"].outcome is Outcome.GOOD


def test_the_console_rollup_agrees_with_the_report():
    # Two views of one run. A row that reads "Violated" in report.html and "Verified" in the
    # terminal is worse than either being wrong on its own.
    @dataclass
    class _Feat:
        display_name: str
        slug: str = "oracle"

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
    verdicts = await formalizer.fetch_verdicts(
        cast(
            ReportComponentInput[RustFormalResult],
            ReportComponentInput(name="Lamport Custody", props=[],
                                 formalized=cast(Any, _Formalized(result))),
        )
    )
    assert verdicts["c_authority_immutable"].unit_file == "c_lamport_custody.rs"
    assert verdicts["c_unplaced"].unit_file == "main.rs"
