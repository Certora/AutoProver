"""Tests for the console/TUI verdict rollup (``composer/rustapp/results.py``).

``report.json`` is the canonical results artifact, but it is written to disk — what a human watching
a run *sees* at the end is this rollup, so it has to account for every check that ran. The unit of
a verdict is a **unit**, not a component: ``units()`` is one unit per property, so a component with
five properties bakes five verdicts and owes five rows.

Stubs throughout — no pipeline, no wheel.
"""

import pathlib
from dataclasses import dataclass

from composer.pipeline.core import CorePipelineResult, Delivered, GaveUp
from composer.pipeline.ptypes import ComponentOutcome
from composer.rustapp.result import RustFormalResult
from composer.rustapp.results import format_verdict_lines, summarize_verdicts
from composer.rustapp.wire import Verdict
from composer.spec.source.report.schema import Outcome

#: Any report backend does — the point of these tests is that the rollup reads its wording out of
#: the report's own per-backend table rather than spelling outcomes itself.
BACKEND = "prover"


@dataclass
class _Feat:
    """The one ``FeatureUnit`` member the rollup reads."""

    display_name: str
    slug: str = "component"


def _delivered(**verdicts: Outcome) -> Delivered[RustFormalResult]:
    """A delivered component whose units check one property each, named ``<unit>_prop``."""
    return Delivered(
        RustFormalResult(
            checks=[(f"{name}_prop", [name]) for name in verdicts],
            verdicts={unit: Verdict.with_outcome(outcome) for unit, outcome in verdicts.items()},
        ),
        pathlib.Path("harness.rs"),
    )


def _result(*outcomes: ComponentOutcome) -> CorePipelineResult[RustFormalResult]:
    return CorePipelineResult(
        n_components=len(outcomes), n_properties=0, outcomes=list(outcomes), failures=[]
    )


def test_every_unit_of_a_component_gets_a_row():
    # A component with several properties bakes several verdicts. Reporting only the first would
    # read as "1 Verified" where three checks ran, and would hide the failing one.
    result = _result(
        ComponentOutcome(
            _Feat("increment"), [],
            _delivered(rule_a=Outcome.GOOD, rule_b=Outcome.BAD, rule_c=Outcome.GOOD),
        )
    )
    summary = summarize_verdicts(result, BACKEND)

    assert [(v.name, v.outcome) for v in summary.verdicts] == [
        ("rule_a_prop", Outcome.GOOD),
        ("rule_b_prop", Outcome.BAD),
        ("rule_c_prop", Outcome.GOOD),
    ]
    assert summary.counts == {Outcome.GOOD: 2, Outcome.BAD: 1}
    assert summary.tally == "2 Verified, 1 Violated"


def test_rows_are_named_by_property_title_falling_back_to_the_unit():
    # The property's own words read better than the backend's unit name; a unit with no title in
    # ``units`` (a wheel that reports a verdict for something it never declared) still gets a row.
    delivered = _delivered(rule_a=Outcome.GOOD)
    delivered.result.verdicts["orphan"] = Verdict.with_outcome(Outcome.ERROR)
    summary = summarize_verdicts(_result(ComponentOutcome(_Feat("c"), [], delivered)), BACKEND)

    assert [v.name for v in summary.verdicts] == ["rule_a_prop", "orphan"]


def test_a_delivered_component_with_no_baked_verdict_is_still_listed():
    # A run-service-backed wheel reports through ``fetch_verdicts`` and bakes nothing; the component
    # must not vanish from the listing.
    result = _result(
        ComponentOutcome(_Feat("no-verdicts"), [], Delivered(RustFormalResult(), pathlib.Path("h.rs")))
    )
    summary = summarize_verdicts(result, BACKEND)

    assert [(v.name, v.outcome) for v in summary.verdicts] == [("no-verdicts", Outcome.UNKNOWN)]


def test_give_ups_are_left_to_the_failures_block():
    result = _result(
        ComponentOutcome(_Feat("gave-up"), [], GaveUp(reason="7 attempts")),
        ComponentOutcome(_Feat("crashed"), [], RuntimeError("boom")),
        ComponentOutcome(_Feat("ok"), [], _delivered(rule_a=Outcome.GOOD)),
    )
    summary = summarize_verdicts(result, BACKEND)

    assert [v.name for v in summary.verdicts] == ["rule_a_prop"]


def test_the_listing_uses_the_reports_own_wording():
    result = _result(
        ComponentOutcome(_Feat("c"), [], _delivered(rule_a=Outcome.GOOD, rule_b=Outcome.TIMEOUT))
    )
    lines = format_verdict_lines(summarize_verdicts(result, BACKEND))

    assert lines[0] == "  Verdicts:     1 Verified, 1 Timeout"
    assert lines[1:] == [
        "    ✓ rule_a_prop — Verified",
        "    ⧖ rule_b_prop — Timeout",
    ]


def test_nothing_delivered_prints_nothing():
    assert format_verdict_lines(summarize_verdicts(_result(), BACKEND)) == []
