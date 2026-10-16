"""``adjust_prover_config``: the two prover settings the author may move.

The list is closed. The loop bound decides how the prover spends its time. ``optimistic_loop``
decides what a green verdict means, and is on the list because the sound remedies for a loop whose
trip count the analysis cannot fix run out before the problem does. A ``rule_sanity`` downgrade and
the prover flags are not on it.
"""

from composer.spec.cvlr.author_tools import AdjustProverConfig, SetLoopIter, SetOptimisticLoop
from composer.spec.cvlr.conf import OptimisticLoop, TunableConf
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


def _adjust(state: dict, edits: list, why: str = WHY):
    return AdjustProverConfig(state=state, tool_call_id="tc", edits=edits, why=why).run()


def test_raising_the_loop_bound_sets_it():
    out = _adjust(_state(), [SetLoopIter(type="loop_iter", iterations=4)])
    assert not isinstance(out, str), out
    assert out.update["prover_settings"].loop_iter == 4


def test_the_loop_bound_is_not_capped():
    """The CVL backend's ``loop_iter`` (``composer/tools/prover.py``) is a plain int steered by
    prose, not a ceiling. A bound the author cannot exceed would turn a cost trade into a refusal."""
    out = _adjust(_state(), [SetLoopIter(type="loop_iter", iterations=9)])
    assert not isinstance(out, str), out
    assert out.update["prover_settings"].loop_iter == 9


def test_a_loop_bound_below_one_is_refused():
    out = _adjust(_state(), [SetLoopIter(type="loop_iter", iterations=0)])
    assert isinstance(out, str) and "not a bound" in out


def test_a_setting_that_is_already_what_you_asked_for_is_refused():
    out = _adjust(_state(), [SetLoopIter(type="loop_iter", iterations=2)])
    assert isinstance(out, str) and "already 2" in out


def test_optimistic_loop_goes_on_and_the_whole_conf_comes_back():
    out = _adjust(_state(), [SetOptimisticLoop(type="optimistic_loop", enabled=True)])
    assert not isinstance(out, str), out
    # The setting keeps the call's `why`: the judge weighs the setting by it.
    assert out.update["prover_settings"].optimistic_loop == OptimisticLoop(why=WHY)
    assert out.update["prover_settings"].loop_iter == 2
    assert '"optimistic_loop": true' in out.update["messages"][0].content


def test_optimistic_loop_already_on_is_refused():
    state = _state(TunableConf(optimistic_loop=OptimisticLoop(why="w")))
    out = _adjust(state, [SetOptimisticLoop(type="optimistic_loop", enabled=True)])
    assert isinstance(out, str) and "already on" in out


def test_optimistic_loop_goes_off_and_takes_its_reason_with_it():
    state = _state(TunableConf(optimistic_loop=OptimisticLoop(why="w")))
    out = _adjust(state, [SetOptimisticLoop(type="optimistic_loop", enabled=False)])
    assert not isinstance(out, str), out
    assert out.update["prover_settings"] == TunableConf()


def test_two_edits_in_one_call_both_land():
    out = _adjust(
        _state(),
        [SetLoopIter(type="loop_iter", iterations=4),
         SetOptimisticLoop(type="optimistic_loop", enabled=True)],
    )
    assert not isinstance(out, str), out
    assert out.update["prover_settings"] == TunableConf(
        loop_iter=4, optimistic_loop=OptimisticLoop(why=WHY)
    )


def test_one_refused_edit_lands_none_of_the_batch():
    """The second edit is refused, so the first must not have landed."""
    state = _state()
    out = _adjust(state, [
        SetLoopIter(type="loop_iter", iterations=4),
        SetOptimisticLoop(type="optimistic_loop", enabled=False),
    ])
    assert isinstance(out, str) and "already off" in out
    assert state["prover_settings"] == TunableConf()


def test_an_unexplained_config_change_is_refused():
    """The conf ships with the deliverable, so a setting nobody explained is one a reader cannot
    weigh."""
    out = _adjust(_state(), [SetLoopIter(type="loop_iter", iterations=3)], why="  ")
    assert isinstance(out, str) and "non-empty `why`" in out


def test_a_config_change_invalidates_the_prover_stamp():
    """A verdict earned under one loop bound is not a verdict under another."""
    before = _state()
    out = _adjust(before, [SetLoopIter(type="loop_iter", iterations=4)])
    assert not isinstance(out, str), out
    after = {**before, "prover_settings": out.update["prover_settings"]}
    assert tuning_history(before) != tuning_history(after)
