"""write_rough_draft echoes the draft and stamps drafted; that is the review."""

from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from composer.authoring.judge import JudgeState, _wrote_rough_draft
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools


class _State(MessagesState, RoughDraftState):
    pass


def _invoke(tools: list, state: dict[str, Any]) -> dict[str, Any]:
    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode(list(tools)))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile().invoke(state)


def _write_call(draft: str, call_id: str = "w1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "write_rough_draft",
        "args": {"rough_draft": draft},
        "id": call_id,
        "type": "tool_call",
    }])


def test_write_echoes_draft_and_stamps_drafted() -> None:
    write, _read = get_rough_draft_tools(_State)
    out = _invoke([write], {
        "messages": [_write_call("the draft")],
        "memory": None,
        "drafted": False,
    })
    assert out["memory"] == "the draft"
    assert out["drafted"] is True
    last = out["messages"][-1]
    assert isinstance(last, ToolMessage)
    assert last.content == "the draft"
    assert last.tool_call_id == "w1"


def test_write_delivers_review_reminder() -> None:
    write, _read = get_rough_draft_tools(_State, review_reminder="check the criteria")
    out = _invoke([write], {
        "messages": [_write_call("the draft")],
        "memory": None,
        "drafted": False,
    })
    echoed, reminder = out["messages"][-2], out["messages"][-1]
    assert isinstance(echoed, ToolMessage)
    assert echoed.content == "the draft"
    assert isinstance(reminder, HumanMessage)
    assert "check the criteria" in reminder.content
    assert reminder.content.startswith("<system-reminder>")


def test_write_with_reminder_rejects_parallel_calls() -> None:
    write, _read = get_rough_draft_tools(_State, review_reminder="check the criteria")
    out = _invoke([write], {
        "messages": [AIMessage(content="", tool_calls=[
            {
                "name": "write_rough_draft",
                "args": {"rough_draft": "first"},
                "id": "w1",
                "type": "tool_call",
            },
            {
                "name": "write_rough_draft",
                "args": {"rough_draft": "second"},
                "id": "w2",
                "type": "tool_call",
            },
        ])],
        "memory": None,
        "drafted": False,
    })
    assert out["memory"] is None
    assert out["drafted"] is False
    for msg in out["messages"][1:]:
        assert isinstance(msg, ToolMessage)
        assert "must be the only tool call" in msg.content


def test_read_still_echoes_stored_draft() -> None:
    write, read = get_rough_draft_tools(_State)
    after_write = _invoke([write], {
        "messages": [_write_call("the draft")],
        "memory": None,
        "drafted": False,
    })
    out = _invoke([read], {
        "messages": [
            *after_write["messages"],
            AIMessage(content="", tool_calls=[{
                "name": "read_rough_draft",
                "args": {},
                "id": "r1",
                "type": "tool_call",
            }]),
        ],
        "memory": after_write["memory"],
        "drafted": after_write["drafted"],
    })
    last = out["messages"][-1]
    assert isinstance(last, ToolMessage)
    assert last.content == "the draft"
    assert last.tool_call_id == "r1"
    assert out["drafted"] is True


def test_writing_a_draft_satisfies_the_completion_gate() -> None:
    """The half the tests above do not cover: that the gate *consuming* the flag agrees. A write
    that stamps the flag is worth nothing if the validator is still waiting for a read.

    Reaching for the private validator on purpose — the only public way to exercise it is to stand
    up the judge's whole graph, which needs a model and a database to answer a question that is one
    function call wide."""
    write, _read = get_rough_draft_tools(_State)
    out = _invoke([write], {
        "messages": [_write_call("the draft")],
        "memory": None,
        "drafted": False,
    })

    assert _wrote_rough_draft(cast(JudgeState, out), None) is None


def test_the_gate_still_refuses_an_agent_that_never_drafted() -> None:
    """The other direction, so the test above cannot be satisfied by a validator that always
    accepts."""
    assert _wrote_rough_draft(
        cast(JudgeState, {"messages": [], "memory": None, "drafted": False}), None
    ) is not None
