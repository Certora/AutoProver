"""
Context-scoped handler installation and graph execution.

This module is the glue between graph execution and event handling.
It provides two public entry points:

``with_handler(io_handler, event_handler, interrupt_handler)``
    Async context manager that installs the scope's handlers into a
    ``ContextVar``, creates an ``EventQueue``, and runs a
    background ``_queue_drainer`` task.  All ``run_graph()`` calls
    within the scope push events to this queue and answer interrupts
    through the interrupt handler.

``run_graph(graph, ctxt, input, run_conf, description)``
    High-level wrapper that reads the installed handlers from the
    context, constructs an event sink (with automatic nesting
    support), and delegates to ``graph_runner.run_graph()``.  Also
    routes the graph's interrupts to the scope's ``InterruptHandler``.

Nesting is automatic: if ``run_graph()`` is called while another
``run_graph()`` is already active (same ``with_handler`` scope),
the inner call's sink wraps events with ``Nested(event,
parent_id=outer_tid)`` before pushing to the queue.  The drainer
peels these layers to reconstruct the full execution path.
"""

from abc import ABC, abstractmethod
from contextvars import ContextVar, Token
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncio


from composer.io.protocol import IOHandler
from composer.io.stream import EventQueue
from composer.io.event_handler import EventHandler

from typing import Any, Awaitable, Callable, Mapping, Protocol, cast, overload, override

from composer.io.events import (
    AllEvents, InnerEvent, Nested, NextCheckpoint,
    CustomUpdate, StateUpdate, Start, End, GraphEvents, ProgressEvent
)
from composer.diagnostics.jsonl_sink import emit as _emit_jsonl

from langgraph._internal._typing import StateLike
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from langchain_core.runnables import RunnableConfig

from composer.io.graph_runner import SinkProtocol, run_graph as _run_graph
from composer.io.protocol import GraphSuspended, InterruptHandler, InterruptId
from langgraph.types import Interrupt
from collections.abc import Sequence

@dataclass(frozen=True)
class HandlerScope:
    """What one ``with_handler`` scope installs: the queue graph events go to,
    the output handler the drainer feeds, the event handler for custom events,
    and the input handler that answers interrupts."""

    queue: EventQueue
    io: IOHandler
    events: EventHandler
    interrupts: InterruptHandler


_io_handler : ContextVar[HandlerScope | None] = ContextVar("_io_handler", default=None)

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
    h: IOHandler,
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
    h: IOHandler,
    event_handler: EventHandler,
    interrupt_handler: InterruptHandler,
):
    """Install the scope's handlers and run a background drainer for it.

    All ``run_graph()`` calls within the scope push events to the
    same ``EventQueue`` and answer interrupts through ``interrupt_handler``.
    On exit, the drainer is cancelled and the context var is restored.
    """
    ev_queue = EventQueue(
        asyncio.Event(),
        []
    )
    tok = _io_handler.set(HandlerScope(ev_queue, h, event_handler, interrupt_handler))
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
    scope = _io_handler.get()
    if scope is None:
        raise ValueError("No IO handler installed")
    scope.queue.push(ProgressEvent(dict(payload)))


