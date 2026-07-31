"""Tests for the in-loop review budget (``composer.rustapp.adapter``).

The judge runs *inside* the authoring session: the author calls `request_review`, revises against the
feedback, and the `result` gate blocks anything the judge hasn't accepted. That loop has to be
bounded, because it can be unwinnable — when the objection is something the author has no power to
change (a fixture it may not edit), "revise and re-review" never converges and just re-spends a
growing context per round until the graph's recursion limit. Observed in a real run: 11+ rounds at
~340k input tokens each, going nowhere.

So after a fixed number of rounds the draft goes forward with the concerns recorded. The compile gate
and the fuzzer still judge it; only the *reviewer* stops being able to block.
"""

from typing import cast

import pytest

from composer.rustapp.adapter import (
    MAX_REVIEW_ROUNDS,
    _budgeted,
    _JudgedAuthorState,
    _review_gate,
    _ReviewBudget,
)


def _state(**kw) -> _JudgedAuthorState:
    """The author-session state the gate reads (messages are irrelevant to it)."""
    return cast(_JudgedAuthorState, kw)


pytestmark = pytest.mark.asyncio

DRAFT = "fn c_invariants(f: &mut Fixture) {}"


def _always_rejects(feedback: str = "vacuous assertion"):
    async def judge(_draft: str) -> tuple[bool, str]:
        return False, feedback

    return judge


async def test_a_rejecting_judge_stops_blocking_once_the_budget_is_spent():
    budget = _ReviewBudget()
    reviewed = _budgeted(_always_rejects(), budget)

    for round_no in range(1, MAX_REVIEW_ROUNDS):
        ok, _feedback = await reviewed(DRAFT)
        assert ok is False, f"round {round_no} should still be a real rejection"
        # …and the gate keeps refusing `result`, naming what is left.
        blocked = _review_gate(_state(review_ok=False, reviewed_text=DRAFT), DRAFT, budget)
        assert blocked is not None and f"{budget.left} review(s) left" in blocked

    # The last round relents: the draft is submitted with the concerns attached, not silently.
    ok, feedback = await reviewed(DRAFT)
    assert ok is True
    assert "vacuous assertion" in feedback  # the objection is still reported…
    assert "No review rounds left" in feedback  # …labelled as unresolved, not as an acceptance
    assert _review_gate(_state(), DRAFT, budget) is None


async def test_an_accepted_draft_ends_the_loop_early_and_leaves_budget_unspent():
    budget = _ReviewBudget()

    async def accepts(_draft: str) -> tuple[bool, str]:
        return True, ""

    ok, _ = await _budgeted(accepts, budget)(DRAFT)
    assert ok is True
    assert budget.used == 1 and not budget.spent
    # The gate passes for that exact draft, and only that draft.
    assert _review_gate(_state(review_ok=True, reviewed_text=DRAFT), DRAFT, budget) is None
    assert _review_gate(_state(review_ok=True, reviewed_text=DRAFT), "other", budget) is not None


async def test_the_gate_blocks_an_unreviewed_draft_while_budget_remains():
    budget = _ReviewBudget()
    # Nothing reviewed yet: `result` is refused and the author is told to review first.
    blocked = _review_gate(_state(), DRAFT, budget)
    assert blocked is not None and "request_review" in blocked
    # A draft the judge rejected is refused too, even though it *was* reviewed.
    assert _review_gate(_state(review_ok=False, reviewed_text=DRAFT), DRAFT, budget) is not None
