"""Marking a rule re-decides the prover stamp against the last run, without another run.

A marking almost always follows the run that showed the violation, so a gate that stamped only at
run time made every unit that ended on a new finding run its unchanged draft through the prover a
second time: on the vault benchmark, Fee Collection marked three failures after its last run, was
accepted by the judge, and was then refused at publish for a stale stamp.
"""

import pytest

from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY, LastVerdicts, tuning_history
from composer.spec.cvlr.verify import ExpectRuleFailure, ExpectRulePassage, prover_stamper

DRAFT = "#[rule] pub fn rule_a() {}\n#[rule] pub fn rule_b() {}\n"


def _state(draft: str = DRAFT, **extra) -> dict:
    return {
        "messages": [],
        "curr_spec": draft,
        "expected_failures": {},
        "skipped": [],
        "property_rules": [],
        "rule_subjects": [],
        "summaries": [],
        "munges": [],
        "prover_settings": TunableConf(),
        "required_validations": [PROVER_VALIDATION_KEY],
        "validations": {},
        "failed": None,
        "budget_curtailed": False,
        **extra,
    }


def _stamp(state: dict) -> dict[str, str]:
    return prover_stamper()(state, tuning_history(state))  # type: ignore[arg-type]


def _after_run(state: dict, status: dict[str, bool], **verdicts) -> dict:
    """``state`` with the verdicts of a run on its current draft recorded."""
    last = LastVerdicts(
        status=status,
        incomplete=verdicts.get("incomplete", []),
        unverdicted=verdicts.get("unverdicted", []),
        stamp=_stamp(state),
    )
    return {**state, "last_verdicts": last}


async def _mark(state: dict, rule: str):
    return await ExpectRuleFailure(
        state=state, tool_call_id="tc", rule_name=rule, reason="the program pays the fee unbounded"
    ).run()


@pytest.mark.asyncio
async def test_marking_the_last_failure_stamps_the_draft_without_a_run():
    state = _after_run(_state(), {"rule_a": True, "rule_b": False})

    out = await _mark(state, "rule_b")

    assert out.update["validations"] == _stamp(state)
    assert "stamped without another run" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_a_marking_that_leaves_a_failure_unaccounted_does_not_stamp():
    state = _after_run(_state(), {"rule_a": False, "rule_b": False})

    out = await _mark(state, "rule_b")

    assert out.update["validations"] == {PROVER_VALIDATION_KEY: ""}


@pytest.mark.asyncio
async def test_a_draft_edited_since_the_run_gains_no_stamp():
    """The kept stamp is the digest of the draft the run checked. Applied to an edited draft it
    simply does not match, which is the gate's existing staleness rule doing the work."""
    state = {**_after_run(_state(), {"rule_b": False}), "curr_spec": DRAFT + "// edited\n"}

    out = await _mark(state, "rule_b")

    assert out.update["validations"][PROVER_VALIDATION_KEY] != _stamp(state)[PROVER_VALIDATION_KEY]
    assert "stamped" not in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_a_rule_that_never_reached_its_property_cannot_be_marked_into_a_stamp():
    state = _after_run(_state(), {"rule_b": False}, incomplete=["rule_b"])

    out = await _mark(state, "rule_b")

    assert out.update["validations"] == {PROVER_VALIDATION_KEY: ""}


@pytest.mark.asyncio
async def test_a_missing_verdict_still_blocks_the_stamp():
    state = _after_run(_state(), {"rule_b": False}, unverdicted=["rule_a"])

    out = await _mark(state, "rule_b")

    assert out.update["validations"] == {PROVER_VALIDATION_KEY: ""}


@pytest.mark.asyncio
async def test_withdrawing_a_marking_the_stamp_relied_on_withdraws_the_stamp():
    """The other direction, which the run-time-only stamp got wrong: a draft stamped with a marked
    failure kept its stamp after the marking was withdrawn, and published an unaccounted failure."""
    base = _state(expected_failures={"rule_b": "real"})
    state = {**_after_run(base, {"rule_b": False}), "validations": _stamp(base)}

    out = await ExpectRulePassage(state=state, tool_call_id="tc", rule_name="rule_b").run()

    assert out.update["validations"] == {PROVER_VALIDATION_KEY: ""}
    assert "stamp is withdrawn" in str(out.update["messages"][0].content)


@pytest.mark.asyncio
async def test_with_no_run_yet_a_marking_touches_no_stamp():
    out = await _mark(_state(), "rule_b")

    assert "validations" not in out.update
