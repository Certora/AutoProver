"""Re-running an interrupted run on its own thread must continue it, not restart it.

Codegen takes ``--thread-id``, so the way to pick up a crashed run is to invoke
the same command again against the same thread. That only works if the second
invocation resumes the thread's last checkpoint: langgraph treats a non-``None``
input as an update from ``START``, so passing the original input again re-enters
the entry node on top of a message history that already holds an initial prompt.
The provider then rejects the request outright ("multiple non-consecutive system
messages"), which is how the bug surfaced in the wild rather than as a quiet
duplicate.

The graphs here are two-node pregel loops over an in-memory checkpointer, with
an entry node that injects a system prompt the way graphcore's does. No LLM, no
Postgres.
"""
import operator
import uuid
from typing import Annotated, Any, TypedDict

import pytest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

from composer.io.context import (
    latest_checkpoint_of,
    run_to_completion,
    with_handler,
)
from composer.io.event_handler import NullEventHandler

pytestmark = pytest.mark.asyncio


class ResumeState(TypedDict):
    messages: Annotated[list[str], operator.add]


class _NullIOHandler:
    async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str) -> None:
        pass

    async def log_state_update(self, path: list[str], st: dict) -> None:
        pass

    async def log_start(self, *, path: list[str], description: str, tool_id: str | None) -> None:
        pass

    async def log_end(self, path: list[str]) -> None:
        pass

    async def human_interaction(self, ty: Any, debug_thunk: Any) -> str:
        raise AssertionError("no HITL interaction expected in resume tests")


class _WedgedOnce(Exception):
    """The crash that ends the first invocation."""


def _build_graph(
    saver: InMemorySaver, node_runs: list[str], fail_on_first_work: bool = True
) -> CompiledStateGraph[ResumeState, None, ResumeState, ResumeState]:
    """entry -> work. ``entry`` injects the system prompt (as graphcore's entry
    node does); ``work`` raises the first time it is reached."""
    work_calls = 0

    async def entry(state: ResumeState) -> dict:
        node_runs.append("entry")
        return {"messages": ["system: you author CVL"]}

    async def work(state: ResumeState) -> dict:
        nonlocal work_calls
        work_calls += 1
        node_runs.append("work")
        if fail_on_first_work and work_calls == 1:
            raise _WedgedOnce("wedged")
        return {"messages": ["assistant: done"]}

    builder = StateGraph(ResumeState)
    builder.add_node("entry", entry)
    builder.add_node("work", work)
    builder.add_edge(START, "entry")
    builder.add_edge("entry", "work")
    builder.add_edge("work", END)
    return builder.compile(checkpointer=saver)


async def _run(
    graph: CompiledStateGraph[ResumeState, None, ResumeState, ResumeState],
    *,
    thread_id: str,
    checkpoint_id: str | None = None,
) -> ResumeState:
    async with with_handler(_NullIOHandler(), NullEventHandler()):
        return await run_to_completion(
            graph,
            {"messages": ["user: implement the contract"]},
            thread_id=thread_id,
            context=None,
            recursion_limit=25,
            description="resume test",
            checkpoint_id=checkpoint_id,
        )


async def test_a_fresh_thread_has_no_checkpoint_to_resume():
    saver = InMemorySaver()
    assert await latest_checkpoint_of(saver, uuid.uuid1().hex) is None


async def test_rerunning_the_same_thread_resumes_instead_of_re_entering():
    saver = InMemorySaver()
    node_runs: list[str] = []
    graph = _build_graph(saver, node_runs)
    tid = uuid.uuid1().hex

    with pytest.raises(_WedgedOnce):
        await _run(graph, thread_id=tid)
    assert node_runs == ["entry", "work"]

    resume_from = await latest_checkpoint_of(saver, tid)
    assert resume_from is not None, "the crashed attempt should have left a checkpoint"

    final = await _run(graph, thread_id=tid, checkpoint_id=resume_from)

    # The completed node stays completed; only the failed one re-runs.
    assert node_runs == ["entry", "work", "work"]
    assert final["messages"].count("system: you author CVL") == 1
    assert final["messages"].count("user: implement the contract") == 1
    assert final["messages"][-1] == "assistant: done"


async def test_without_the_resume_point_the_entry_node_runs_twice():
    """The regression this guards against: the same second invocation, minus the
    resume point, re-enters ``entry`` and duplicates the initial prompt."""
    saver = InMemorySaver()
    node_runs: list[str] = []
    graph = _build_graph(saver, node_runs)
    tid = uuid.uuid1().hex

    with pytest.raises(_WedgedOnce):
        await _run(graph, thread_id=tid)

    final = await _run(graph, thread_id=tid)

    assert node_runs == ["entry", "work", "entry", "work"]
    assert final["messages"].count("system: you author CVL") == 2


async def test_resuming_a_finished_thread_is_a_no_op():
    """A re-run of a thread that already completed returns its final state
    rather than authoring a second time."""
    saver = InMemorySaver()
    node_runs: list[str] = []
    graph = _build_graph(saver, node_runs, fail_on_first_work=False)
    tid = uuid.uuid1().hex

    first = await _run(graph, thread_id=tid)
    assert node_runs == ["entry", "work"]

    resume_from = await latest_checkpoint_of(saver, tid)
    assert resume_from is not None
    again = await _run(graph, thread_id=tid, checkpoint_id=resume_from)

    assert node_runs == ["entry", "work"]
    assert again["messages"] == first["messages"]
