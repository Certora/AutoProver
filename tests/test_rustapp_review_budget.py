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

import json
import pathlib
from typing import Any, cast

import pytest

from composer.pipeline.core import GaveUp

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


# ---------------------------------------------------------------------------
# A turn that produced nothing. `run_llm_agent` returns None when the agent ended without ever
# calling the result tool — it used to be JSON-dumped, so the caller received the literal string
# "null" and treated it as the authored artifact.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_authoring_turn_with_no_artifact_costs_an_attempt_but_never_reaches_the_toolchain(
    monkeypatch,
):
    from composer.rustapp import adapter
    from composer.rustapp.wire import AuthorInput

    compiled: list[str] = []

    class _Wheel:
        def author_prompt(self, _input_json, failure_json):
            self.last_failure = failure_json
            return '{"instruction": "author it"}'

        def compile(self, _input_json, spec, _workdir, _sandbox_json):
            compiled.append(spec)
            return '{"status": "ok"}'

        def judge_prompt(self, _input_json, _spec):
            return None

    wheel = _Wheel()
    turns = 0

    async def no_result(*_a, **_kw) -> str | None:
        nonlocal turns
        turns += 1
        return None  # the agent explored and stopped without calling `result`

    monkeypatch.setattr(adapter, "run_llm_agent", no_result)
    outcome = await adapter.author_and_compile(
        cast(Any, wheel),
        AuthorInput(kind="setup", program="vault"),
        env=cast(Any, None), sandbox_dict={"argv_prefix": [], "timeout_s": 1},
        workdir=pathlib.Path("/nonexistent"), recursion_limit=4, backend_name="t",
        emit=lambda *_a: None, max_attempts=2,
    )

    # Every attempt is spent, and the loop gives up — but nothing was ever handed to `compile`,
    # because there was no draft to build. ("null" used to be.)
    assert isinstance(outcome, GaveUp)
    assert turns == 2
    assert compiled == []
    # …and the next turn is told what went wrong, rather than being asked to revise a draft of "null".
    assert "without calling the `result` tool" in json.loads(wheel.last_failure)["errors"]
