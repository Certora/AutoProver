"""Tests for the retry machinery in ``composer.io.context`` — the ambient
(run-wide) retry floor, per-run overrides, and the ``FreshRetryPolicy``
escalation, all first-class in ``run_graph``.

The graphs are static pregel loops (plan → author) over in-memory
checkpointers; nodes consult a scripted ``FlakyLLM`` that raises on the turns
the script says to. No Postgres, no real backoff sleeps (policies get a
recording backoff), no LLM.

What the scenarios pin down:

* a retryable failure resumes from the LAST CHECKPOINT — completed nodes do
  not re-run, the failed node does, and the backoff is consulted with the
  failed attempt's index;
* a non-retryable failure propagates immediately, no backoff consulted;
* the ambient floor (``install_retry_policy``) applies when no per-run policy
  is passed — and to NESTED sub-graph runs, which each track their own
  checkpoints via their own sink wrapper;
* a per-run policy that is a ``RetryPolicy`` overrides the ambient floor;
* the state-backoff ladder: a sub-agent that EXHAUSTS its floor bubbles into
  the parent's floor retry, which re-enters the spawning node — respawning
  the sub-agent on a fresh thread id with pristine state, while the exhausted
  spawn's thread is abandoned mid-flight;
* a plain ``FreshRetryPolicy`` (deliberately NOT a ``RetryPolicy``) rides the
  ambient floor for transient failures while escalating wedged ones by
  rebuilding the input on a fresh thread — attempt 1's state does not leak;
* exhaustion, both loops: a retryable failure on the final attempt propagates
  as-is — no parting backoff, no pointless final ``rebuild_input``.
"""
import operator
import uuid
from contextlib import contextmanager
from typing import Annotated, Any, TypedDict, override

import pytest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

from composer.io.context import (
    Backoff,
    DefaultRetryPolicy,
    FreshRetryPolicy,
    RetryPolicy,
    install_retry_policy,
    run_to_completion,
    with_handler,
)
from composer.io.event_handler import NullEventHandler

pytestmark = pytest.mark.asyncio


class RetryState(TypedDict):
    log: Annotated[list[str], operator.add]


class FakeOverloadedError(Exception):
    """Stands in for the transiently-retryable class (529/rate limit)."""


class FakeCorruptedError(Exception):
    """Stands in for a wedged-thread failure that needs a fresh start."""


class FakeFatalError(Exception):
    """Retryable by no policy."""


def _retry_on(*types: type[Exception]):
    return lambda e: isinstance(e, types)


class FlakyLLM:
    """Scripted fake: each ``ainvoke`` consumes the next script entry —
    a str to return, or an exception instance to raise."""

    def __init__(self, script: list[str | Exception]):
        self.script = list(script)
        self.calls = 0

    async def ainvoke(self, prompt: str) -> str:
        i = self.calls
        self.calls += 1
        assert i < len(self.script), f"FlakyLLM script exhausted at call {i} ({prompt!r})"
        item = self.script[i]
        if isinstance(item, Exception):
            raise item
        return item


class _RecordingIOHandler:
    """IOHandler that records log_start descriptions (the retry loops label
    attempts through them) and refuses HITL (none expected here)."""

    def __init__(self) -> None:
        self.descriptions: list[str] = []

    async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str) -> None:
        pass

    async def log_state_update(self, path: list[str], st: dict) -> None:
        pass

    async def log_start(self, *, path: list[str], description: str, tool_id: str | None) -> None:
        self.descriptions.append(description)

    async def log_end(self, path: list[str]) -> None:
        pass

    async def human_interaction(self, ty: Any, debug_thunk: Any) -> str:
        raise AssertionError("no HITL interaction expected in retry tests")


def _recording_backoff(record: list[int]) -> Backoff:
    async def _backoff(i: int) -> None:
        record.append(i)
    return _backoff


@contextmanager
def _ambient_policy(policy: RetryPolicy):
    tok = install_retry_policy(policy)
    try:
        yield
    finally:
        tok.var.reset(tok)


