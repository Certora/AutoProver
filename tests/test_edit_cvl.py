"""Tests for ``edit_cvl``'s parallel-call guard, wired through a mocked ReAct
graph (``graphcore.testing.Scenario``).

An AI turn carrying more than one ``edit_cvl`` call must be refused: parallel
edits against the same buffer would race through the state reducer. The guard
is per-tool, not per-turn — a single ``edit_cvl`` alongside a *different* tool
call is allowed (unlike ``verify_spec``'s solo-turn rule).

Both tests stay on paths that return before ``maybe_update_cvl``, so the real
CVL parser (Typechecker jar) is never launched — this is a fast unit test.
"""
import pytest

from langgraph.graph import MessagesState

from composer.cvl.tools import WithCurrSpec, edit_cvl, get_cvl

from graphcore.testing import Scenario, tool_call_raw

pytestmark = pytest.mark.asyncio


class EditTestState(MessagesState, WithCurrSpec):
    pass


EDIT_TOOL = edit_cvl(EditTestState)
GET_TOOL = get_cvl(EditTestState)

# The exact refusal edit_cvl returns for a parallel-edit turn.
_REFUSAL = "`edit_cvl` tool cannot be called in parallel within the same turn."

_SPEC = """\
rule foo {
    assert true;
}
"""


def _edit(old: str, new: str):
    return tool_call_raw("edit_cvl", old_string=old, new_string=new)


def _scenario():
    return Scenario(EditTestState, EDIT_TOOL, GET_TOOL).init(curr_spec=_SPEC)


async def test_parallel_edits_both_refused():
    st = await _scenario().turn(
        _edit("assert true", "assert false"),
        _edit("rule foo", "rule bar"),
    ).run()
    responses = [p["resp"] for p in Scenario.get_last_tool_result(st)["edit_cvl"]]
    assert len(responses) == 2
    assert all(r == _REFUSAL for r in responses)
    # Neither edit landed: the buffer is untouched.
    assert st["curr_spec"] == _SPEC


async def test_single_edit_alongside_other_tool_not_refused():
    """One edit_cvl next to a different tool is not a parallel-edit turn. The
    edit targets a span absent from the buffer, so it fails in replace_unique
    — deterministically past the guard, without reaching the CVL parser."""
    st = await _scenario().turn(
        _edit("does not appear in the buffer", "irrelevant"),
        tool_call_raw("get_cvl"),
    ).run()
    results = Scenario.get_last_tool_result(st)
    (edit_pair,) = results["edit_cvl"]
    assert edit_pair["resp"] != _REFUSAL
    assert "`old_string` was not found" in edit_pair["resp"]
    # The sibling call was serviced normally (responses are harness-stripped).
    (get_pair,) = results["get_cvl"]
    assert get_pair["resp"] == _SPEC.strip()
