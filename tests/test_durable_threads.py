"""``DurableThread``: sub-agent thread ids derived from the spawn's identity.

A parent graph's ``delegate`` node plays the tool: it spawns a child graph through
``run_to_completion`` with a ``DurableThread`` and a fixed ``within_tool`` id, the way
a tool body does. The child is ``gather -> act``; both graphs sit on in-memory savers
and consult scripted fakes, so no Postgres and no LLM.

Pinned:

* the id is a pure function of parent thread, prefix, tool call and generation;
* a floor retry of the parent re-enters the spawn and RESUMES the child from its
  tip: the child's completed node does not re-run;
* the ladder survives: a child whose own run raised is exhausted for the process,
  so the next spawn takes the next generation, fresh;
* a cold start (empty registry) walks the generations from the checkpointer:
  ``g+1`` existing means ``g`` was abandoned, else the highest existing
  generation resumes, and a child that had already finished returns at once;
* execution-scoped spawns keep plain string ids and stay fresh.
"""

import operator
from typing import Annotated, Any, TypedDict

import pytest

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from composer.io.context import (
    DefaultRetryPolicy,
    DurableThread,
    durable_thread_id,
    install_retry_policy,
    run_to_completion,
    with_handler,
)
from composer.io.event_handler import NullEventHandler
from composer.io.protocol import RefuseInterrupts
from tests.test_graph_retry import (
    FakeFatalError,
    FakeOverloadedError,
    FlakyLLM,
    _RecordingIOHandler,
    _recording_backoff,
    _retry_on,
)

pytestmark = pytest.mark.asyncio

TOOL_CALL = "toolu_01deterministic"
PARENT = "parent-thread"


class ChildState(TypedDict):
    log: Annotated[list[str], operator.add]


class ParentState(TypedDict):
    log: Annotated[list[str], operator.add]


class Harness:
    """One parent graph and one child graph, with the knobs the scenarios need."""

    def __init__(self, child_script: list[str | Exception], parent_script: list[str | Exception]) -> None:
        self.child_llm = FlakyLLM(child_script)
        self.parent_llm = FlakyLLM(parent_script)
        self.child_runs: list[str] = []
        self.parent_runs: list[str] = []
        self.child_threads: list[str] = []
        self.child_saver = InMemorySaver()
        self.child = self._build_child()
        self.parent = self._build_parent()
        self.handler = _RecordingIOHandler()
        #: Raised by ``delegate`` after the child returned, once, to model a
        #: parent that dies with its child already done.
        self.raise_after_child: Exception | None = None

    def _build_child(self) -> CompiledStateGraph[ChildState, None, ChildState, ChildState]:
        async def gather(state: ChildState, config: RunnableConfig) -> dict:
            self.child_runs.append("gather")
            self.child_threads.append(config["configurable"]["thread_id"])
            return {"log": [f"gathered: {await self.child_llm.ainvoke('gather')}"]}

        async def act(state: ChildState) -> dict:
            self.child_runs.append("act")
            return {"log": [f"acted: {await self.child_llm.ainvoke('act')}"]}

        b = StateGraph(ChildState)
        b.add_node("gather", gather)
        b.add_node("act", act)
        b.add_edge(START, "gather")
        b.add_edge("gather", "act")
        b.add_edge("act", END)
        return b.compile(checkpointer=self.child_saver)

    def _build_parent(self) -> CompiledStateGraph[ParentState, None, ParentState, ParentState]:
        async def plan(state: ParentState) -> dict:
            self.parent_runs.append("plan")
            return {"log": [f"plan: {await self.parent_llm.ainvoke('plan')}"]}

        async def delegate(state: ParentState) -> dict:
            self.parent_runs.append("delegate")
            child_state = await run_to_completion(
                self.child,
                {"log": ["<child input>"]},
                thread_id=DurableThread("worker"),
                within_tool=TOOL_CALL,
                recursion_limit=10,
                description="child work",
            )
            if self.raise_after_child is not None:
                exc, self.raise_after_child = self.raise_after_child, None
                raise exc
            return {"log": [f"delegate: {child_state['log'][-1]}"]}

        b = StateGraph(ParentState)
        b.add_node("plan", plan)
        b.add_node("delegate", delegate)
        b.add_edge(START, "plan")
        b.add_edge("plan", "delegate")
        b.add_edge("delegate", END)
        return b.compile(checkpointer=InMemorySaver())

    async def run_parent(self, *, resume: bool = False, retry: DefaultRetryPolicy | None = None) -> ParentState:
        async with with_handler(self.handler, NullEventHandler(), RefuseInterrupts()):
            return await run_to_completion(
                self.parent,
                None if resume else {"log": ["<parent input>"]},
                thread_id=PARENT,
                recursion_limit=10,
                description="parent",
                retry=retry,
            )

    async def child_log(self, thread_id: str) -> list[str]:
        return (await self.child.aget_state({"configurable": {"thread_id": thread_id}})).values.get("log", [])


G0 = durable_thread_id(PARENT, "worker", TOOL_CALL, 0)
G1 = durable_thread_id(PARENT, "worker", TOOL_CALL, 1)


def new_process() -> None:
    """A restarted process remembers nothing about exhausted spawns."""
    install_retry_policy(None)


