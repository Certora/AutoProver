"""Interrupt handling and suspension: ``InterruptHandler.handle_interrupts`` answers every
interrupt a graph is paused on by id, or raises ``GraphSuspended`` to park the run
for a later process, which re-runs from the checkpoint, reaches the same
interrupts, and asks again.

Pinned:

* two tasks interrupting in one superstep reach the handler together and are
  answered by id in one resume;
* ``HumanInteractionBridge`` turns a one-question-at-a-time handler into that;
* a suspension raised inside a durable child propagates through the parent
  unchanged, is not retried, and does not exhaust the child's generation: the
  next process resumes the parent, re-enters the tool, and the child picks up at
  its interrupt without re-running the work before it;
* under graphcore's per-tool-call dispatch, a sibling tool call that finished
  while the other's child suspended is not re-run on resume.
"""

import operator
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, TypedDict

import pytest

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Interrupt, interrupt

from composer.io.context import (
    DefaultRetryPolicy,
    DurableThread,
    durable_thread_id,
    install_retry_policy,
    run_to_completion,
    with_handler,
)
from composer.io.event_handler import NullEventHandler
from composer.io.protocol import GraphSuspended, HumanInteractionBridge, InterruptId
from graphcore.graph import FlowInput, build_async_workflow, tool_output
from tests.test_graph_retry import FakeOverloadedError, _recording_backoff, _retry_on
from tests.test_graphcore_send_dispatch import DispatchState, RecordingFakeLLM, _ai, _tc

pytestmark = pytest.mark.asyncio


class _Quiet:
    async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str) -> None:
        pass

    async def log_state_update(self, path: list[str], st: dict) -> None:
        pass

    async def log_start(self, *, path: list[str], description: str, tool_id: str | None) -> None:
        pass

    async def log_end(self, path: list[str]) -> None:
        pass


class ScriptedHandler(_Quiet):
    """Answers interrupts from ``answers`` (question text -> reply); with no
    answers it suspends. Records what it was asked."""

    def __init__(self, answers: dict[str, str] | None) -> None:
        self.answers = answers
        self.asked: list[list[str]] = []

    async def handle_interrupts(self, interrupts: Sequence[Interrupt], state: Any) -> Mapping[InterruptId, str]:
        self.asked.append([i.value["q"] for i in interrupts])
        if self.answers is None:
            raise GraphSuspended(interrupts)
        return {InterruptId(i.id): self.answers[i.value["q"]] for i in interrupts}


class OneAtATime(HumanInteractionBridge[dict], _Quiet):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def human_interaction(self, ty: dict) -> str:
        self.prompts.append(ty["q"])
        return f"re:{ty['q']}"


class QState(TypedDict):
    log: Annotated[list[str], operator.add]


def two_questions_graph():
    """START fans out to two nodes; each interrupts, so one superstep carries two."""

    def ask_a(state: QState) -> dict:
        return {"log": [f"a={interrupt({'q': 'a?'})}"]}

    def ask_b(state: QState) -> dict:
        return {"log": [f"b={interrupt({'q': 'b?'})}"]}

    b = StateGraph(QState)
    b.add_node("ask_a", ask_a)
    b.add_node("ask_b", ask_b)
    b.add_edge(START, "ask_a")
    b.add_edge(START, "ask_b")
    b.add_edge("ask_a", END)
    b.add_edge("ask_b", END)
    return b.compile(checkpointer=InMemorySaver())


async def test_parallel_interrupts_are_answered_together_by_id() -> None:
    handler = ScriptedHandler({"a?": "A", "b?": "B"})
    async with with_handler(handler, NullEventHandler(), handler):
        result = await run_to_completion(
            two_questions_graph(), {"log": []}, thread_id="t", recursion_limit=10, description="q"
        )
    assert handler.asked == [["a?", "b?"]]
    assert sorted(result["log"]) == ["a=A", "b=B"]


async def test_bridge_asks_one_question_at_a_time() -> None:
    handler = OneAtATime()
    async with with_handler(handler, NullEventHandler(), handler):
        result = await run_to_completion(
            two_questions_graph(), {"log": []}, thread_id="t", recursion_limit=10, description="q"
        )
    assert handler.prompts == ["a?", "b?"]
    assert sorted(result["log"]) == ["a=re:a?", "b=re:b?"]


# --- nested: a durable child suspends inside the parent's tool ---------------------

TOOL_CALL = "toolu_01suspend"
PARENT = "parent"
CHILD_G0 = durable_thread_id(PARENT, "worker", TOOL_CALL, 0)


class Nested:
    def __init__(self) -> None:
        self.child_runs: list[str] = []
        self.parent_runs: list[str] = []
        self.child = self._child()
        self.parent = self._parent()

    def _child(self):
        async def gather(state: QState) -> dict:
            self.child_runs.append("gather")
            return {"log": ["gathered"]}

        async def ask(state: QState) -> dict:
            self.child_runs.append("ask")
            return {"log": [f"answer={interrupt({'q': 'go?'})}"]}

        b = StateGraph(QState)
        b.add_node("gather", gather)
        b.add_node("ask", ask)
        b.add_edge(START, "gather")
        b.add_edge("gather", "ask")
        b.add_edge("ask", END)
        return b.compile(checkpointer=InMemorySaver())

    def _parent(self):
        async def plan(state: QState) -> dict:
            self.parent_runs.append("plan")
            return {"log": ["planned"]}

        async def delegate(state: QState) -> dict:
            self.parent_runs.append("delegate")
            child = await run_to_completion(
                self.child, {"log": []}, thread_id=DurableThread("worker"), within_tool=TOOL_CALL,
                recursion_limit=10, description="child",
            )
            return {"log": [f"delegate: {child['log'][-1]}"]}

        b = StateGraph(QState)
        b.add_node("plan", plan)
        b.add_node("delegate", delegate)
        b.add_edge(START, "plan")
        b.add_edge("plan", "delegate")
        b.add_edge("delegate", END)
        return b.compile(checkpointer=InMemorySaver())

    async def run(self, handler: ScriptedHandler, *, resume: bool, retry: DefaultRetryPolicy | None = None) -> QState:
        async with with_handler(handler, NullEventHandler(), handler):
            return await run_to_completion(
                self.parent, None if resume else {"log": []}, thread_id=PARENT,
                recursion_limit=10, description="parent", retry=retry,
            )


