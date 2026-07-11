"""The console/TUI verdict rollup (``composer.crucible.results``) — no wheel / LLM.

A completed Crucible run bakes a per-invariant ``Outcome`` into each delivered result
(``RustFormalResult.verdicts``). These tests pin how that rollup renders in the console
counts block: a tally line in the report's crucible vocabulary ("No counterexample" /
"Counterexample") plus a per-invariant listing, give-ups excluded (they live in
``failures``), and nothing at all when no invariant was delivered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from composer.crucible.results import (
    format_verdict_lines,
    summarize_verdicts,
)
from composer.pipeline.core import ComponentOutcome, CorePipelineResult, Delivered, GaveUp
from composer.rustapp.result import RustFormalResult
from composer.spec.source.report.schema import Outcome


class _Feat:
    """Minimal duck-typed unit — the rollup only reads ``display_name``."""

    def __init__(self, name: str):
        self.display_name = name


def _feat(name: str) -> Any:
    # The rollup only reads ``display_name``; a real ContractComponentInstance is overkill.
    return cast(Any, _Feat(name))


def _delivered(name: str, outcome: str) -> ComponentOutcome:
    res = RustFormalResult(verdicts={name: {"outcome": outcome}})
    return ComponentOutcome(_feat(name), [], Delivered(res, Path(f"fuzz/{name}.rs")))


def _gave_up(name: str) -> ComponentOutcome:
    return ComponentOutcome(_feat(name), [], GaveUp(reason="did not compile"))


def _result(*outcomes: ComponentOutcome) -> CorePipelineResult:
    return CorePipelineResult(len(outcomes), len(outcomes), list(outcomes), [])


def test_tally_uses_crucible_labels_and_counts():
    result = _result(
        _delivered("conservation", "GOOD"),
        _delivered("solvency", "BAD"),
        _delivered("bounds", "GOOD"),
    )
    summary = summarize_verdicts(result)
    assert summary.counts == {Outcome.GOOD: 2, Outcome.BAD: 1}
    # GOOD is listed before BAD (display order), with the crucible wording.
    assert summary.tally == "2 No counterexample, 1 Counterexample"


def test_lines_have_tally_then_per_invariant_listing():
    result = _result(_delivered("solvency", "BAD"), _delivered("bounds", "GOOD"))
    lines = format_verdict_lines(summarize_verdicts(result))
    # The tally is outcome-ordered (GOOD before BAD); the listing keeps pipeline order.
    assert lines[0] == "  Verdicts:     1 No counterexample, 1 Counterexample"
    assert lines[1] == "    ✗ solvency — Counterexample"
    assert lines[2] == "    ✓ bounds — No counterexample"


def test_gave_up_invariants_are_excluded():
    # Give-ups are surfaced in `failures`, not as verdicts.
    result = _result(_delivered("bounds", "GOOD"), _gave_up("solvency"))
    summary = summarize_verdicts(result)
    assert [v.name for v in summary.verdicts] == ["bounds"]


def test_no_delivered_verdicts_renders_nothing():
    assert format_verdict_lines(summarize_verdicts(_result(_gave_up("x")))) == []
    assert format_verdict_lines(summarize_verdicts(_result())) == []


def test_delivered_without_baked_verdict_is_unknown():
    res = RustFormalResult(verdicts={})
    outcome = ComponentOutcome(_feat("x"), [], Delivered(res, Path("fuzz/x.rs")))
    summary = summarize_verdicts(_result(outcome))
    assert summary.counts == {Outcome.UNKNOWN: 1}