def test_thread_id_is_a_pure_function_of_the_spawn() -> None:
    assert G0 == f"{PARENT}/worker:{TOOL_CALL}/g0"
    assert G1 != G0
    assert durable_thread_id("other", "worker", TOOL_CALL, 0) != G0


async def test_parent_retry_resumes_the_child_instead_of_respawning() -> None:
    """The child finishes; the parent's delegate node then hits a transient error.
    The parent's floor retry re-enters delegate, whose spawn resolves to the same
    thread and finds it complete: no child node re-runs, the result is the same."""
    h = Harness(child_script=["g", "a"], parent_script=["p"])
    h.raise_after_child = FakeOverloadedError("529")
    backoffs: list[int] = []
    policy = DefaultRetryPolicy(_retry_on(FakeOverloadedError), _recording_backoff(backoffs), max_retries=3)

    result = await h.run_parent(retry=policy)

    assert backoffs == [0]
    assert h.parent_runs == ["plan", "delegate", "delegate"]
    assert h.child_runs == ["gather", "act"]
    assert h.child_threads == [G0]
    assert result["log"][-1] == "delegate: acted: a"


async def test_exhausted_child_bumps_the_generation() -> None:
    """The child's own floor exhausts (its 'act' keeps raising). That bubbles into
    the parent's retry, which re-enters delegate; the spawn now takes generation 1,
    fresh, and the abandoned generation-0 thread keeps its partial state."""
    h = Harness(
        child_script=["g", FakeOverloadedError("1"), FakeOverloadedError("2"), "g", "a"],
        parent_script=["p"],
    )
    backoffs: list[int] = []
    policy = DefaultRetryPolicy(_retry_on(FakeOverloadedError), _recording_backoff(backoffs), max_retries=2)
    install_retry_policy(policy)

    result = await h.run_parent()

    assert h.child_threads == [G0, G1]
    assert h.child_runs == ["gather", "act", "act", "gather", "act"]
    assert h.parent_runs == ["plan", "delegate", "delegate"]
    assert await h.child_log(G0) == ["<child input>", "gathered: g"]
    assert await h.child_log(G1) == ["<child input>", "gathered: g", "acted: a"]
    assert result["log"][-1] == "delegate: acted: a"


async def test_cold_start_resumes_a_child_that_was_in_flight() -> None:
    """Process 1 dies with the child between 'gather' and 'act'. Process 2 resumes the
    parent; the spawn probes the checkpointer, finds generation 0 and no successor,
    and resumes it: 'gather' never runs again."""
    h = Harness(child_script=["g", FakeFatalError("power cut"), "a"], parent_script=["p"])
    with pytest.raises(FakeFatalError):
        await h.run_parent()
    assert h.child_runs == ["gather", "act"]

    new_process()
    result = await h.run_parent(resume=True)

    assert h.child_threads == [G0]
    assert h.child_runs == ["gather", "act", "act"]
    assert h.parent_runs == ["plan", "delegate", "delegate"]
    assert result["log"][-1] == "delegate: acted: a"


async def test_cold_start_skips_generations_proven_abandoned() -> None:
    """Generation 1 existing is the durable proof that generation 0 was given up on:
    a fresh process resumes generation 1 even with an empty registry."""
    h = Harness(
        child_script=["g", FakeOverloadedError("1"), FakeOverloadedError("2"), "g", FakeFatalError("power cut"), "a"],
        parent_script=["p"],
    )
    policy = DefaultRetryPolicy(_retry_on(FakeOverloadedError), _recording_backoff([]), max_retries=2)
    install_retry_policy(policy)
    with pytest.raises(FakeFatalError):
        await h.run_parent()
    assert h.child_threads == [G0, G1]

    new_process()
    result = await h.run_parent(resume=True)

    assert h.child_threads == [G0, G1]  # no third spawn of 'gather'
    assert h.child_runs[-1] == "act"
    assert await h.child_log(G1) == ["<child input>", "gathered: g", "acted: a"]
    assert result["log"][-1] == "delegate: acted: a"


async def test_cold_start_returns_a_finished_child_at_once() -> None:
    """The child completed before process 1 died. Process 2's spawn resumes a thread
    with nothing pending and gets its final state back without running a node."""
    h = Harness(child_script=["g", "a"], parent_script=["p"])
    h.raise_after_child = FakeFatalError("power cut")
    with pytest.raises(FakeFatalError):
        await h.run_parent()
    assert h.child_runs == ["gather", "act"]

    new_process()
    result = await h.run_parent(resume=True)

    assert h.child_runs == ["gather", "act"]
    assert h.child_threads == [G0]
    assert result["log"][-1] == "delegate: acted: a"


async def test_durable_thread_needs_its_anchors() -> None:
    h = Harness(child_script=["g", "a"], parent_script=["p"])
    async with with_handler(h.handler, NullEventHandler(), RefuseInterrupts()):
        with pytest.raises(ValueError, match="within_tool"):
            await run_to_completion(
                h.child, {"log": []}, thread_id=DurableThread("worker"), recursion_limit=10, description="x"
            )
        with pytest.raises(ValueError, match="enclosing"):
            await run_to_completion(
                h.child, {"log": []}, thread_id=DurableThread("worker"), within_tool=TOOL_CALL,
                recursion_limit=10, description="x",
            )
