"""``adjust_prover_config``: the two prover settings the author may move.

The list is closed. The loop bound decides how the prover spends its time. ``optimistic_loop``
decides what a green verdict means, and is on the list because the sound remedies for a loop whose
trip count the analysis cannot fix run out before the problem does. A ``rule_sanity`` downgrade and
the prover flags are not on it.
"""

from composer.spec.cvlr.author_tools import AdjustProverConfig
from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.state import tuning_history

DRAFT = "#[rule] pub fn rule_a() {}\n"
WHY = "tried bounding the operands first"


def _state(settings: TunableConf = TunableConf()) -> dict:
    return {
        "messages": [],
        "curr_spec": DRAFT,
        "skipped": [],
        "summaries": [],
        "vfs": {},
        "version_history": [],
        "prover_settings": settings,
        "required_validations": [],
        "validations": {},
    }


def _adjust(state: dict, *, why: str = WHY, **fields):
    return AdjustProverConfig(state=state, tool_call_id="tc", why=why, **fields).run()


def test_raising_the_loop_bound_sets_it():
    out = _adjust(_state(), loop_iter=4)
    assert not isinstance(out, str), out
    assert out.update["prover_settings"].loop_iter == 4


def test_the_loop_bound_is_not_capped():
    """The CVL backend's ``loop_iter`` (``composer/tools/prover.py``) is a plain int steered by
    prose, not a ceiling. A bound the author cannot exceed would turn a cost trade into a refusal."""
    out = _adjust(_state(), loop_iter=9)
    assert not isinstance(out, str), out
    assert out.update["prover_settings"].loop_iter == 9


def test_a_loop_bound_below_one_is_refused():
    out = _adjust(_state(), loop_iter=0)
    assert isinstance(out, str) and "not a bound" in out


def test_a_setting_that_is_already_what_you_asked_for_is_refused():
    out = _adjust(_state(), loop_iter=2)
    assert isinstance(out, str) and "already 2" in out


def test_optimistic_loop_goes_on_and_the_whole_conf_comes_back():
    out = _adjust(_state(), optimistic_loop=True)
    assert not isinstance(out, str), out
    assert out.update["prover_settings"].optimistic_loop is True
    assert out.update["prover_settings"].loop_iter == 2
    assert '"optimistic_loop": true' in out.update["messages"][0].content


def test_optimistic_loop_already_on_is_refused():
    state = _state(TunableConf(optimistic_loop=True))
    out = _adjust(state, optimistic_loop=True)
    assert isinstance(out, str) and "already on" in out


def test_optimistic_loop_goes_off():
    state = _state(TunableConf(optimistic_loop=True))
    out = _adjust(state, optimistic_loop=False)
    assert not isinstance(out, str), out
    assert out.update["prover_settings"] == TunableConf()


def test_two_settings_in_one_call_both_land():
    out = _adjust(_state(), loop_iter=4, optimistic_loop=True)
    assert not isinstance(out, str), out
    assert out.update["prover_settings"] == TunableConf(loop_iter=4, optimistic_loop=True)


def test_one_refused_setting_lands_neither():
    """The optimistic-loop change is refused, so the loop bound must not have landed."""
    state = _state()
    out = _adjust(state, loop_iter=4, optimistic_loop=False)
    assert isinstance(out, str) and "already off" in out
    assert state["prover_settings"] == TunableConf()


def test_turning_optimistic_loop_on_without_a_reason_is_refused():
    out = _adjust(_state(), optimistic_loop=True, why="  ")
    assert isinstance(out, str) and "non-empty `why`" in out


def test_a_loop_bound_change_does_not_require_a_reason():
    """A loop bound does not change what a green verdict means."""
    out = _adjust(_state(), loop_iter=4, why="")
    assert not isinstance(out, str), out
    assert out.update["prover_settings"].loop_iter == 4
    assert out.update["prover_settings"].optimistic_loop is False


def test_a_config_change_invalidates_the_prover_stamp():
    """A verdict earned under one loop bound is not a verdict under another."""
    before = _state()
    out = _adjust(before, loop_iter=4)
    assert not isinstance(out, str), out
    after = {**before, "prover_settings": out.update["prover_settings"]}
    assert tuning_history(before) != tuning_history(after)
