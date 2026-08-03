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
    Accepted,
    Rejected,
    Review,
    _budgeted,
    _JudgedAuthorState,
    _parse_judge,
    _review_gate,
    _ReviewBudget,
)


def _state(**kw) -> _JudgedAuthorState:
    """The author-session state the gate reads (messages are irrelevant to it)."""
    return cast(_JudgedAuthorState, kw)


DRAFT = "fn c_invariants(f: &mut Fixture) {}"


def _always_rejects(feedback: str = "vacuous assertion"):
    async def judge(_draft: str) -> Review:
        return Rejected(feedback)

    return judge


@pytest.mark.asyncio
async def test_a_rejecting_judge_stops_blocking_once_the_budget_is_spent():
    budget = _ReviewBudget()
    reviewed = _budgeted(_always_rejects(), budget)

    for round_no in range(1, MAX_REVIEW_ROUNDS):
        review = await reviewed(DRAFT)
        assert isinstance(review, Rejected), f"round {round_no} should still be a real rejection"
        # …and the gate keeps refusing `result`, naming what is left.
        blocked = _review_gate(_state(review_ok=False, reviewed_text=DRAFT), DRAFT, budget)
        assert blocked is not None and f"{budget.left} review(s) left" in blocked

    # The last round relents. The verdict really is an acceptance — the gate opens — but it carries
    # the objection, labelled as unresolved rather than papered over.
    review = await reviewed(DRAFT)
    assert isinstance(review, Accepted)
    assert "vacuous assertion" in review.feedback
    assert "No review rounds left" in review.feedback
    assert _review_gate(_state(), DRAFT, budget) is None


@pytest.mark.asyncio
async def test_an_accepted_draft_ends_the_loop_early_and_leaves_budget_unspent():
    budget = _ReviewBudget()

    async def accepts(_draft: str) -> Review:
        return Accepted()

    assert isinstance(await _budgeted(accepts, budget)(DRAFT), Accepted)
    assert budget.used == 1 and not budget.spent
    # The gate passes for that exact draft, and only that draft.
    assert _review_gate(_state(review_ok=True, reviewed_text=DRAFT), DRAFT, budget) is None
    assert _review_gate(_state(review_ok=True, reviewed_text=DRAFT), "other", budget) is not None


@pytest.mark.asyncio
async def test_the_gate_blocks_an_unreviewed_draft_while_budget_remains():
    budget = _ReviewBudget()
    # Nothing reviewed yet: `result` is refused and the author is told to review first.
    blocked = _review_gate(_state(), DRAFT, budget)
    assert blocked is not None and "request_review" in blocked
    # A draft the judge rejected is refused too, even though it *was* reviewed.
    assert _review_gate(_state(review_ok=False, reviewed_text=DRAFT), DRAFT, budget) is not None


# ---------------------------------------------------------------------------
# Reading the judge's reply. Two shapes reach here: the JSON a wheel's judge prompt asks for, and
# whatever prose the model produced when it ignored that instruction.
# ---------------------------------------------------------------------------


def test_a_json_verdict_is_authoritative():
    assert _parse_judge('{"accept": true, "feedback": "looks right"}') == Accepted("looks right")
    assert _parse_judge('{"accept": false, "feedback": "vacuous"}') == Rejected("vacuous")


def test_a_rejection_with_no_reason_still_says_something_to_revise_against():
    # An empty feedback string used to be handed to the next authoring turn as its revise context —
    # a round spent on "you were rejected" with no statement of why.
    review = _parse_judge('{"accept": false}')
    assert isinstance(review, Rejected) and review.feedback.strip()


def test_prose_is_read_by_its_leading_verdict():
    assert _parse_judge("REJECT — the invariant is vacuous") == Rejected(
        "REJECT — the invariant is vacuous"
    )
    assert isinstance(_parse_judge("ACCEPT, this covers the properties"), Accepted)


def test_prose_with_no_leading_verdict_is_taken_as_an_acceptance():
    # Deliberate, and the one behaviour here that is a policy choice rather than a mechanism: the
    # reviewer is advisory, in front of the compile/validate gates that actually decide, so an
    # unparseable reply lets the draft through instead of burning a revise round on a verdict nobody
    # stated. Pinned so flipping it has to be a deliberate edit to this test.
    assert isinstance(_parse_judge("I read the fixture and the properties."), Accepted)
