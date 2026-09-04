"""
Low-level graph execution with event emission.

Streams a compiled LangGraph graph and translates each stream item
into an event pushed to the caller-supplied sink.  Handles HITL
interrupts by delegating to a ``human_handler`` callback.

This module knows nothing about queues, handlers, or nesting — it
just writes events to the sink it is given.  The higher-level
``context.run_graph()`` sets up the sink (with nesting support)
and connects it to the ``EventQueue`` / drainer infrastructure.
"""

import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, Callable, Awaitable, cast

from composer.io.events import GraphEvents, NextCheckpoint, CustomUpdate, Start, End, StateUpdate
from composer.io.protocol import InterruptId
from composer.io.thread_logging import log_thread

from langgraph._internal._typing import StateLike
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt

from langchain_core.runnables import RunnableConfig


def _normalize_updates_payload(payload: dict[str, Any]) -> dict:
    """Flatten langgraph's mixed-mode tools-node payload to ``dict[node, dict]``.

    When a ``ToolNode`` batch contains tools whose returns differ in shape
    (some return raw ``ToolMessage`` / ``str`` / dicts; some return
    ``Command(update=...)`` with extra state-channel deltas), langgraph's
    ``_combine_tool_outputs`` emits the node's value as a *list* of separate
    updates instead of a single merged dict — see
    ``langgraph/prebuilt/tool_node.py``. Every downstream ``log_state_update``
    handler in this codebase expects ``dict[node, dict_with_messages]`` and
    silently drops the list shape (either via ``isinstance(update, dict)``
    guards or ``"messages" in update`` membership checks, which return
    ``False`` against a list of dicts/Commands). The result is that any
    ToolMessage produced by a parallel batch with at least one
    ``Command``-returning tool never reaches the renderer — the tool widgets
    stay "pending" until something else forces a redraw.

    Merge here so handlers stay simple. ``messages`` lists are concatenated;
    other state keys retain last-write-wins semantics (matches what
    langgraph's reducers would do for non-message channels, which the
    handlers in this codebase don't introspect for ingest purposes).
    """
    out: dict[str, Any] = {}
    for node_name, value in payload.items():
        if isinstance(value, dict) or not isinstance(value, list):
            out[node_name] = value
            continue
        merged: dict[str, Any] = {}
        for item in value:
            d = item.update if isinstance(item, Command) and isinstance(item.update, dict) else item
            if not isinstance(d, dict):
                continue
            for k, v in d.items():
                if (
                    k == "messages"
                    and isinstance(merged.get(k), list)
                    and isinstance(v, list)
                ):
                    merged[k] = [*merged[k], *v]
                else:
                    merged[k] = v
        out[node_name] = merged
    return out


class SinkProtocol(Protocol):
    """Write-only event sink.  Synchronous — must not block."""
    def __call__(self, event: GraphEvents) -> None:
        ...

type InterruptHandler[S] = Callable[[Sequence[Interrupt], S], Awaitable[Mapping[InterruptId, str]]]
"""Given every interrupt pending on the thread and its current state, return a
person's text reply for each, keyed by ``Interrupt.id``. Interrupts left out stay
pending: the graph raises them again and the handler is asked again. A handler
that cannot answer in this process raises; what it raises is its business, not
the runner's.
"""


async def run_graph[S: StateLike, I: StateLike, C: StateLike | None](
    event_sink: SinkProtocol,
    graph: CompiledStateGraph[S, C, I, Any],
    ctxt: C,
    input: I | None,
    run_conf: RunnableConfig,
    description: str,
    interrupt_handler: InterruptHandler[S] | None = None,
    within_tool: str | None = None,
) -> S:
    """Stream a graph to completion, emitting events to *event_sink*.

    Emits ``Start`` on entry, ``End`` on exit (in ``finally``), and
    ``StateUpdate`` / ``NextCheckpoint`` / ``CustomUpdate`` as the
    graph produces output.

    ``input=None`` resumes the thread at its latest checkpoint: LangGraph
    replays the persisted writes of the tasks that had finished and runs only
    the rest. A ``checkpoint_id`` in the config is different: it forks the
    thread at that checkpoint and re-executes the whole step, finished tasks
    included. Pass an id only to deliberately go back.

    When the graph pauses on interrupts, hands all of them (one per
    interrupting task) to *interrupt_handler* together with the thread's
    state, and resumes with the map it returns.
    """
    config = run_conf.get("configurable", None)
    if config is None or "thread_id" not in config:
        raise ValueError("`configurable` must be set in graph config with thread_id")
    tid : str = config["thread_id"]

    graph_input : I | Command | None = input

    if "checkpoint_id" in config:
        graph_input = None

    curr_config = run_conf.copy()
    curr_config["configurable"] = config.copy()

    curr_checkpoint : str
    mono_start = time.perf_counter()
    event_sink(Start(
        tid,
        description=description,
        tool_id=within_tool,
        started_at_wall=time.time(),
        started_at_mono=mono_start,
    ))
    err_name: str | None = None
    async with log_thread(
        description=description,
        runnable=run_conf,
        within_tool=within_tool
    ) as cp_logger:
        try:
            while True:
                curr_input = graph_input
                graph_input = None
                # Every interrupt pending after this stream, by id: one per
                # interrupting task, so a turn with two human-facing tool
                # calls yields two, each resumable on its own.
                pending: dict[InterruptId, Interrupt] = {}
                async for (ty, payload) in graph.astream(
                    curr_input, config=curr_config, context=ctxt, stream_mode=["checkpoints", "updates", "custom"]
                ):
                    assert isinstance(payload, dict)
                    if ty == "checkpoints":
                        curr_checkpoint = payload["config"]["configurable"]["checkpoint_id"]
                        cp_logger.last_checkpoint(curr_checkpoint)
                        event_sink(
                            NextCheckpoint(tid, curr_checkpoint)
                        )
                    elif ty == "custom":
                        event_sink(
                            CustomUpdate(payload, thread_id=tid, checkpoint_id=curr_checkpoint) # pyright: ignore[reportPossiblyUnboundVariable]
                        )
                    else:
                        assert ty == "updates"
                        if "__interrupt__" in payload:
                            if interrupt_handler is None:
                                raise RuntimeError(f"graph {tid} interrupted but no interrupt handler is installed")
                            if "configurable" in curr_config and "checkpoint_id" in curr_config["configurable"]:
                                del curr_config["configurable"]["checkpoint_id"]
                            # Record the interrupts but keep draining the stream:
                            # the interrupt checkpoint may not be committed when
                            # this update is yielded, and the resume below targets
                            # the thread's latest checkpoint. Breaking out here
                            # races that write — a resume against the stale
                            # checkpoint replays the previous node (duplicated LLM
                            # turns / re-fired interrupts / dropped state updates).
                            for intr in payload["__interrupt__"]:
                                pending[InterruptId(intr.id)] = intr
                            continue
                        event_sink(
                            StateUpdate(
                                _normalize_updates_payload(payload), thread_id=tid
                            )
                        )
                if pending:
                    assert interrupt_handler is not None
                    curr_state = cast(S, (await graph.aget_state({"configurable": {"thread_id": tid}})).values)
                    resume = await interrupt_handler(list(pending.values()), curr_state)
                    graph_input = Command(resume=dict(resume))
                    continue

                result_state = (await graph.aget_state({"configurable": {"thread_id": tid}})).values
                return cast(S, result_state)
        except BaseException as exc:
            err_name = type(exc).__name__
            raise
        finally:
            event_sink(End(
                tid,
                duration_s=time.perf_counter() - mono_start,
                error=err_name,
            ))
