"""LangChain callback that accumulates the running USD cost of an LLM's calls.

Sibling to :class:`composer.diagnostics.usage_callback.UsageCallback`: attached at
model construction so it fires for *every* ``invoke`` / ``ainvoke`` through the
model. Where ``UsageCallback`` records raw token counts, this one prices each
response through a :data:`~composer.llm.pricing.PriceProvider` (the model's pricing
curve, curried on model name) and adds the result to a running total.

Implemented as an :class:`~langchain_core.callbacks.AsyncCallbackHandler` with
``run_inline = True``. The async ``on_llm_end`` is awaited on the event-loop thread
either way, but ``run_inline`` also decides *which* context it runs in: without it,
``ahandle_event`` dispatches the handler through ``asyncio.gather`` — each coroutine
wrapped in a ``Task`` against a ``copy_context()`` snapshot, so any ``ContextVar``
the handler *sets* lands in that throwaway copy and never reaches the caller. With
``run_inline`` the handler is instead awaited directly in the caller's task and
context (``manager.ahandle_event`` line ~437), so its contextvar reads and writes
are visible to the surrounding LLM call. This matters because the accumulator
participates in contextvar state, not just its own counter. On the sync ``invoke``
path LangChain still drives the coroutine to completion."""

from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from graphcore.utils import get_normalized_token_usage
from composer.llm.pricing import PriceProvider
from .budget import accumulate_cost


class CostAccumulator(AsyncCallbackHandler):
    """Prices each LLM response and accumulates the total into :attr:`total_cost`.

    ``price_provider`` maps a call's input-token count to its per-MTok
    :class:`~composer.llm.pricing.PriceTier`. ``long_cache`` selects the 1-hour
    cache-write rate over the 5-minute one; the whole conversation is assumed to
    share a single cache TTL (see ``builder_for``)."""

    # Route through the direct-await dispatch branch so the handler runs in the
    # caller's task/context (not a gather-spawned Task with a copied context):
    # required for the handler's contextvar reads/writes to reach the caller.
    run_inline = True

    def __init__(self, price_provider: PriceProvider, *, long_cache: bool = False) -> None:
        self._price_provider = price_provider
        self._long_cache = long_cache
        self.total_cost: float = 0.0

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        try:
            generation = response.generations[0][0]
        except IndexError:
            return
        if not isinstance(generation, ChatGeneration):
            return
        msg = generation.message
        if isinstance(msg, AIMessage):
            accumulate_cost(self._cost_of(msg))

    def _cost_of(self, msg: AIMessage) -> float:
        """USD cost of a single response, in dollars. Zero for models with no
        pricing-table entry."""
        usage = get_normalized_token_usage(msg)
        tier = self._price_provider(usage["total_input_tokens"])
        if tier is None:
            return 0.0

        cache_read = usage["cache_read_tokens"]
        cache_write = usage["cache_write_tokens"]
        # Fresh input is the total minus the two cache buckets, which the tier
        # prices separately. Clamp against provider rounding wobble.
        fresh_input = max(0, usage["total_input_tokens"] - cache_read - cache_write)
        cache_write_rate = tier.cache_write_1h if self._long_cache else tier.cache_write

        # thinking_tokens are a subset of total_output_tokens and bill at the
        # output rate, so they need no separate term here.
        per_mtok = (
            fresh_input * tier.input
            + cache_read * tier.cache_read
            + cache_write * cache_write_rate
            + usage["total_output_tokens"] * tier.output
        )
        return per_mtok / 1_000_000