def _build_graph(
    llm: FlakyLLM, node_runs: list[str]
) -> CompiledStateGraph[RetryState, None, RetryState, RetryState]:
    async def plan(state: RetryState) -> dict:
        node_runs.append("plan")
        return {"log": [f"plan: {await llm.ainvoke('plan')}"]}

    async def author(state: RetryState) -> dict:
        node_runs.append("author")
        return {"log": [f"author: {await llm.ainvoke('author')}"]}

    builder = StateGraph(RetryState)
    builder.add_node("plan", plan)
    builder.add_node("author", author)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "author")
    builder.add_edge("author", END)
    return builder.compile(checkpointer=InMemorySaver())


async def _run(
    graph: CompiledStateGraph[RetryState, None, RetryState, RetryState],
    handler: _RecordingIOHandler,
    *,
    thread_id: str,
    retry: "RetryPolicy | FreshRetryPolicy[Any, Any] | None" = None,
) -> RetryState:
    async with with_handler(handler, NullEventHandler()):
        return await run_to_completion(
            graph,
            {"log": ["<input>"]},
            thread_id=thread_id,
            context=None,
            recursion_limit=25,
            description="retry test",
            retry=retry,
        )


# =========================================================================
# Floor policy: resume-from-checkpoint, propagation, exhaustion
# =========================================================================


async def test_retryable_failures_resume_from_last_checkpoint():
    node_runs: list[str] = []
    llm = FlakyLLM([
        "planned",
        FakeOverloadedError("529 attempt 1"),
        FakeOverloadedError("529 attempt 2"),
        "authored",
    ])
    backoffs: list[int] = []
    handler = _RecordingIOHandler()
    policy = DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(backoffs), max_retries=3,
    )

    result = await _run(_build_graph(llm, node_runs), handler, thread_id="retry-resume", retry=policy)

    assert result["log"] == ["<input>", "plan: planned", "author: authored"]
    # Resume, not restart: plan's checkpoint survived the failures, so plan ran
    # exactly once while the failing author task re-ran on each attempt.
    assert node_runs == ["plan", "author", "author", "author"]
    assert llm.calls == 4
    # Backoff consulted once per failed attempt, with that attempt's index.
    assert backoffs == [0, 1]
    # Attempts are labeled for the handler.
    assert handler.descriptions == [
        "retry test", "retry test (Retry 1)", "retry test (Retry 2)",
    ]


async def test_non_retryable_failure_propagates_without_backoff():
    node_runs: list[str] = []
    llm = FlakyLLM(["planned", FakeFatalError("boom")])
    backoffs: list[int] = []
    handler = _RecordingIOHandler()
    policy = DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(backoffs), max_retries=3,
    )

    with pytest.raises(FakeFatalError):
        await _run(_build_graph(llm, node_runs), handler, thread_id="retry-fatal", retry=policy)

    assert node_runs == ["plan", "author"]
    assert backoffs == []


async def test_retry_exhaustion_raises_last_error_without_parting_backoff():
    node_runs: list[str] = []
    llm = FlakyLLM([
        "planned",
        FakeOverloadedError("529 attempt 1"),
        FakeOverloadedError("529 attempt 2"),
    ])
    backoffs: list[int] = []
    handler = _RecordingIOHandler()
    policy = DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(backoffs), max_retries=2,
    )

    with pytest.raises(FakeOverloadedError, match="attempt 2"):
        await _run(_build_graph(llm, node_runs), handler, thread_id="retry-exhausted", retry=policy)

    assert node_runs == ["plan", "author", "author"]
    # Backoff paid between attempts only — the final failure exits immediately.
    assert backoffs == [0]


# =========================================================================
# Ambient floor: applies by default, overridable per-run, reaches sub-graphs
# =========================================================================


async def test_ambient_floor_applies_without_explicit_policy():
    node_runs: list[str] = []
    llm = FlakyLLM(["planned", FakeOverloadedError("blip"), "authored"])
    backoffs: list[int] = []
    handler = _RecordingIOHandler()

    with _ambient_policy(DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(backoffs), max_retries=3,
    )):
        result = await _run(_build_graph(llm, node_runs), handler, thread_id="ambient-floor")

    assert result["log"] == ["<input>", "plan: planned", "author: authored"]
    assert node_runs == ["plan", "author", "author"]
    assert backoffs == [0]


