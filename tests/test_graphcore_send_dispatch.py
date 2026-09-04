"""Per-tool-call dispatch in graphcore's agent graph.

Each tool call of an AI turn runs as its own Pregel task (``Send`` into the tools node), with a
no-op join before the post-tools routing. The tests below pin the properties that buys:
an interrupt in one tool call neither drops nor re-runs its siblings, every interrupt has its
own id, and the end-of-loop check sees the merged state of the whole turn.

No network, no Postgres: a scripted fake chat model drives the real graph built by
``build_async_workflow`` over an ``InMemorySaver``.
"""

from typing import Annotated, Any, NotRequired, override
from collections.abc import Callable, Sequence

import pytest

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import MessagesState
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import InjectedState
from langgraph.types import Command, Interrupt, interrupt
from pydantic import Field

from graphcore.graph import FlowInput, build_async_workflow, tool_output

pytestmark = pytest.mark.asyncio

OUTPUT_KEY = "result"


class DispatchState(MessagesState):
    result: NotRequired[str]
    # A channel outside the input schema: only a prior run on the thread can seed it.
    note: NotRequired[str]


class RecordingFakeLLM(FakeMessagesListChatModel):
    """Scripted chat model that tolerates ``bind_tools`` and records every prompt it completes."""

    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def build_tools(counter: dict[str, int]) -> list[BaseTool]:
    @tool
    async def count() -> str:
        """Increment the test's counter."""
        counter["n"] += 1
        return "counted"

    @tool
    async def finish(value: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
        """Deliver the final result."""
        return tool_output(tool_call_id, {OUTPUT_KEY: value})

    @tool
    async def ask_human(question: str) -> str:
        """Ask the human a question and return their answer."""
        return interrupt({"q": question})

    @tool
    async def peek(state: Annotated[DispatchState, InjectedState]) -> str:
        """Report how many messages are in the graph state."""
        return str(len(state["messages"]))

    @tool
    async def set_note(text: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
        """Write the note channel."""
        return Command(update={"note": text, "messages": [ToolMessage("noted", tool_call_id=tool_call_id)]})

    @tool
    async def read_note(state: Annotated[DispatchState, InjectedState]) -> str:
        """Read the note channel."""
        return state.get("note", "<missing>")

    return [count, finish, ask_human, peek, set_note, read_note]


def _tc(name: str, tid: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": tid, "type": "tool_call"}


def _ai(*tool_calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(tool_calls))


def _cfg(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}


def _build(
    responses: list[BaseMessage], counter: dict[str, int]
) -> tuple[CompiledStateGraph, RecordingFakeLLM]:
    llm = RecordingFakeLLM(responses=responses)
    builder, _bound = build_async_workflow(
        state_class=DispatchState,
        input_type=FlowInput,
        tools_list=build_tools(counter),
        sys_prompt="You are a test subject.",
        initial_prompt="Use the tools, then call finish.",
        output_key=OUTPUT_KEY,
        unbound_llm=llm,
    )
    return builder.compile(checkpointer=InMemorySaver()), llm


async def _drain(
    graph: CompiledStateGraph, inp: Any, cfg: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[Interrupt]]:
    """Run the graph to its next pause or completion; separate interrupts from node updates."""
    updates: list[dict[str, Any]] = []
    interrupts: list[Interrupt] = []
    async for chunk in graph.astream(inp, config=cfg, stream_mode="updates"):
        if "__interrupt__" in chunk:
            interrupts.extend(chunk["__interrupt__"])
        else:
            updates.append(chunk)
    return updates, interrupts


async def _values(graph: CompiledStateGraph, cfg: dict[str, Any]) -> dict[str, Any]:
    snapshot = await graph.aget_state(cfg)
    return snapshot.values


def _tool_messages_after_turn(msgs: list[BaseMessage], turn: AIMessage) -> list[ToolMessage]:
    """The ToolMessages sitting directly after ``turn`` in ``msgs``, up to the next non-tool message."""
    start = msgs.index(turn) + 1
    out: list[ToolMessage] = []
    for m in msgs[start:]:
        if not isinstance(m, ToolMessage):
            break
        out.append(m)
    return out


async def test_finish_with_sibling_ends_after_one_llm_call():
    """A result tool called alongside a sibling ends the graph in that superstep: routing runs once on the merged state."""
    counter = {"n": 0}
    graph, llm = _build([_ai(_tc("finish", "f1", value="done"), _tc("count", "c1"))], counter)
    cfg = _cfg("t1")

    _updates, interrupts = await _drain(graph, FlowInput(input=[]), cfg)

    assert interrupts == []
    snapshot = await graph.aget_state(cfg)
    assert snapshot.next == ()
    assert snapshot.values[OUTPUT_KEY] == "done"
    assert counter["n"] == 1
    assert len(llm.seen) == 1


async def test_interrupting_sibling_does_not_rerun_completed_tool():
    """An interrupt in one tool task keeps its finished sibling's result; resuming re-runs only the interrupted task."""
    counter = {"n": 0}
    first_turn = _ai(_tc("ask_human", "a1", question="ok?"), _tc("count", "c1"))
    graph, llm = _build([first_turn, _ai(_tc("finish", "f1", value="done"))], counter)
    cfg = _cfg("t2")

    _updates, interrupts = await _drain(graph, FlowInput(input=[]), cfg)

    assert len(interrupts) == 1
    (pending,) = interrupts
    assert pending.value == {"q": "ok?"}
    assert pending.id
    assert counter["n"] == 1
    assert len(llm.seen) == 1
    assert OUTPUT_KEY not in await _values(graph, cfg)

    _updates, interrupts = await _drain(graph, Command(resume={pending.id: "yes"}), cfg)

    assert interrupts == []
    assert counter["n"] == 1
    assert len(llm.seen) == 2
    state = await _values(graph, cfg)
    assert state[OUTPUT_KEY] == "done"

    msgs = state["messages"]
    turn = next(m for m in msgs if isinstance(m, AIMessage) and m.tool_calls)
    results = _tool_messages_after_turn(msgs, turn)
    assert [m.tool_call_id for m in results] == ["a1", "c1"]
    assert [m.content for m in results] == ["yes", "counted"]

    second_prompt = llm.seen[1]
    assert [m.tool_call_id for m in second_prompt if isinstance(m, ToolMessage)] == ["a1", "c1"]


async def test_two_interrupts_in_one_turn_are_distinct():
    """Two interrupting tool calls in one turn carry different ids; a bare resume is refused, a keyed resume answers each."""
    counter = {"n": 0}
    graph, _llm = _build(
        [
            _ai(_tc("ask_human", "a1", question="one?"), _tc("ask_human", "a2", question="two?")),
            _ai(_tc("finish", "f1", value="done")),
        ],
        counter,
    )
    cfg = _cfg("t3")

    _updates, interrupts = await _drain(graph, FlowInput(input=[]), cfg)

    assert len(interrupts) == 2
    id_by_question = {i.value["q"]: i.id for i in interrupts}
    assert set(id_by_question) == {"one?", "two?"}
    assert id_by_question["one?"] != id_by_question["two?"]

    with pytest.raises(RuntimeError, match="multiple pending interrupts"):
        await _drain(graph, Command(resume="x"), cfg)

    _updates, interrupts = await _drain(
        graph,
        Command(resume={id_by_question["one?"]: "a", id_by_question["two?"]: "b"}),
        cfg,
    )

    assert interrupts == []
    state = await _values(graph, cfg)
    assert state[OUTPUT_KEY] == "done"
    content_by_id = {m.tool_call_id: m.content for m in state["messages"] if isinstance(m, ToolMessage)}
    assert content_by_id["a1"] == "a"
    assert content_by_id["a2"] == "b"


async def test_injected_state_tool_sees_state_under_send_dispatch():
    """A tool with InjectedState receives the real graph state through the Send payload."""
    graph, _llm = _build([_ai(_tc("peek", "p1")), _ai(_tc("finish", "f1", value="done"))], {"n": 0})
    cfg = _cfg("t4")

    _updates, interrupts = await _drain(graph, FlowInput(input=[]), cfg)

    assert interrupts == []
    state = await _values(graph, cfg)
    assert state[OUTPUT_KEY] == "done"
    msgs = state["messages"]
    peek_index = next(i for i, m in enumerate(msgs) if isinstance(m, ToolMessage) and m.tool_call_id == "p1")
    seen = int(msgs[peek_index].content)
    assert seen > 0
    # Everything before the peek's own result was in the state handed to it.
    assert seen == peek_index


async def test_first_turn_tools_see_state_channels_outside_the_input_schema():
    """A first-turn tool's InjectedState carries every state channel, not just the input schema's.

    The channel is seeded by an earlier run on the same thread; a second input on that thread
    re-enters through the initial node, whose outgoing edge is the one that would otherwise be
    read through the narrower input schema.
    """
    graph, _llm = _build(
        [
            _ai(_tc("set_note", "s1", text="seeded")),
            _ai(_tc("finish", "f1", value="done")),
            _ai(_tc("read_note", "r1")),
        ],
        {"n": 0},
    )
    cfg = _cfg("t6")

    _updates, interrupts = await _drain(graph, FlowInput(input=[]), cfg)
    assert interrupts == []
    assert (await _values(graph, cfg))["note"] == "seeded"

    # A fresh input on the same thread is a continuation: the initial node runs again over the
    # kept channels. `result` is still set from the first run, so the join ends the graph right
    # after the read.
    _updates, interrupts = await _drain(graph, FlowInput(input=[]), cfg)
    assert interrupts == []
    msgs = (await _values(graph, cfg))["messages"]
    read = next(m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == "r1")
    assert read.content == "seeded"


async def test_turn_without_tool_calls_still_routes_to_scolding():
    """An AI turn with no tool calls routes to no_tools and gets the scolding reminder, as before."""
    graph, llm = _build([AIMessage(content="All done, I think."), _ai(_tc("finish", "f1", value="done"))], {"n": 0})
    cfg = _cfg("t5")

    _updates, interrupts = await _drain(graph, FlowInput(input=[]), cfg)

    assert interrupts == []
    state = await _values(graph, cfg)
    assert state[OUTPUT_KEY] == "done"
    msgs = state["messages"]
    bare_index = next(i for i, m in enumerate(msgs) if isinstance(m, AIMessage) and not m.tool_calls)
    scolding = msgs[bare_index + 1]
    assert isinstance(scolding, HumanMessage)
    assert "Every AI turn must end with at least one tool call" in scolding.content
    assert len(llm.seen) == 2
