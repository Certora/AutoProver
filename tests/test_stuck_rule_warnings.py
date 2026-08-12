"""Regression tests for :func:`stuck_rule_warnings` — the prover author's "you have been
stuck on this rule for three runs" detector.

The loop this covers used to be inline in ``verify_spec`` and carried three defects:

1. it iterated ``stuck_count.keys()`` while deleting from it, so any run that broke a
   rule's streak crashed the whole CVL-generation task with ``RuntimeError: dictionary
   changed size during iteration``;
2. it never advanced the history index on the ``run`` branch, so it re-counted the *same*
   prover run until the threshold tripped — every first stuck run nagged as though it had
   already failed identically three times;
3. it did ``del stuck_count[r]`` for previously-nagged rules, which ``KeyError``s whenever
   a rule that was nagged before isn't stuck now.
"""

from composer.prover.ptypes import RulePath
from composer.spec.source.prover import (
    NagMarker, ProverHistoryItem, ProverRunLog, STUCK_RULE_NAG_THRESHOLD,
    stuck_rule_warnings,
)

R1 = RulePath(rule="r1")
R2 = RulePath(rule="r2")


def _run(*results: tuple[RulePath, str], tc_id: str = "tc", rules: list[str] | None = None) -> ProverHistoryItem:
    return ProverRunLog(
        tool_call_id=tc_id,
        prover_results=list(results),  # type: ignore[arg-type]
        rules=rules,
        spec_digest="d",
        sort="run",
    )


def _warn(stuck, history, known=("tc",)):
    return stuck_rule_warnings(stuck, history, set(known))


def test_threshold_counts_the_current_run_plus_two_priors() -> None:
    # Defect 2: a single stuck run must NOT nag. The tally starts at 1 for the run being
    # processed, so it takes two prior identical failures to reach the threshold.
    assert STUCK_RULE_NAG_THRESHOLD == 3

    stuck = {R1: "TIMEOUT"}
    assert _warn(stuck, [])[0] == set()
    assert _warn(stuck, [_run((R1, "TIMEOUT"))])[0] == set()
    assert _warn(stuck, [_run((R1, "TIMEOUT")), _run((R1, "TIMEOUT"))])[0] == {R1}


def test_a_broken_streak_drops_the_rule() -> None:
    # Defect 1: this is the shape that used to raise RuntimeError — a rule leaves the
    # tally mid-iteration because an earlier run did not fail identically.
    stuck = {R1: "TIMEOUT"}
    history = [
        _run((R1, "VIOLATED")),   # oldest: different status, breaks the streak
        _run((R1, "TIMEOUT")),
    ]
    assert _warn(stuck, history)[0] == set()

    # A differing status anywhere in the walk-back stops the count, even with plenty of
    # older identical failures behind it.
    history = [
        _run((R1, "TIMEOUT")), _run((R1, "TIMEOUT")), _run((R1, "TIMEOUT")),
        _run((R1, "VIOLATED")),
        _run((R1, "TIMEOUT")),
    ]
    assert _warn(stuck, history)[0] == set()


def test_rules_are_tallied_independently() -> None:
    # Exercises the delete-during-iteration path with a survivor alongside it.
    stuck = {R1: "TIMEOUT", R2: "ERROR"}
    history = [
        _run((R1, "TIMEOUT"), (R2, "VIOLATED")),
        _run((R1, "TIMEOUT"), (R2, "ERROR")),
    ]
    assert _warn(stuck, history)[0] == {R1}


def test_targeted_runs_are_transparent_to_untouched_rules() -> None:
    # A re-run scoped to r2 neither extends nor breaks r1's streak.
    stuck = {R1: "TIMEOUT"}
    history = [
        _run((R1, "TIMEOUT")),
        _run((R2, "ERROR"), rules=["r2"]),
        _run((R1, "TIMEOUT")),
    ]
    assert _warn(stuck, history)[0] == {R1}


def test_nag_marker_restarts_the_streak() -> None:
    stuck = {R1: "TIMEOUT"}
    history = [
        _run((R1, "TIMEOUT")),
        _run((R1, "TIMEOUT")),
        NagMarker(nagged_rules=[R1], sort="nag"),
        _run((R1, "TIMEOUT")),
    ]
    # Without the marker this would be four identical failures; the marker means R1 was
    # already warned about, so the author isn't nagged again for the same stretch.
    assert _warn(stuck, history)[0] == set()


def test_nag_marker_for_a_rule_that_is_no_longer_stuck() -> None:
    # Defect 3: R2 was nagged previously but only R1 is stuck now — must not KeyError.
    stuck = {R1: "TIMEOUT"}
    history = [
        _run((R1, "TIMEOUT")),
        NagMarker(nagged_rules=[R2], sort="nag"),
        _run((R1, "TIMEOUT")),
    ]
    assert _warn(stuck, history)[0] == {R1}


def test_reports_history_older_than_the_last_compaction() -> None:
    stuck = {R1: "TIMEOUT"}
    visible = [_run((R1, "TIMEOUT"), tc_id="tc"), _run((R1, "TIMEOUT"), tc_id="tc")]
    assert _warn(stuck, visible, known=("tc",)) == ({R1}, False)

    compacted = [_run((R1, "TIMEOUT"), tc_id="gone"), _run((R1, "TIMEOUT"), tc_id="tc")]
    assert _warn(stuck, compacted, known=("tc",)) == ({R1}, True)


def test_no_stuck_rules_is_a_no_op() -> None:
    assert _warn({}, [_run((R1, "TIMEOUT"))]) == (set(), False)