async def test_explicit_policy_overrides_the_ambient_floor():
    """A per-run RetryPolicy replaces the ambient floor entirely: the ambient
    policy would have retried this failure, the override refuses to."""
    node_runs: list[str] = []
    llm = FlakyLLM(["planned", FakeOverloadedError("would be retried ambiently")])
    ambient_backoffs: list[int] = []
    override_backoffs: list[int] = []
    handler = _RecordingIOHandler()

    with _ambient_policy(DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(ambient_backoffs), max_retries=3,
    )):
        with pytest.raises(FakeOverloadedError):
            await _run(
                _build_graph(llm, node_runs), handler, thread_id="floor-override",
                retry=DefaultRetryPolicy(
                    lambda e: False, backoff=_recording_backoff(override_backoffs), max_retries=3,
                ),
            )

    assert node_runs == ["plan", "author"]
    assert ambient_backoffs == []
    assert override_backoffs == []


async def test_nested_subgraph_failures_retry_under_the_ambient_floor():
    """The capability the sink-wrapper tracking buys: a transient failure
    inside a NESTED graph run retries from the nested run's own checkpoint,
    without the failure ever reaching (or re-running) the parent."""
    child_runs: list[str] = []
    child_llm = FlakyLLM([FakeOverloadedError("nested blip"), "child works"])
    backoffs: list[int] = []
    handler = _RecordingIOHandler()

    async def child_work(state: RetryState) -> dict:
        child_runs.append("child-work")
        return {"log": [f"child: {await child_llm.ainvoke('child')}"]}

    child_builder = StateGraph(RetryState)
    child_builder.add_node("child_work", child_work)
    child_builder.add_edge(START, "child_work")
    child_builder.add_edge("child_work", END)
    child_graph = child_builder.compile(checkpointer=InMemorySaver())

    parent_runs: list[str] = []

    async def delegate(state: RetryState) -> dict:
        parent_runs.append("delegate")
        child_state = await run_to_completion(
            child_graph,
            {"log": ["<child input>"]},
            thread_id="nested-child",
            context=None,
            recursion_limit=25,
            description="nested child",
        )
        return {"log": [f"delegate got: {child_state['log'][-1]}"]}

    parent_builder = StateGraph(RetryState)
    parent_builder.add_node("delegate", delegate)
    parent_builder.add_edge(START, "delegate")
    parent_builder.add_edge("delegate", END)
    parent_graph = parent_builder.compile(checkpointer=InMemorySaver())

    with _ambient_policy(DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(backoffs), max_retries=3,
    )):
        async with with_handler(handler, NullEventHandler()):
            result = await run_to_completion(
                parent_graph,
                {"log": ["<input>"]},
                thread_id="nested-parent",
                context=None,
                recursion_limit=25,
                description="parent",
            )

    # The nested failure was retried in place: the child node re-ran, the
    # parent's delegate node ran exactly once, and the result flowed through.
    assert child_runs == ["child-work", "child-work"]
    assert parent_runs == ["delegate"]
    assert backoffs == [0]
    assert result["log"] == ["<input>", "delegate got: child: child works"]
    # The retry was labeled on the nested run, not the parent.
    assert "nested child (Retry 1)" in handler.descriptions
    assert "parent (Retry 1)" not in handler.descriptions


