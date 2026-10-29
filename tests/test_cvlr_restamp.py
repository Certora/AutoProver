"""Marking a rule re-decides the prover stamp against the last run, without another run.

A marking usually follows the run that showed the violation. If only a run could stamp, a unit that
ended on a new finding would have to submit its unchanged draft again before it could publish.
"""

import pytest

from composer.authoring.state import checked_digest, draft_digest
from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY, LastVerdicts, tuning_history
from composer.spec.cvlr.author_tools import ExpectRuleFailure, ExpectRulePassage

DRAFT = "#[rule] pub fn rule_a() {}\n#[rule] pub fn rule_b() {}\n"


def _state(draft: str = DRAFT, **extra) -> dict:
    return {
        "messages": [],
        "curr_spec": draft,
        "expected_failures": {},
        "skipped": [],
        "summaries": [],
        "vfs": {},
        "version_history": [],
        "prover_settings": TunableConf(),
        "required_validations": [PROVER_VALIDATION_KEY],
        "validations": {},
        **extra,
    }


def _stamp(state: dict) -> dict[str, str]:
    """The stamp the publish gate accepts for ``state``, markings included."""
    return {PROVER_VALIDATION_KEY: checked_digest(state, tuning_history(state))}  # type: ignore[arg-type]


def _marked(state: dict, **expected_failures: str) -> dict:
    return {**state, "expected_failures": expected_failures}


def _after_run(state: dict, status: dict[str, str], **verdicts) -> dict:
    """``state`` with the verdicts of a run on its current draft recorded."""
    last = LastVerdicts(
        status=status,
        incomplete=verdicts.get("incomplete", []),
        unverdicted=verdicts.get("unverdicted", []),
        draft=draft_digest(state, tuning_history(state)),  # type: ignore[arg-type]
    )
    return {**state, "last_verdicts": last}


async def _mark(state: dict, rule: str):
    return await ExpectRuleFailure(
        state=state, tool_call_id="tc", rule_name=rule, reason="the program pays the fee unbounded"
    ).run()


@pytest.mark.asyncio
async def test_marking_the_last_failure_stamps_the_draft_without_a_run():
    state = _after_run(_state(), {"rule_a": "VERIFIED", "rule_b": "VIOLATED"})

    out = await _mark(state, "rule_b")

    assert out.update["validations"] == _stamp(_marked(state, rule_b="real"))
    assert "stamped without another run" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_a_marking_that_leaves_a_failure_unaccounted_does_not_stamp():
    state = _after_run(_state(), {"rule_a": "VIOLATED", "rule_b": "VIOLATED"})

    out = await _mark(state, "rule_b")

    assert "validations" not in out.update
    assert out.update["expected_failures"] == {"rule_b": "the program pays the fee unbounded"}


@pytest.mark.asyncio
async def test_a_draft_edited_since_the_run_gains_no_stamp():
    state = {**_after_run(_state(), {"rule_b": "VIOLATED"}), "curr_spec": DRAFT + "// edited\n"}

    out = await _mark(state, "rule_b")

    assert "validations" not in out.update
    assert "stamped" not in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_an_incomplete_check_cannot_be_marked():
    """A stored mark would excuse a later real violation of the same rule."""
    base = _state()
    state = {
        **_after_run(base, {"rule_b": "VIOLATED"}, incomplete=["rule_b"]),
        "validations": _stamp(base),
    }

    out = await _mark(state, "rule_b")

    assert "expected_failures" not in out.update
    assert "validations" not in out.update
    assert "prover generated" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_a_timeout_cannot_be_marked():
    state = _after_run(_state(), {"rule_b": "TIMEOUT"})

    out = await _mark(state, "rule_b")

    assert "expected_failures" not in out.update
    assert "TIMEOUT" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_a_missing_verdict_still_blocks_the_stamp():
    state = _after_run(_state(), {"rule_b": "VIOLATED"}, unverdicted=["rule_a"])

    out = await _mark(state, "rule_b")

    assert "validations" not in out.update


@pytest.mark.asyncio
async def test_withdrawing_a_marking_the_stamp_relied_on_leaves_the_stamp_stale():
    """The stamp covers the markings, so the gate stops accepting it without anything clearing it."""
    base = _state(expected_failures={"rule_b": "real"})
    state = {**_after_run(base, {"rule_b": "VIOLATED"}), "validations": _stamp(base)}

    out = await ExpectRulePassage(state=state, tool_call_id="tc", rule_name="rule_b").run()

    assert out.update["expected_failures"] == {"rule_b": ""}
    assert "validations" not in out.update
    assert _stamp(_marked(state)) != _stamp(base)
    assert "no longer stamped" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_with_no_run_yet_a_marking_is_refused():
    out = await _mark(_state(), "rule_b")

    assert "expected_failures" not in out.update
    assert "validations" not in out.update
    assert "no verdict yet" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_withdrawing_the_marking_of_a_rule_that_verified_stamps_the_draft():
    state = _after_run(
        _state(expected_failures={"rule_b": "real"}), {"rule_a": "VERIFIED", "rule_b": "VERIFIED"}
    )

    out = await ExpectRulePassage(state=state, tool_call_id="tc", rule_name="rule_b").run()

    assert out.update["expected_failures"] == {"rule_b": ""}
    assert out.update["validations"] == _stamp(_marked(state))
    assert "stamped without another run" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_withdrawing_a_marking_that_is_not_there_changes_nothing():
    state = _after_run(_state(), {"rule_b": "VIOLATED"})

    out = await ExpectRulePassage(state=state, tool_call_id="tc", rule_name="rule_b").run()

    assert "expected_failures" not in out.update
    assert "validations" not in out.update
    assert "not marked" in str(out.update["messages"][0].content)
