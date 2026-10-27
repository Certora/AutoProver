"""``adjust_prover_config``: the two prover settings the author may move.

The list is closed. The loop bound decides how the prover spends its time. ``optimistic_loop``
decides what a green verdict means, and is on the list because the sound remedies for a loop whose
trip count the analysis cannot fix run out before the problem does. A ``rule_sanity`` downgrade and
the prover flags are not on it.
"""

from typing import Annotated, TypedDict

from langgraph.graph import START, StateGraph

from composer.spec.cvlr.author_tools import AdjustProverConfig
from composer.spec.cvlr.conf import (
    OptimisticLoop,
    OptimisticLoopOff,
    SettingsEdit,
    TunableConf,
    merge_settings,
)
from composer.spec.cvlr.state import tuning_history

DRAFT = "#[rule] pub fn rule_a() {}\n"
WHY = "tried bounding the operands first"


def _state(settings: TunableConf = TunableConf()) -> dict:
    return {
        "messages": [],
        "curr_spec": DRAFT,
        "expected_failures": {},
        "skipped": [],
        "property_rules": [],
        "rule_subjects": [],
        "summaries": [],
        "vfs": {},
        "version_history": [],
        "prover_settings": settings,
        "required_validations": [],
        "validations": {},
        "failed": None,
        "budget_curtailed": False,
    }


def _adjust(state: dict, *, why: str = WHY, **fields):
    return AdjustProverConfig(state=state, tool_call_id="tc", why=why, **fields).run()


def _settings_after(state: dict, out) -> TunableConf:
    """The settings once the state's reducer has applied what the call wrote."""
    assert not isinstance(out, str), out
    return merge_settings(state["prover_settings"], out.update["prover_settings"])


def test_raising_the_loop_bound_sets_it():
    state = _state()
    out = _adjust(state, loop_iter=4)
    assert _settings_after(state, out).loop_iter == 4


def test_the_loop_bound_is_not_capped():
    """The CVL backend's ``loop_iter`` (``composer/tools/prover.py``) is a plain int steered by
    prose, not a ceiling. A bound the author cannot exceed would turn a cost trade into a refusal."""
    state = _state()
    out = _adjust(state, loop_iter=9)
    assert _settings_after(state, out).loop_iter == 9


def test_a_loop_bound_below_one_is_refused():
    out = _adjust(_state(), loop_iter=0)
    assert isinstance(out, str) and "not a bound" in out


def test_a_setting_that_is_already_what_you_asked_for_is_refused():
    out = _adjust(_state(), loop_iter=2)
    assert isinstance(out, str) and "already 2" in out


def test_optimistic_loop_goes_on_and_the_whole_conf_comes_back():
    state = _state()
    out = _adjust(state, optimistic_loop=True)
    # The setting keeps the call's `why`: the judge weighs the setting by it.
    assert _settings_after(state, out) == TunableConf(optimistic_loop=OptimisticLoop(why=WHY))
    assert '"optimistic_loop": true' in out.update["messages"][0].content


def test_optimistic_loop_already_on_is_refused():
    state = _state(TunableConf(optimistic_loop=OptimisticLoop(why="w")))
    out = _adjust(state, optimistic_loop=True)
    assert isinstance(out, str) and "already on" in out


def test_optimistic_loop_goes_off_and_takes_its_reason_with_it():
    state = _state(TunableConf(optimistic_loop=OptimisticLoop(why="w")))
    out = _adjust(state, optimistic_loop=False)
    assert _settings_after(state, out) == TunableConf()


def test_two_settings_in_one_call_both_land():
    state = _state()
    out = _adjust(state, loop_iter=4, optimistic_loop=True)
    assert _settings_after(state, out) == TunableConf(
        loop_iter=4, optimistic_loop=OptimisticLoop(why=WHY)
    )


def test_one_refused_setting_lands_neither():
    """The optimistic-loop change is refused, so the loop bound must not have landed."""
    state = _state()
    out = _adjust(state, loop_iter=4, optimistic_loop=False)
    assert isinstance(out, str) and "already off" in out
    assert state["prover_settings"] == TunableConf()


def test_an_unexplained_config_change_is_refused():
    """The conf ships with the deliverable, so a setting nobody explained is one a reader cannot
    weigh."""
    out = _adjust(_state(), loop_iter=3, why="  ")
    assert isinstance(out, str) and "non-empty `why`" in out


def test_a_config_change_invalidates_the_prover_stamp():
    """A verdict earned under one loop bound is not a verdict under another."""
    before = _state()
    out = _adjust(before, loop_iter=4)
    assert not isinstance(out, str), out
    after = {**before, "prover_settings": _settings_after(before, out)}
    assert tuning_history(before) != tuning_history(after)


def test_parallel_calls_that_set_different_settings_both_land():
    """Several ``adjust_prover_config`` calls can land in one graph step. Each read the settings
    before the step, so each writes only what it set, and the reducer applies both."""

    class S(TypedDict):
        prover_settings: Annotated[TunableConf, merge_settings]

    on = OptimisticLoop(why=WHY)
    graph = StateGraph(S)
    graph.add_node("bound", lambda _s: {"prover_settings": SettingsEdit(loop_iter=4)})
    graph.add_node("optimistic", lambda _s: {"prover_settings": SettingsEdit(optimistic_loop=on)})
    graph.add_edge(START, "bound")
    graph.add_edge(START, "optimistic")
    out = graph.compile().invoke({"prover_settings": TunableConf()})
    assert out["prover_settings"] == TunableConf(loop_iter=4, optimistic_loop=on)


def test_turning_optimistic_loop_off_leaves_the_bound():
    start = TunableConf(loop_iter=3, optimistic_loop=OptimisticLoop(why="w"))
    edit = SettingsEdit(optimistic_loop=OptimisticLoopOff())
    assert merge_settings(start, edit) == TunableConf(loop_iter=3)


def test_the_initial_settings_replace_the_defaults():
    """A unit's starting settings arrive as a whole conf, not an edit."""
    assert merge_settings(TunableConf(), TunableConf(loop_iter=3)) == TunableConf(loop_iter=3)