async def test_exhausted_subagent_escalates_to_parent_and_respawns_fresh():
    """The state-backoff ladder, end to end. Spawn 1's inner floor retries
    resume the sub-agent's own checkpoint (level 1); once exhausted, the
    failure bubbles into the parent's floor retry (level 2), which resumes
    the PARENT's checkpoint and re-enters the spawning node — respawning the
    sub-agent under a fresh thread id with pristine state. Each level rolls
    back strictly more state."""
    act_llm = FlakyLLM([
        FakeOverloadedError("spawn 1, act attempt 1"),
        FakeOverloadedError("spawn 1, act attempt 2"),
        FakeOverloadedError("spawn 2, act attempt 1"),
        "acted",
    ])
    child_runs: list[str] = []
    gather_views: list[tuple[str, ...]] = []

    async def gather(state: RetryState) -> dict:
        child_runs.append("gather")
        gather_views.append(tuple(state["log"]))
        return {"log": ["gathered"]}

    async def act(state: RetryState) -> dict:
        child_runs.append("act")
        return {"log": [f"act: {await act_llm.ainvoke('act')}"]}

    child_builder = StateGraph(RetryState)
    child_builder.add_node("gather", gather)
    child_builder.add_node("act", act)
    child_builder.add_edge(START, "gather")
    child_builder.add_edge("gather", "act")
    child_builder.add_edge("act", END)
    child_graph = child_builder.compile(checkpointer=InMemorySaver())

    parent_runs: list[str] = []
    spawned_tids: list[str] = []

    async def plan(state: RetryState) -> dict:
        parent_runs.append("plan")
        return {"log": ["planned"]}

    async def delegate(state: RetryState) -> dict:
        parent_runs.append("delegate")
        # Mirrors how tool bodies spawn sub-agents: a unique thread id per
        # spawn, so a parent-level retry starts the sub-agent over instead of
        # resuming the exhausted spawn's conversation.
        tid = f"ladder-child-{uuid.uuid4().hex}"
        spawned_tids.append(tid)
        child_state = await run_to_completion(
            child_graph,
            {"log": ["<child input>"]},
            thread_id=tid,
            context=None,
            recursion_limit=25,
            description="child work",
        )
        return {"log": [f"delegate got: {child_state['log'][-1]}"]}

    parent_builder = StateGraph(RetryState)
    parent_builder.add_node("plan", plan)
    parent_builder.add_node("delegate", delegate)
    parent_builder.add_edge(START, "plan")
    parent_builder.add_edge("plan", "delegate")
    parent_builder.add_edge("delegate", END)
    parent_graph = parent_builder.compile(checkpointer=InMemorySaver())

    backoffs: list[int] = []
    handler = _RecordingIOHandler()
    with _ambient_policy(DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(backoffs), max_retries=2,
    )):
        async with with_handler(handler, NullEventHandler()):
            result = await run_to_completion(
                parent_graph,
                {"log": ["<input>"]},
                thread_id="ladder-parent",
                context=None,
                recursion_limit=25,
                description="parent ladder",
            )

    # Level 1 (within a spawn): inner retries RESUME — act re-ran without
    # gather re-running. Level 2 (across spawns): the respawn is FRESH —
    # gather ran again.
    assert child_runs == ["gather", "act", "act", "gather", "act", "act"]
    # The parent resumed from its checkpoint: plan ran once, delegate re-ran.
    assert parent_runs == ["plan", "delegate", "delegate"]
    # Two distinct spawns...
    assert len(spawned_tids) == 2 and spawned_tids[0] != spawned_tids[1]
    # ...and both saw pristine state: no trace of the sibling spawn's history.
    assert gather_views == [("<child input>",), ("<child input>",)]
    # One backoff per failure that had a next attempt: spawn 1's inner retry,
    # the parent's retry, spawn 2's inner retry — spawn 1's exhausting second
    # failure paid none.
    assert backoffs == [0, 0, 0]
    assert result["log"] == ["<input>", "planned", "delegate got: act: acted"]
    # The exhausted spawn's thread is abandoned mid-flight; the fresh spawn's
    # completed — "backing off the state".
    spawn1 = (await child_graph.aget_state({"configurable": {"thread_id": spawned_tids[0]}})).values
    spawn2 = (await child_graph.aget_state({"configurable": {"thread_id": spawned_tids[1]}})).values
    assert spawn1["log"] == ["<child input>", "gathered"]
    assert spawn2["log"] == ["<child input>", "gathered", "act: acted"]
    # Each level labels its own attempts.
    assert handler.descriptions == [
        "parent ladder",
        "child work", "child work (Retry 1)",
        "parent ladder (Retry 1)",
        "child work", "child work (Retry 1)",
    ]


# =========================================================================
# Fresh-start escalation
# =========================================================================


