"""LangChain callback that accumulates per-call LLM token usage into the active run.

Attached at model construction (:func:`composer.workflow.services.create_llm_base`)
so it fires for *every* ``invoke`` / ``ainvoke`` through the model — including
``.bind_tools()`` derivatives and out-of-graph side-calls (prover counterexample
analysis, interactive refinement) that never reach the graph's ``StateUpdate`` stream
and so are invisible to the TUI token bar.

Because it is the one place that sees every billed call, it is also where a call's
**origin** is captured: the LangGraph ``thread_id`` and node it ran under, or nothing
at all for a call made outside any graph. Without that, a run's billed totals cannot
be reconciled against its recorded message history — on one 4h25m run, 92% of the
cache-write spend belonged to calls that appear in neither the executor log nor any
``ap-trail`` thread, and there was no way to say which code path produced them. The
origin travels into :meth:`RunSummary.record_token_usage` and out again through
``job_info.json``'s ``token_usage.by_origin``.

``run_inline = True`` keeps dispatch on the event-loop thread (see
``langchain_core.callbacks.manager._ahandle_event_for_handler``): the active-task
context var read by :meth:`RunSummary.record_token_usage` stays visible and the shared
counters are mutated single-threaded (no race). Compatible across the pinned
``langchain_core >=1.2,<1.3.3`` range — uses only the long-stable sync
``BaseCallbackHandler`` surface.
"""

import logging
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from graphcore.utils import get_normalized_token_usage
from composer.diagnostics.budget import current_cost_center
from composer.diagnostics.timing import CallOrigin, get_current_task_id, get_run_summary

logger = logging.getLogger(__name__)

#: Bound on in-flight starts held while waiting for their matching end. A call that
#: never ends (cancelled, crashed) would otherwise leak its entry; well past any real
#: concurrency, so hitting it means something is wrong, not that the cap is too small.
_MAX_INFLIGHT = 4096


def _origin_from_metadata(metadata: dict[str, Any] | None) -> CallOrigin:
    """Read the LangGraph thread/node off a call's run metadata.

    LangGraph stamps ``thread_id`` (from the RunnableConfig's ``configurable``) and
    ``langgraph_node`` into the metadata of every call it drives. A call with neither
    was not driven by a graph — that is the signal, so absence is recorded as such
    rather than being back-filled from ambient context."""
    if not metadata:
        return CallOrigin()
    thread_id = metadata.get("thread_id")
    return CallOrigin(
        thread_id=str(thread_id) if thread_id is not None else None,
        node=metadata.get("langgraph_node"),
    )


class UsageCallback(BaseCallbackHandler):
    """Records each LLM response's token usage, and its origin, into the active
    ``RunSummary``."""

    # Run on the calling event-loop thread instead of a thread-pool executor.
    # In async runs LangChain otherwise offloads sync handlers to an executor
    # (see manager._ahandle_event_for_handler): inline keeps the active-task
    # context var read by record_token_usage visible, and keeps the shared
    # RunSummary counter mutations single-threaded (no cross-thread race).
    run_inline = True

    def __init__(self) -> None:
        # Metadata is passed to the *start* hooks only, so the origin is stashed
        # here and re-joined at end by LangChain's per-call run_id. Single-threaded
        # by run_inline, so a plain dict is safe.
        self._origins: dict[UUID, CallOrigin] = {}

    # Chat models dispatch on_chat_model_start; completion models on_llm_start.
    # Both carry the same metadata, and only one fires per call.
    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        self._remember(run_id, metadata)

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], *, run_id: UUID,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        self._remember(run_id, metadata)

    def _remember(self, run_id: UUID, metadata: dict[str, Any] | None) -> None:
        if len(self._origins) >= _MAX_INFLIGHT:
            # Starts without ends have accumulated; drop the oldest rather than grow.
            # Losing an origin costs attribution on one call, never a token count.
            self._origins.pop(next(iter(self._origins)), None)
        self._origins[run_id] = _origin_from_metadata(metadata)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        # A failed call is billed by the provider but produces no response here, so
        # there is nothing to record — just release the stashed origin.
        self._origins.pop(run_id, None)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID | None = None, **kwargs: Any) -> None:
        origin = self._origins.pop(run_id, CallOrigin()) if run_id is not None else CallOrigin()
        try:
            generation = response.generations[0][0]
        except IndexError:
            return
        if not isinstance(generation, ChatGeneration):
            return
        msg = generation.message
        if not isinstance(msg, AIMessage):
            return
        # The normalized usage_metadata is the one source every transport and
        # provider fills in — the raw response_metadata["usage"] dict exists only
        # on non-streamed Anthropic responses. get_run_summary() returns an inert
        # throwaway outside a run, so this is a no-op when no autoprove run is
        # active (e.g. ad-hoc model use).
        usage = get_normalized_token_usage(msg)
        get_run_summary().record_token_usage(usage, origin=origin)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "LLM call attributed: model=%s task=%s cost_center=%s thread=%s node=%s "
                "in=%d out=%d cache_read=%d cache_write=%d",
                usage.get("model_name"), get_current_task_id(), current_cost_center(),
                origin.thread_id or "-", origin.node or "-",
                usage["total_input_tokens"], usage["total_output_tokens"],
                usage["cache_read_tokens"], usage["cache_write_tokens"],
            )
