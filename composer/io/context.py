"""
Context-scoped handler installation and graph execution.

This module is the glue between graph execution and event handling.
It provides two public entry points:

``with_handler(io_handler, event_handler)``
    Async context manager that installs a handler pair into a
    ``ContextVar``, creates an ``EventQueue``, and runs a
    background ``_queue_drainer`` task.  All ``run_graph()`` calls
    within the scope push events to this queue.

``run_graph(graph, ctxt, input, run_conf, description)``
    High-level wrapper that reads the installed handlers from the
    context, constructs an event sink (with automatic nesting
    support), and delegates to ``graph_runner.run_graph()``.  Also
    bridges HITL interrupts to ``IOHandler.human_interaction()``.

Nesting is automatic: if ``run_graph()`` is called while another
``run_graph()`` is already active (same ``with_handler`` scope),
the inner call's sink wraps events with ``Nested(event,
parent_id=outer_tid)`` before pushing to the queue.  The drainer
peels these layers to reconstruct the full execution path.
"""

from abc import ABC, abstractmethod
from contextvars import ContextVar, Token
from contextlib import asynccontextmanager

import asyncio


from composer.io.protocol import IOHandler
from composer.io.stream import EventQueue
from composer.io.event_handler import EventHandler

from typing import Any, Awaitable, Callable, Mapping, Protocol, cast, override

from composer.io.events import (
    AllEvents, InnerEvent, Nested, NextCheckpoint,
    CustomUpdate, StateUpdate, Start, End, GraphEvents, ProgressEvent
)
from composer.diagnostics.jsonl_sink import emit as _emit_jsonl

from langgraph._internal._typing import StateLike
from langgraph.graph.state import CompiledStateGraph

from langchain_core.runnables import RunnableConfig

from composer.io.graph_runner import SinkProtocol, run_graph as _run_graph
from langgraph._internal._typing import StateLike

_io_handler : ContextVar[None | tuple[EventQueue, IOHandler[Any], EventHandler]] = ContextVar("_io_handler", default=None)

_current_sink : ContextVar[tuple[SinkProtocol, str] | None] = ContextVar("_current_sink", default=None)
"""Tracks the active event sink and thread_id for nesting detection.

Set by ``run_graph()``; when non-None at the start of a new
``run_graph()`` call, the new call is nested and wraps the parent's
sink with ``Nested(...)``."""


def _unwrap(event: GraphEvents) -> tuple[list[str], InnerEvent]:
    """Peel off Nested layers, collecting parent_ids into a path prefix."""
    path: list[str] = []
    while isinstance(event, Nested):
        path.append(event.parent_id)
        event = event.inner
    return (path, event)


async def _queue_drainer(
    q: EventQueue,
    h: IOHandler[Any],
    event_handler: EventHandler
):
    """Background task: consume events and dispatch to handlers.

    Structural events (``Start``, ``End``, ``StateUpdate``,
    ``NextCheckpoint``) go to the ``IOHandler``.  ``CustomUpdate``
    events go to the ``EventHandler``.  ``Nested`` wrappers are
    peeled off to reconstruct the execution path.
    """
    async for e in q.stream_events():
        if isinstance(e, ProgressEvent):
            _emit_jsonl(e, path=[])
            await event_handler.handle_progress_event(e.payload)
            continue
        (parents, inner) = _unwrap(e)
        full_path = parents + [inner.thread_id]
        _emit_jsonl(inner, path=full_path)
        match inner:
            case Start():
                await h.log_start(path=full_path, description=inner.description, tool_id=inner.tool_id)
            case End():
                await h.log_end(full_path)
            case NextCheckpoint():
                await h.log_checkpoint_id(path=full_path, checkpoint_id=inner.checkpoint_id)
            case CustomUpdate():
                await event_handler.handle_event(inner.payload, full_path, inner.checkpoint_id)
            case StateUpdate():
                await h.log_state_update(full_path, inner.payload)