async def run_graph[S: StateLike, C: StateLike | None, I: StateLike](
    graph: CompiledStateGraph[S, C, I, Any],
    ctxt: C,
    input: I | None,
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

    Interrupts are routed to the scope's ``InterruptHandler``, which either
    answers them or raises :class:`GraphSuspended`.

    Retry is first-class: the floor policy (``retry`` when it is a
    :class:`RetryPolicy`, else the ambient :func:`install_retry_policy` one)
    re-runs transient failures from the last checkpoint this run streamed; a
    :class:`FreshRetryPolicy` (``retry`` when it is one) escalates wedged
    failures by rebuilding the input on a fresh thread. Checkpoints are
    tracked by wrapping THIS run's sink — children's checkpoints arrive
    ``Nested``-wrapped and don't register — so retry works for nested
    (sub-agent) runs, each tracking only its own thread.
    """
    scope = _io_handler.get()
    if scope is None:
        raise ValueError("No IO handler installed")

    # The outermost run owns the exhausted-thread registry: created here, in an
    # ancestor context of every task the run spawns, so nested spawns and the
    # parent's own floor retries all share one set.
    if _exhausted_threads.get() is None:
        _exhausted_threads.set(set())

    # Determine thread_id from config
    configurable = run_conf.get("configurable", {})
    tid = configurable.get("thread_id")
    if tid is None:
        raise ValueError("thread_id required in run config")

    # Determine sink: top-level uses queue.push, nested wraps parent's sink
    parent = _current_sink.get()
    if parent is None:
        sink: SinkProtocol = scope.queue.push
    else:
        (parent_sink, parent_tid) = parent
        sink = lambda event: parent_sink(Nested(event, parent_id=parent_tid))

    # The most recent checkpoint THIS run committed (seeded from an explicit
    # fork point when the caller passed one): proof that a floor retry has
    # something to resume, and the state a fresh retry rebuilds from. Recorded
    # by wrapping this run's own sink rather than the scope's queue, so
    # concurrent and nested runs never cross-talk.
    caller_checkpoint: str | None = configurable.get("checkpoint_id")
    last_checkpoint: str | None = caller_checkpoint

    def tracking_sink(event: GraphEvents) -> None:
        nonlocal last_checkpoint
        if isinstance(event, NextCheckpoint):
            last_checkpoint = event.checkpoint_id
        sink(event)

    floor = retry if isinstance(retry, RetryPolicy) else _run_retry_policy.get()
    fresh = retry if isinstance(retry, FreshRetryPolicy) else None

    async def handle_interrupts(interrupts: Sequence[Interrupt], st: S) -> Mapping[InterruptId, str]:
        return await scope.interrupts.handle_interrupts(interrupts, st)

    async def _attempt(inp: I | None, tid_: str, desc: str, fork_at: str | None) -> S:
        conf = run_conf.copy()
        merged: dict[str, Any] = {**configurable, "thread_id": tid_}
        merged.pop("checkpoint_id", None)
        if fork_at is not None:
            merged["checkpoint_id"] = fork_at
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
                interrupt_handler=handle_interrupts,
                within_tool=within_tool,
            )
        finally:
            _current_sink.reset(tok)

    async def _with_floor(inp: I | None, tid_: str, desc_base: str, fork_at: str | None) -> S:
        attempts = floor.max_retries if floor is not None else 1
        for i in range(attempts):
            desc = desc_base if i == 0 else f"{desc_base} (Retry {i})"
            try:
                if i == 0:
                    return await _attempt(inp, tid_, desc, fork_at)
                # A retry resumes the thread at its latest checkpoint, by thread
                # alone: naming the checkpoint would fork it, and a fork re-runs
                # the finished tasks of the failed step instead of replaying
                # their persisted writes.
                return await _attempt(None, tid_, desc, None)
            except GraphSuspended:
                # Not a failure: the thread is parked on its interrupts for a
                # later process. Retrying would only ask the same question.
                raise
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
        return await _with_floor(input, tid, description, caller_checkpoint)

    curr_input: I | None = input
    curr_tid = tid
    for j in range(fresh.max_fresh_retries):
        if j > 0:
            last_checkpoint = None
        desc = description if j == 0 else f"{description} (Attempt {j})"
        try:
            return await _with_floor(curr_input, curr_tid, desc, caller_checkpoint if j == 0 else None)
        except GraphSuspended:
            raise
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
            if curr_input is None:
                raise ValueError("cannot rebuild the input of a run that resumed without one") from e
            (curr_input, curr_tid) = await fresh.rebuild_input(
                last_state=cast(S, last_state.values),
                last_input=curr_input
            )
    assert False  # unreachable: the last iteration either returns or raises


@dataclass(frozen=True)
class DurableThread:
    """Ask :func:`run_to_completion` to derive the sub-graph's thread id from the
    spawn's durable identity instead of minting a fresh one.

    The id is ``durable_thread_id(parent_thread, prefix, tool_call_id, generation)``:
    the enclosing run's thread, the spawning tool call (``within_tool``), and a
    generation counter. Because a floor retry of the parent replays the same tool
    call id, a re-entered spawn finds the same thread and RESUMES it from its tip
    rather than starting over. That is what makes a process restart pick up
    sub-agents in flight.

    The generation keeps the state-backoff ladder: a spawn whose run raised is
    recorded as exhausted for the rest of this process, so the next spawn for the
    same tool call takes the next generation, fresh. On a cold start the registry
    is empty, so the walk consults the checkpointer instead: generation ``g+1``
    existing is the durable proof that ``g`` was abandoned.

    Only for spawns whose input is a function of the parent's checkpoint (tool
    arguments, store contents). A spawn fed by non-deterministic work done earlier
    in the same tool call (a prover run's counterexample) must keep a fresh id.
    """

    prefix: str


def durable_thread_id(parent_thread: str, prefix: str, tool_call_id: str, generation: int) -> str:
    return f"{parent_thread}/{prefix}:{tool_call_id}/g{generation}"


_exhausted_threads: ContextVar[set[str] | None] = ContextVar("_exhausted_threads", default=None)
"""Thread ids whose run raised in this process. Created by the outermost
``run_graph`` (and reset by :func:`install_retry_policy`, the run's start), so
every spawn and every floor retry in the run shares it."""


def _exhausted() -> set[str]:
    registry = _exhausted_threads.get()
    if registry is None:
        # No enclosing run: nothing can have been exhausted, and nothing will be.
        return set()
    return registry


async def _checkpoint_tip(
    graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str
) -> str | None:
    """The latest checkpoint id on ``thread_id``, or None when the thread does not
    exist (or the graph has no durable checkpointer)."""
    if not isinstance(graph.checkpointer, BaseCheckpointSaver):
        return None
    # An absent thread comes back as an empty snapshot echoing the request
    # config, which carries no checkpoint id.
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return snapshot.config.get("configurable", {}).get("checkpoint_id")


async def _resolve_durable_thread(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    parent_thread: str,
    prefix: str,
    tool_call_id: str,
) -> tuple[str, bool]:
    """Pick the generation to run and whether it already exists:
    ``(thread_id, resume)``. An existing thread is resumed at its latest
    checkpoint; a missing one starts fresh."""
    exhausted = _exhausted()
    generation = 0
    while True:
        tid = durable_thread_id(parent_thread, prefix, tool_call_id, generation)
        if tid in exhausted:
            generation += 1
            continue
        successor = durable_thread_id(parent_thread, prefix, tool_call_id, generation + 1)
        if await _checkpoint_tip(graph, successor) is not None:
            exhausted.add(tid)
            generation += 1
            continue
        return tid, await _checkpoint_tip(graph, tid) is not None


@overload
async def run_to_completion[I: StateLike, S: StateLike, C: StateLike | None](
    graph: CompiledStateGraph[S, C, I, Any],
    input: I | None,
    thread_id: str,
    context: C = None,
    *,
    checkpoint_id: str | None = None,
    recursion_limit: int,
    description: str,
    within_tool: str | None = None,
    retry: "RetryPolicy | FreshRetryPolicy[S, I] | None" = None,
) -> S: ...


@overload
async def run_to_completion[I: StateLike, S: StateLike, C: StateLike | None](
    graph: CompiledStateGraph[S, C, I, Any],
    input: I,
    thread_id: DurableThread,
    context: C = None,
    *,
    recursion_limit: int,
    description: str,
    within_tool: str,
    retry: "RetryPolicy | FreshRetryPolicy[S, I] | None" = None,
) -> S: ...


async def run_to_completion[I: StateLike, S: StateLike, C: StateLike | None](
    graph: CompiledStateGraph[S, C, I, Any],
    input: I | None,
    thread_id: str | DurableThread,
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

    A thread that already has a checkpoint is RESUMED at it, whatever
    ``input`` says: a ``str`` thread id is the caller's deterministic name for
    the work (a ``WorkflowContext.thread_id``, say), and a restarted process
    naming it again means "pick this back up". ``input`` is only used for a
    fresh start. The one way to start over on an existing thread is
    ``checkpoint_id``, which forks the thread at that checkpoint and
    re-executes the step from scratch: what an operator going back in time
    wants and nothing else does. An id that never exists (a fresh uuid) costs
    one probe of the checkpointer and starts fresh.

    ``within_tool`` is the calling tool's ``tool_call_id`` when this graph is
    being run as a sub-agent from inside a tool. It anchors the sub-graph's
    UI panel under the tool-call widget so the renderer can mount nested
    output in the right place. Pass ``self.tool_call_id`` from a tool that
    mixes in ``WithInjectedId``; leave ``None`` for top-level / pipeline-
    phase invocations.

    ``thread_id`` may instead be a :class:`DurableThread`, in which case the id
    is derived from the enclosing run's thread and ``within_tool``, with the
    generation walk for the retry ladder. The overloads make that variant
    require ``within_tool`` and refuse ``checkpoint_id``; the checks below
    repeat the contract for callers the type checker does not see.
    """
    durable = isinstance(thread_id, DurableThread)
    if isinstance(thread_id, DurableThread):
        if checkpoint_id is not None:
            raise ValueError("a DurableThread finds its own resume point; do not pass checkpoint_id")
        if within_tool is None:
            raise ValueError("a DurableThread is anchored on the spawning tool call; pass within_tool")
        parent = _current_sink.get()
        if parent is None:
            raise ValueError("a DurableThread needs an enclosing run_graph to anchor on")
        tid, resume = await _resolve_durable_thread(graph, parent[1], thread_id.prefix, within_tool)
        if resume:
            input = None
    else:
        tid = thread_id
        if checkpoint_id is None and input is not None and await _checkpoint_tip(graph, tid) is not None:
            input = None

    run_conf: RunnableConfig = {
        "configurable": {"thread_id": tid},
        "recursion_limit": recursion_limit,
    }
    if checkpoint_id is not None:
        run_conf["configurable"]["checkpoint_id"] = checkpoint_id

    try:
        return await run_graph(
            graph=graph,
            ctxt=context,
            input=input,
            run_conf=run_conf,
            description=description,
            within_tool=within_tool,
            retry=retry,
        )
    except GraphSuspended:
        # Parked, not failed: the same generation resumes when the answer comes.
        raise
    except Exception:
        # A spawn that raised is never resumed by this process: the parent's
        # retry re-enters the tool and the next spawn takes the next generation.
        # Cancellation is not an Exception, so a sibling cancelled by another
        # task's failure keeps its generation and re-attaches.
        if durable:
            _exhausted().add(tid)
        raise

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
    scoped callers (tests) can reset.

    Also starts a fresh exhausted-thread registry: a new run is a new process
    as far as the state-backoff ladder is concerned, so durable spawns walk
    their generations from the checkpointer, not from memory."""
    _exhausted_threads.set(set())
    return _run_retry_policy.set(policy)

