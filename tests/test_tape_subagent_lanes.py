"""Tape lanes for sub-agents: a sub-agent started from a tool is recorded to, and replayed from, a
lane named by the tool call that started it.

The failure this exists for: two ``cvlr_research`` sub-agents started in one author turn shared the
author's lane, and the replay served their turns in whichever order they happened to ask — not the
order they were recorded in. A researcher handed its sibling's ``result`` before writing a draft was
rejected, the extra turn shifted the lane, and the author ended up served turns that were not its own.
"""

import asyncio
from typing import Annotated, Any, TypedDict
import operator

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from composer.diagnostics.timing import set_current_task_id
from composer.io.context import get_spawning_tool_calls, run_to_completion, with_handler
from composer.io.event_handler import NullEventHandler
from composer.testing import record_tape
from composer.testing.harness_tape import HarnessFakeLLM, tape_lane_path

pytestmark = pytest.mark.asyncio


class _State(TypedDict):
    log: Annotated[list[str], operator.add]


class _QuietIOHandler:
    async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str) -> None:
        pass

    async def log_state_update(self, path: list[str], st: dict) -> None:
        pass

    async def log_start(self, *, path: list[str], description: str, tool_id: str | None) -> None:
        pass

    async def log_end(self, path: list[str]) -> None:
        pass

    async def human_interaction(self, ty: Any, debug_thunk: Any) -> str:
        raise AssertionError("no HITL interaction expected")


def _graph(node):
    builder = StateGraph(_State)
    builder.add_node("work", node)
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    return builder.compile(checkpointer=InMemorySaver())


async def _run(graph, thread_id: str, within_tool: str | None = None) -> _State:
    return await run_to_completion(
        graph, {"log": []}, thread_id=thread_id, recursion_limit=25,
        description=thread_id, within_tool=within_tool,
    )


def _fake(lanes: dict[str, list[str]]) -> HarnessFakeLLM:
    return HarnessFakeLLM(
        lanes={k: [AIMessage(content=c) for c in v] for k, v in lanes.items()},
        with_human_delay=False,
    )


async def test_concurrent_subagents_each_replay_their_own_lane_whatever_order_they_ask_in():
    fake = _fake({
        "formalize-0/toolu_A": ["A1", "A2"],
        "formalize-0/toolu_B": ["B1", "B2"],
    })
    # A was recorded first; here it waits until B has asked twice.
    b_done = asyncio.Event()

    async def researcher_a(state: _State) -> dict:
        await b_done.wait()
        return {"log": [str((await fake.ainvoke("q")).content) for _ in range(2)]}

    async def researcher_b(state: _State) -> dict:
        answers = [str((await fake.ainvoke("q")).content) for _ in range(2)]
        b_done.set()
        return {"log": answers}

    with set_current_task_id("formalize-0"):
        async with with_handler(_QuietIOHandler(), NullEventHandler()):
            a, b = await asyncio.gather(
                _run(_graph(researcher_a), "research-a", within_tool="toolu_A"),
                _run(_graph(researcher_b), "research-b", within_tool="toolu_B"),
            )

    assert a["log"] == ["A1", "A2"]
    assert b["log"] == ["B1", "B2"]


async def test_a_nested_subagent_extends_its_callers_path_and_the_caller_keeps_its_own():
    seen: dict[str, Any] = {}

    async def grandchild_node(state: _State) -> dict:
        seen["grandchild"] = tape_lane_path()
        return {"log": []}

    grandchild = _graph(grandchild_node)

    async def child(state: _State) -> dict:
        seen["child"] = tape_lane_path()
        await _run(grandchild, "grandchild", within_tool="toolu_inner")
        seen["child_after"] = get_spawning_tool_calls()
        return {"log": []}

    with set_current_task_id("formalize-2"):
        async with with_handler(_QuietIOHandler(), NullEventHandler()):
            await _run(_graph(child), "child", within_tool="toolu_outer")
            seen["top"] = tape_lane_path()

    assert seen["child"] == ("formalize-2", "toolu_outer")
    assert seen["grandchild"] == ("formalize-2", "toolu_outer", "toolu_inner")
    assert seen["child_after"] == ("toolu_outer",)
    assert seen["top"] == ("formalize-2",)


async def test_a_graph_run_without_a_spawning_tool_stays_in_its_callers_lane():
    async def node(state: _State) -> dict:
        return {"log": [repr(tape_lane_path())]}

    with set_current_task_id("report"):
        async with with_handler(_QuietIOHandler(), NullEventHandler()):
            state = await _run(_graph(node), "report-thread")

    assert state["log"] == [repr(("report",))]


async def test_a_tape_without_subagent_lanes_serves_the_subagent_from_its_callers_lane():
    """The hand-authored tapes interleave a judge's turns into its caller's lane."""
    fake = _fake({"cvl-0": ["author", "judge"]})

    async def judge(state: _State) -> dict:
        return {"log": [str((await fake.ainvoke("q")).content)]}

    with set_current_task_id("cvl-0"):
        first = await fake.ainvoke("q")
        async with with_handler(_QuietIOHandler(), NullEventHandler()):
            state = await _run(_graph(judge), "judge", within_tool="toolu_feedback")

    assert first.content == "author"
    assert state["log"] == ["judge"]


async def test_the_recorder_files_a_subagents_turn_under_its_full_path(tmp_path):
    recorder = record_tape.TapeRecorder("t", tmp_path / "ui_harness_t.py")

    async def researcher(state: _State) -> dict:
        recorder.record(AIMessage(content="answer"))
        return {"log": []}

    with set_current_task_id("formalize-1"):
        recorder.record(AIMessage(content="author"))
        async with with_handler(_QuietIOHandler(), NullEventHandler()):
            await _run(_graph(researcher), "research", within_tool="toolu_R")

    assert {k: [m.content for m in v] for k, v in recorder.lanes.items()} == {
        "formalize-1": ["author"],
        "formalize-1/toolu_R": ["answer"],
    }