@asynccontextmanager
async def with_handler(
    h: IOHandler[Any],
    event_handler: EventHandler
):
    """Install a handler pair and run a background drainer for this scope.

    All ``run_graph()`` calls within the scope push events to the
    same ``EventQueue``.  On exit, the drainer is cancelled and
    the context var is restored.
    """
    ev_queue = EventQueue(
        asyncio.Event(),
        []
    )
    tok = _io_handler.set((ev_queue, h, event_handler))
    background_task = asyncio.create_task(
        _queue_drainer(ev_queue, h, event_handler)
    )
    try:
        yield
    finally:
        # Drain events still queued when the scope exits (e.g. AutoSetup's
        # completion event, emitted just before its run_task returns with no
        # further await to let the drainer catch up) instead of cancelling the
        # drainer and dropping them. Fall back to cancellation if a handler hangs.
        ev_queue.close()
        try:
            await asyncio.wait_for(background_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        _io_handler.reset(tok)

def emit_custom_event(payload: Mapping[str, Any]):
    curr_io = _io_handler.get()
    if curr_io is None:
        raise ValueError("No IO handler installed")
    curr_io[0].push(ProgressEvent(dict(payload)))


async def run_graph[S: StateLike, C: StateLike | None, I: StateLike](
    graph: CompiledStateGraph[S, C, I, Any],
    ctxt: C,
    input: I,
    run_conf: RunnableConfig,
    description: str,
    within_tool: str | None = None,
    retry: "RetryPolicy | FreshRetryPolicy[S, I] | None" = None,
) -> S:
    """Execute a graph within the current ``with_handler`` scope.

    Constructs an event sink that pushes to the scope's
    ``EventQueue``.  If another ``run_graph()`` is already active
    in the same scope, the sink wraps events with ``Nested`` so
    the drainer can reconstruct the execution path.

    HITL interrupts are bridged to ``IOHandler.human_interaction()``.

    Retry is first-class: the floor policy (``retry`` when it is a
    :class:`RetryPolicy`, else the ambient :func:`install_retry_policy` one)
    re-runs transient failures from the last checkpoint this run streamed; a
    :class:`FreshRetryPolicy` (``retry`` when it is one) escalates wedged
    failures by rebuilding the input on a fresh thread. Checkpoints are
    tracked by wrapping THIS run's sink — children's checkpoints arrive
    ``Nested``-wrapped and don't register — so retry works for nested
    (sub-agent) runs, each tracking only its own thread.
    """
    curr_io = _io_handler.get()
    if curr_io is None:
        raise ValueError("No IO handler installed")

    (ev, handle, _) = curr_io

    # Determine thread_id from config
    configurable = run_conf.get("configurable", {})
    tid = configurable.get("thread_id")
    if tid is None:
        raise ValueError("thread_id required in run config")

    # Determine sink: top-level uses queue.push, nested wraps parent's sink
    parent = _current_sink.get()
    if parent is None:
        sink: SinkProtocol = ev.push
    else:
        (parent_sink, parent_tid) = parent
        sink = lambda event: parent_sink(Nested(event, parent_id=parent_tid))

    # The most recent checkpoint THIS run committed (seeded from an explicit
    # resume point when the caller passed one) — the resume anchor for floor
    # retries. Recorded by wrapping this run's own sink rather than the
    # scope's queue, so concurrent and nested runs never cross-talk.
    last_checkpoint: str | None = configurable.get("checkpoint_id")

    def tracking_sink(event: GraphEvents) -> None:
        nonlocal last_checkpoint
        if isinstance(event, NextCheckpoint):
            last_checkpoint = event.checkpoint_id
        sink(event)

    floor = retry if isinstance(retry, RetryPolicy) else _run_retry_policy.get()
    fresh = retry if isinstance(retry, FreshRetryPolicy) else None

    async def handle_human(
        h: Any,
        st: S
    ) -> str:
        return await handle.human_interaction(h, lambda: None)

    async def _attempt(inp: I, tid_: str, desc: str) -> S:
        conf = run_conf.copy()
        merged: dict[str, Any] = {**configurable, "thread_id": tid_}
        # ``configurable`` may carry the caller's original resume point; the
        # tracked checkpoint (None after a fresh restart) is authoritative.
        merged.pop("checkpoint_id", None)
        if last_checkpoint is not None:
            merged["checkpoint_id"] = last_checkpoint
        conf["configurable"] = merged
        tok = _current_sink.set((tracking_sink, tid_))
        try:
            return await _run_graph(
                event_sink=tracking_sink,
                graph=graph,
                ctxt=ctxt,
                input=inp,
                run_conf=conf,
                description=desc,
                human_handler=handle_human,
                within_tool=within_tool,
            )
        finally:
            _current_sink.reset(tok)

    async def _with_floor(inp: I, tid_: str, desc_base: str) -> S:
        attempts = floor.max_retries if floor is not None else 1
        for i in range(attempts):
            desc = desc_base if i == 0 else f"{desc_base} (Retry {i})"
            try:
                return await _attempt(inp, tid_, desc)
            except Exception as e:
                # Non-retryable, or that was the last attempt: propagate as-is
                # (no parting backoff — there is no next attempt to wait for).
                if floor is None or not floor.should_retry(e) or i + 1 >= attempts:
                    raise e
                if last_checkpoint is None:
                    raise ValueError("Got retryable error but no checkpoint to resume from") from e
                await floor.try_backoff(i)
        assert False  # unreachable: the last iteration either returns or raises

    if fresh is None:
        return await _with_floor(input, tid, description)

    curr_input = input
    curr_tid = tid
    for j in range(fresh.max_fresh_retries):
        if j > 0:
            last_checkpoint = None
        desc = description if j == 0 else f"{description} (Attempt {j})"
        try:
            return await _with_floor(curr_input, curr_tid, desc)
        except Exception as e:
            # Not fresh-retryable, or that was the last fresh attempt:
            # propagate as-is (no pointless rebuild of an input that will
            # never run).
            if not fresh.should_retry_fresh(e) or j + 1 >= fresh.max_fresh_retries:
                raise e
            if last_checkpoint is None:
                raise ValueError("Have retryable error but no checkpoint from which to recover") from e
            last_state = await graph.aget_state({"configurable": {
                "thread_id": curr_tid,
                "checkpoint_id": last_checkpoint
            }})
            if not last_state.values:
                raise ValueError(f"No state at the last checkpoint {last_checkpoint} in {curr_tid}") from e
            (curr_input, curr_tid) = await fresh.rebuild_input(
                last_state=cast(S, last_state.values),
                last_input=curr_input
            )
    assert False  # unreachable: the last iteration either returns or raises


async def run_to_completion[I: StateLike, S: StateLike, C: StateLike | None](
    graph: CompiledStateGraph[S, C, I, Any],
    input: I,
    thread_id: str,
    context: C = None,
    *,
    checkpoint_id: str | None = None,
    recursion_limit: int,
    description: str,
    within_tool: str | None = None,
    retry: "RetryPolicy | FreshRetryPolicy[S, I] | None" = None,
) -> S:
    """Run a compiled state graph to completion.

    Delegates to :func:`run_graph`, which handles event nesting automatically
    via context vars and applies the ambient (or ``retry``-overridden) retry
    policy. Requires ``with_handler()`` to be active.

    ``within_tool`` is the calling tool's ``tool_call_id`` when this graph is
    being run as a sub-agent from inside a tool. It anchors the sub-graph's
    UI panel under the tool-call widget so the renderer can mount nested
    output in the right place. Pass ``self.tool_call_id`` from a tool that
    mixes in ``WithInjectedId``; leave ``None`` for top-level / pipeline-
    phase invocations.
    """
    run_conf: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    if checkpoint_id is not None:
        run_conf["configurable"]["checkpoint_id"] = checkpoint_id

    return await run_graph(
        graph=graph,
        ctxt=context,
        input=input,
        run_conf=run_conf,
        description=description,
        within_tool=within_tool,
        retry=retry,
    )

class RetryPolicy(ABC):
    """The transient-failure ("floor") retry contract: which exceptions are
    worth re-running from the last checkpoint, and how long to back off
    between attempts. Installed run-wide via :func:`install_retry_policy`,
    or passed per-run through ``run_graph`` / ``run_to_completion`` to
    override the ambient floor."""

    max_retries: int

    @abstractmethod
    def should_retry(self, exc: Exception) -> bool: ...

    @abstractmethod
    async def try_backoff(self, try_count: int): ...


class FreshRetryPolicy[S: StateLike, I: StateLike](ABC):
    """Fresh-start escalation, independent of the floor policy: for failures
    a checkpoint-resume can't fix (the thread is wedged, not the request),
    rebuild the input from the crashed attempt's last checkpointed state and
    start over on the fresh thread id ``rebuild_input`` returns.

    Deliberately NOT a :class:`RetryPolicy`: specifying an escalation does not
    require restating a floor — a plain ``FreshRetryPolicy`` rides whatever
    floor is ambiently installed. A type inheriting BOTH overrides the floor
    as well."""

    max_fresh_retries: int

    @abstractmethod
    def should_retry_fresh(self, exc: Exception) -> bool: ...

    @abstractmethod
    async def rebuild_input(
        self, last_state: S, last_input: I
    ) -> tuple[I, str]: ...


type RetryPredicate = Callable[[Exception], bool]

type Backoff = Callable[[int], Awaitable[None]]

async def exponential_backoff(
    i: int
):
    await asyncio.sleep((2 * 2 ** i) * 60) # aggressive backoff in *minutes*

DEFAULT_MAX_RETRIES = 3

class DefaultRetryPolicy(RetryPolicy):
    def __init__(
        self,
        should_retry: RetryPredicate,
        backoff: Backoff = exponential_backoff,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self._should_retry = should_retry
        self.backoff_policy = backoff
        self.max_retries = max_retries

    @override
    def should_retry(self, exc: Exception) -> bool:
        return self._should_retry(exc)

    @override
    async def try_backoff(self, try_count: int):
        await self.backoff_policy(try_count)


_run_retry_policy: ContextVar[RetryPolicy | None] = ContextVar("_run_retry_policy", default=None)
"""The run-wide retry floor. Read by every ``run_graph`` (nested sub-agent
runs included) when no per-run policy overrides it; ``None`` means failures
propagate on the first attempt, as before."""


def install_retry_policy(policy: RetryPolicy | None) -> Token[RetryPolicy | None]:
    """Install the run-wide retry floor.

    The pipeline harness calls this once per run (mirroring
    ``install_run_summary``) before any graph work spawns; contextvar
    inheritance carries it into every task of the run. Returns the token so
    scoped callers (tests) can reset."""
    return _run_retry_policy.set(policy)