class _RecordingFreshPolicy(FreshRetryPolicy[Any, Any]):
    """Fresh-only escalation — deliberately NOT a RetryPolicy, so the floor
    (if any) comes from the ambient installation."""

    max_fresh_retries = 2

    def __init__(self, next_thread_id: str):
        self._next_thread_id = next_thread_id
        self.rebuilds: list[tuple[Any, Any]] = []

    @override
    def should_retry_fresh(self, exc: Exception) -> bool:
        return isinstance(exc, FakeCorruptedError)

    @override
    async def rebuild_input(self, last_state: Any, last_input: Any) -> tuple[Any, str]:
        self.rebuilds.append((last_state, last_input))
        return ({"log": ["<fresh input>"]}, self._next_thread_id)


async def test_fresh_start_rebuilds_input_on_a_fresh_thread():
    node_runs: list[str] = []
    llm = FlakyLLM([
        "planned v1",
        FakeCorruptedError("thread is wedged"),
        "planned v2",
        "authored v2",
    ])
    handler = _RecordingIOHandler()
    policy = _RecordingFreshPolicy(next_thread_id="retry-fresh-2")

    result = await _run(_build_graph(llm, node_runs), handler, thread_id="retry-fresh-1", retry=policy)

    # The fresh attempt ran on the rebuilt input, and attempt 1's state did NOT
    # leak into it: a fresh thread id is a genuinely fresh history.
    assert result["log"] == ["<fresh input>", "plan: planned v2", "author: authored v2"]
    # Restart, not resume: the whole graph re-ran on the fresh thread.
    assert node_runs == ["plan", "author", "plan", "author"]
    # rebuild_input saw the crashed attempt's checkpointed state (plan's work)
    # and the input that attempt ran with.
    assert len(policy.rebuilds) == 1
    (last_state, last_input) = policy.rebuilds[0]
    assert last_state["log"] == ["<input>", "plan: planned v1"]
    assert last_input == {"log": ["<input>"]}
    assert handler.descriptions == ["retry test", "retry test (Attempt 1)"]


async def test_fresh_only_policy_rides_the_ambient_floor():
    """The decoupling's point: a fresh-only policy gets transient failures
    handled by the ambient floor AND wedged failures escalated — without
    restating the floor."""
    node_runs: list[str] = []
    llm = FlakyLLM([
        "planned v1",
        FakeOverloadedError("transient blip"),
        FakeCorruptedError("wedged"),
        "planned v2",
        "authored v2",
    ])
    backoffs: list[int] = []
    handler = _RecordingIOHandler()
    policy = _RecordingFreshPolicy(next_thread_id="fresh-floor-2")

    with _ambient_policy(DefaultRetryPolicy(
        _retry_on(FakeOverloadedError), backoff=_recording_backoff(backoffs), max_retries=3,
    )):
        result = await _run(
            _build_graph(llm, node_runs), handler, thread_id="fresh-floor-1", retry=policy,
        )

    assert result["log"] == ["<fresh input>", "plan: planned v2", "author: authored v2"]
    # The transient failure was floor-retried in place (author re-ran); the
    # wedged failure escalated to a fresh start (whole graph re-ran).
    assert node_runs == ["plan", "author", "author", "plan", "author"]
    assert backoffs == [0]
    assert len(policy.rebuilds) == 1


async def test_fresh_start_exhaustion_raises_without_final_rebuild():
    node_runs: list[str] = []
    llm = FlakyLLM([
        "planned v1",
        FakeCorruptedError("wedged, first time"),
        "planned v2",
        FakeCorruptedError("wedged, second time"),
    ])
    handler = _RecordingIOHandler()
    policy = _RecordingFreshPolicy(next_thread_id="fresh-exhausted-2")
    assert policy.max_fresh_retries == 2

    with pytest.raises(FakeCorruptedError, match="second time"):
        await _run(_build_graph(llm, node_runs), handler, thread_id="fresh-exhausted-1", retry=policy)

    assert node_runs == ["plan", "author", "plan", "author"]
    # Rebuilt once, between the attempts — the final failure propagates
    # without a pointless rebuild of an input that would never run.
    assert len(policy.rebuilds) == 1