async def test_child_suspension_propagates_and_the_next_process_resumes_it() -> None:
    n = Nested()
    backoffs: list[int] = []
    policy = DefaultRetryPolicy(_retry_on(FakeOverloadedError), _recording_backoff(backoffs), max_retries=3)
    install_retry_policy(policy)

    cold = ScriptedHandler(None)
    with pytest.raises(GraphSuspended) as caught:
        await n.run(cold, resume=False, retry=policy)
    assert [i.value for i in caught.value.interrupts] == [{"q": "go?"}]
    assert backoffs == []  # a suspension is not retried, by the child's floor or the parent's
    assert n.child_runs == ["gather", "ask"]
    assert n.parent_runs == ["plan", "delegate"]

    # Process 2: the mailbox now has the answer. The parent resumes from its own
    # checkpoint, re-enters delegate, and the child resumes at the interrupt.
    install_retry_policy(policy)  # a new process: empty exhausted-thread registry
    warm = ScriptedHandler({"go?": "yes"})
    result = await n.run(warm, resume=True, retry=policy)

    # 'gather' did not run again. 'ask' runs twice more: once to re-raise the pending
    # interrupt (LangGraph re-executes an interrupted node from the top), once answered.
    assert n.child_runs == ["gather", "ask", "ask", "ask"]
    assert n.parent_runs == ["plan", "delegate", "delegate"]
    assert warm.asked == [["go?"]]
    assert result["log"][-1] == "delegate: answer=yes"
    child_log = (await n.child.aget_state({"configurable": {"thread_id": CHILD_G0}})).values["log"]
    assert child_log == ["gathered", "answer=yes"]


# --- per-tool-call dispatch: a finished sibling stays finished ------------------------


async def test_sibling_tool_call_is_not_rerun_when_the_other_suspends() -> None:
    """The parent is a real graphcore agent graph. One tool call spawns a durable child
    that suspends; the sibling `count` call finishes in the same superstep. Because
    ``GraphSuspended`` is a ``GraphBubbleUp``, LangGraph lets the sibling finish and
    persists its write, so the resumed superstep runs only the suspended call."""
    counter = {"n": 0}
    child_runs: list[str] = []

    async def ask(state: QState) -> dict:
        child_runs.append("ask")
        return {"log": [f"answer={interrupt({'q': 'go?'})}"]}

    cb = StateGraph(QState)
    cb.add_node("ask", ask)
    cb.add_edge(START, "ask")
    cb.add_edge("ask", END)
    child = cb.compile(checkpointer=InMemorySaver())

    @tool
    async def count() -> str:
        """Increment the counter."""
        counter["n"] += 1
        return "counted"

    @tool
    async def delegate(tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        """Hand the question to the child."""
        st = await run_to_completion(
            child, {"log": []}, thread_id=DurableThread("worker"), within_tool=tool_call_id,
            recursion_limit=10, description="child",
        )
        return st["log"][-1]

    @tool
    async def finish(value: str, tool_call_id: Annotated[str, InjectedToolCallId]):
        """Deliver the result."""
        return tool_output(tool_call_id, {"result": value})

    llm = RecordingFakeLLM(
        responses=[_ai(_tc("delegate", "d1"), _tc("count", "c1")), _ai(_tc("finish", "f1", value="done"))]
    )
    builder, _ = build_async_workflow(
        state_class=DispatchState, input_type=FlowInput, tools_list=[count, delegate, finish],
        sys_prompt="test", initial_prompt="go", output_key="result", unbound_llm=llm,
    )
    graph = builder.compile(checkpointer=InMemorySaver())

    cold = ScriptedHandler(None)
    async with with_handler(cold, NullEventHandler(), cold):
        with pytest.raises(GraphSuspended):
            await run_to_completion(graph, FlowInput(input=[]), thread_id="p", recursion_limit=25, description="agent")
    assert counter["n"] == 1
    assert child_runs == ["ask"]
    assert len(llm.seen) == 1

    install_retry_policy(None)  # new process
    warm = ScriptedHandler({"go?": "yes"})
    async with with_handler(warm, NullEventHandler(), warm):
        state = await run_to_completion(graph, None, thread_id="p", recursion_limit=25, description="agent")

    assert counter["n"] == 1, "the finished sibling was not re-run"
    assert child_runs == ["ask", "ask", "ask"]  # re-raised once, then answered
    assert state["result"] == "done"
    assert len(llm.seen) == 2
    results = {m.tool_call_id: m.content for m in state["messages"] if isinstance(m, ToolMessage)}
    assert results["c1"] == "counted"
    assert results["d1"] == "answer=yes"
