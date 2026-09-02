"""The tightened exits a prioritized author runs under.

Two escape hatches let a CVL author walk away from a property: the `give_up` tool, and
`record_skip`. With a whole run staked on one property, either is a total loss, so both are
constrained — but neither may be constrained into a deadlock, which is what these tests are
really about. The gate must stay satisfiable when the toolchain is broken, and the skip
protection must stand down when the budget monitor orders a wrap-up.
"""

import pytest
from langchain_core.messages import AIMessage

from composer.authoring.tools import (
    GatedGiveUp, RecordSkip, SkipScope, _verify_attempts, skip_tools,
)
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
        out = await inst.run()
    finally:
        _Gated._dep_ctx.reset(tok)
    # A refusal is a plain string; only an accepted surrender writes state.
    return out if isinstance(out, str) else str(out.update["messages"][0].content)


async def _skip(scope: SkipScope, title: str, *, curtailed: bool = False) -> str:
    """Run ``record_skip`` against the scope a builder produced, with the state it reads."""
    inst = _Skip(
        property_title=PropertyTitle(title), reason="cannot do it",
        state={"messages": [], "budget_curtailed": curtailed}, tool_call_id="t",
    )
    tok = _Skip._dep_ctx.set(scope)
    try:
        out = await inst.run()
    finally:
        _Skip._dep_ctx.reset(tok)
    return out if isinstance(out, str) else str(out.update["messages"][0].content)


TITLES = [PropertyTitle("solvency"), PropertyTitle("shares_sane")]
PROTECT_SOLVENCY = [PropertyTitle("solvency")]


def _direct(protected):
    """The editing branch builds the pair with ``skip_tools`` (spec/source/author.py)."""
    return skip_tools(TITLES, skip_description="d", skip_reason="r", protected=protected)


def _via_property_tools(protected):
    """The non-editing branch reaches the same pair through ``property_tools``
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


# --- the skip protection --------------------------------------------------


@pytest.mark.parametrize("build", [_direct, _via_property_tools], ids=["skip_tools", "property_tools"])
async def test_both_binding_paths_offer_record_skip(build):
    # The author reaches its skip pair by either route; a protection wired into only one is
    # silently bypassable from the other.
    assert {"record_skip", "unskip_property"} <= {tool.name for tool in build(PROTECT_SOLVENCY)}


async def test_a_protected_property_cannot_be_skipped():
    scope = SkipScope(titles=lambda: TITLES, protected=frozenset(PROTECT_SOLVENCY))
    assert "cannot be skipped" in await _skip(scope, "solvency")


async def test_an_unprotected_property_can_still_be_skipped():
    scope = SkipScope(titles=lambda: TITLES, protected=frozenset(PROTECT_SOLVENCY))
    assert "Recorded skip" in await _skip(scope, "shares_sane")


async def test_nothing_is_protected_by_default():
    assert "Recorded skip" in await _skip(SkipScope(titles=lambda: TITLES), "solvency")


async def test_the_budget_wrap_up_lifts_the_focus_protection():
    # The wrap-up order is "skip everything that does not work", which the protection would
    # otherwise refuse, deadlocking the session the budget was ending. The signal is
    # ``budget_curtailed`` in the graph state, so it survives a checkpoint; a flag on the policy
    # object would not, and could disagree with the state it shadows.
    scope = SkipScope(titles=lambda: TITLES, protected=frozenset(PROTECT_SOLVENCY))
    assert "cannot be skipped" in await _skip(scope, "solvency")
    assert "Recorded skip" in await _skip(scope, "solvency", curtailed=True)


async def test_the_focus_policy_holds_no_mutable_state():
    assert not hasattr(FocusPolicy(protected=(PropertyTitle("solvency"),)), "lifted")
