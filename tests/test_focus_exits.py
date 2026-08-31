"""The tightened exits a prioritized author runs under.

Two escape hatches let a CVL author walk away from a property: the `give_up` tool, and
`record_skip`. With a whole run staked on one property, either is a total loss, so both are
constrained — but neither may be constrained into a deadlock, which is what these tests are
really about. The gate must stay satisfiable when the toolchain is broken, and the skip
protection must stand down when the budget monitor orders a wrap-up.
"""

import pytest
from langchain_core.messages import AIMessage

from composer.authoring.tools import GatedGiveUp, RecordSkip, _verify_attempts, skip_tools
from composer.spec.source.author import FocusPolicy
from composer.spec.types import PropertyTitle

pytestmark = pytest.mark.asyncio

FLOOR = 3
_Gated = GatedGiveUp.with_template(
    description="Stop work.", reason="why", label="CVL generation"
)
_Skip = RecordSkip.with_template(description="Skip it.", reason="why")


def _prover_calls(n: int) -> list[AIMessage]:
    return [
        AIMessage(content="", tool_calls=[{"name": "verify_spec", "args": {}, "id": f"c{i}"}])
        for i in range(n)
    ]


async def _give_up(sort: str, runs: int, floor: int = FLOOR) -> str:
    inst = _Gated(
        sort=sort, reason="r", attempts=["tried the obvious rule"],
        state={"messages": _prover_calls(runs)}, tool_call_id="t",
    )
    tok = _Gated._dep_ctx.set(floor)
    try:
        cmd = await inst.run()
    finally:
        _Gated._dep_ctx.reset(tok)
    return str(cmd.update["messages"][0].content)


async def _skip_via(tools, title: str) -> str:
    """Invoke the ``record_skip`` from a built tool list, as the graph would."""
    tool = next(t for t in tools if t.name == "record_skip")
    out = await tool.ainvoke(
        {"name": "record_skip", "args": {"property_title": title, "reason": "cannot do it"},
         "id": "t", "type": "tool_call"}
    )
    return str(out.update["messages"][0].content)


TITLES = [PropertyTitle("solvency"), PropertyTitle("shares_sane")]


def _direct(protected):
    """The editing branch: ``skip_tools`` called directly (spec/source/author.py)."""
    return skip_tools(TITLES, skip_description="d", skip_reason="r", protected=protected)


def _via_property_tools(protected):
    """The non-editing branch: the same pair reached through ``property_tools``
    (spec/cvl_generation.py). Both must honour the protection, or the ban depends on which
    branch happened to build the suite."""
    from composer.spec.cvl_generation import property_tools

    class _Services:
        titles = TITLES

    return property_tools(_Services(), protected=protected)  # type: ignore[arg-type]


# --- the give-up gate ------------------------------------------------------


@pytest.mark.parametrize("runs", [0, 1, FLOOR - 1])
async def test_exhausted_is_refused_before_the_prover_floor(runs):
    out = await _give_up("exhausted", runs)
    assert "Rejected" in out and str(FLOOR) in out


@pytest.mark.parametrize("runs", [FLOOR, FLOOR + 5])
async def test_exhausted_is_accepted_once_the_floor_is_met(runs):
    assert await _give_up("exhausted", runs) == "Accepted"


async def test_an_environment_stop_is_never_gated():
    # A missing certoraRun or solc produces no prover run at all, so gating this stop would make
    # the floor unsatisfiable and hold the agent against its recursion limit instead.
    assert await _give_up("environment", 0) == "Accepted"


async def test_the_reason_must_enumerate_attempts():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _Gated(
            sort="exhausted", reason="r", attempts=[],
            state={"messages": []}, tool_call_id="t",
        )


async def test_attempts_are_counted_from_tool_calls_not_prover_reports():
    # prover_history only grows when the prover returns a report, and the failures worth
    # escalating through (a spec that will not type-check) return before that. Counting calls
    # is what makes the floor reachable.
    state = {"messages": [*_prover_calls(2), AIMessage(content="thinking out loud")]}
    assert _verify_attempts(state) == 2
    assert _verify_attempts({"messages": []}) == 0
    assert _verify_attempts({}) == 0


# --- the focus policy ------------------------------------------------------


async def test_the_budget_wrap_up_lifts_the_focus_protection():
    # The wrap-up alert orders the agent to skip everything that does not work. A protection
    # that outlived that order would deadlock the session the budget was trying to end.
    focus = FocusPolicy(protected=(PropertyTitle("solvency"),))
    assert focus.protected_titles() == ("solvency",)
    focus.lifted = True
    assert focus.protected_titles() == ()


# --- the skip protection, on both binding paths ----------------------------


@pytest.mark.parametrize("build", [_direct, _via_property_tools], ids=["skip_tools", "property_tools"])
async def test_a_protected_property_cannot_be_skipped(build):
    out = await _skip_via(build(lambda: (PropertyTitle("solvency"),)), "solvency")
    assert "cannot be skipped" in out


@pytest.mark.parametrize("build", [_direct, _via_property_tools], ids=["skip_tools", "property_tools"])
async def test_an_unprotected_property_can_still_be_skipped(build):
    # The supporting cluster is protected too in a real run, but the mechanism must not be
    # "refuse everything" — an unprotected title still goes through.
    out = await _skip_via(build(lambda: (PropertyTitle("solvency"),)), "shares_sane")
    assert "Recorded skip" in out


@pytest.mark.parametrize("build", [_direct, _via_property_tools], ids=["skip_tools", "property_tools"])
async def test_nothing_is_protected_by_default(build):
    out = await _skip_via(build(()), "solvency")
    assert "Recorded skip" in out


async def test_the_lifted_policy_lets_the_budget_wrap_up_skip_the_focus():
    focus = FocusPolicy(protected=(PropertyTitle("solvency"),))
    tools = _direct(focus.protected_titles)
    assert "cannot be skipped" in await _skip_via(tools, "solvency")
    focus.lifted = True
    assert "Recorded skip" in await _skip_via(tools, "solvency")
