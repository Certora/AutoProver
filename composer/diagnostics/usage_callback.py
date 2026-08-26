"""LangChain callback that accumulates per-call LLM token usage into the active run.

Attached at model construction (:func:`composer.workflow.services.create_llm_base`)
so it fires for *every* ``invoke`` / ``ainvoke`` through the model — including
``.bind_tools()`` derivatives and out-of-graph side-calls (prover counterexample
analysis, interactive refinement) that never reach the graph's ``StateUpdate`` stream
and so are invisible to the TUI token bar.

``run_inline = True`` keeps dispatch on the event-loop thread (see
``langchain_core.callbacks.manager._ahandle_event_for_handler``): the active-task
context var read by :meth:`RunSummary.record_token_usage` stays visible and the shared
counters are mutated single-threaded (no race). Compatible across the pinned
``langchain_core >=1.2,<1.3.3`` range — uses only the long-stable sync
``BaseCallbackHandler.on_llm_end`` surface.
"""

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from graphcore.utils import TokenUsageDict, get_normalized_token_usage, get_token_usage
from composer.diagnostics.timing import get_run_summary


def _usage_of(msg: AIMessage) -> TokenUsageDict:
    """Token usage of a response, whichever transport produced it.

    ``get_token_usage`` reads the raw Anthropic ``response_metadata["usage"]`` dict,
    which only a non-streamed response carries; a streamed response reports usage
    solely through the provider-normalized ``usage_metadata``. Fall back to
    ``get_normalized_token_usage`` over that, translating back to the raw shape:
    normalized input is the total including both cache buckets, where the raw
    count excludes them."""
    usage = get_token_usage(msg)
    if "usage" in msg.response_metadata:
        return usage
    norm = get_normalized_token_usage(msg)
    cache_read = norm["cache_read_tokens"]
    cache_write = norm["cache_write_tokens"]
    return {
        "model_name": usage["model_name"] or norm["model_name"],
        "input_tokens": max(0, norm["total_input_tokens"] - cache_read - cache_write),
        "output_tokens": norm["total_output_tokens"],
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


class UsageCallback(BaseCallbackHandler):
    """Records each LLM response's token usage into the active ``RunSummary``."""

    # Run on the calling event-loop thread instead of a thread-pool executor.
    # In async runs LangChain otherwise offloads sync handlers to an executor
    # (see manager._ahandle_event_for_handler): inline keeps the active-task
    # context var read by record_token_usage visible, and keeps the shared
    # RunSummary counter mutations single-threaded (no cross-thread race).
    run_inline = True

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        try:
            generation = response.generations[0][0]
        except IndexError:
            return
        if not isinstance(generation, ChatGeneration):
            return
        msg = generation.message
        if isinstance(msg, AIMessage):
            # get_run_summary() returns an inert throwaway outside a run, so this is
            # a no-op when no autoprove run is active (e.g. ad-hoc model use).
            get_run_summary().record_token_usage(_usage_of(msg))
