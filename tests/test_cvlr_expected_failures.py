"""Marking a rule records the marking and nothing else.

The prover stamp covers the markings, so a marking changed after a green run leaves that run's
stamp stale. Only ``verify_rules`` stamps the draft again.
"""

from composer.authoring.state import check_completion, checked_digest, make_verdict_stamper
from composer.spec.cvlr.author_tools import ExpectRuleFailure, ExpectRulePassage
from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY, tuning_history

DRAFT = "#[rule] pub fn rule_a() {}\n#[rule] pub fn rule_b() {}\n"


def _state(**extra) -> dict:
    return {
        "messages": [],
        "curr_spec": DRAFT,
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


def _stamped(state: dict) -> dict:
    """``state`` as a green run on it leaves it."""
    stamp = checked_digest(state, tuning_history(state))  # type: ignore[arg-type]
    return {**state, "validations": {PROVER_VALIDATION_KEY: stamp}}


def _applied(state: dict, marking: dict[str, str]) -> dict:
    marked = {**state["expected_failures"], **marking}
    return {**state, "expected_failures": {k: v for k, v in marked.items() if v}}


def _complete(state: dict) -> bool:
    stamper = make_verdict_stamper(PROVER_VALIDATION_KEY)
    return check_completion(state, [stamper], tuning_history(state)) is None  # type: ignore[arg-type]


def test_marking_a_rule_records_it_and_stamps_nothing():
    out = ExpectRuleFailure(
        state=_state(), tool_call_id="tc", rule_name="rule_b", reason="the program underflows"
    ).run()

    assert out.update["expected_failures"] == {"rule_b": "the program underflows"}
    assert "validations" not in out.update
    assert "verify_rules" in str(out.update["messages"][0].content)


def test_a_marking_added_after_a_green_run_leaves_its_stamp_stale():
    state = _stamped(_state())
    assert _complete(state)

    out = ExpectRuleFailure(
        state=state, tool_call_id="tc", rule_name="rule_b", reason="the program underflows"
    ).run()

    assert not _complete(_applied(state, out.update["expected_failures"]))


def test_withdrawing_a_marking_after_a_green_run_leaves_its_stamp_stale():
    state = _stamped(_state(expected_failures={"rule_b": "the program underflows"}))
    assert _complete(state)

    out = ExpectRulePassage(state=state, tool_call_id="tc", rule_name="rule_b").run()

    assert out.update["expected_failures"] == {"rule_b": ""}
    assert "validations" not in out.update
    assert not _complete(_applied(state, out.update["expected_failures"]))


def test_a_marking_needs_a_reason():
    out = ExpectRuleFailure(state=_state(), tool_call_id="tc", rule_name="rule_b", reason=" ").run()

    assert "expected_failures" not in out.update


def test_withdrawing_a_marking_that_is_not_there_changes_nothing():
    out = ExpectRulePassage(state=_state(), tool_call_id="tc", rule_name="rule_b").run()

    assert "expected_failures" not in out.update
    assert "not marked" in str(out.update["messages"][0].content)
