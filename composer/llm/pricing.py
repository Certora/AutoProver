from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class PriceTier:
    """Per-million-token prices in USD for one model at one
    context tier.

    ``input`` is the price for *fresh* input tokens (the bucket left
    after subtracting ``cache_read`` and ``cache_write`` from the
    total). ``output`` is the price for output tokens, which on the
    OpenAI side already includes reasoning tokens (billed at the
    output rate by both providers). ``cache_read`` is the
    cache-hit rate.

    Cache writes come in two rates: ``cache_write`` is the 5-minute
    ephemeral rate and ``cache_write_1h`` is the 1-hour ephemeral rate
    (~2× base input on Anthropic). Which one applies depends on the
    conversation's cache TTL. OpenAI has no separate cache-write rate,
    so both fields carry the same value there."""
    input: float
    output: float
    cache_read: float
    cache_write: float
    cache_write_1h: float


@dataclass(frozen=True)
class ModelPricing:
    """Pricing entry for one model family. ``long`` is the
    long-context tier (used when input token count exceeds the
    threshold) and applies only to OpenAI models that publish a
    separate >272K-input rate; Anthropic models keep ``long = None``
    and bill everything at ``short`` rates."""
    short: PriceTier
    long: PriceTier | None = None


# OpenAI's published >272K input-token threshold for long-context
# pricing. Once an individual call's input crosses this, the long
# tier applies *for the full session* per OpenAI's terms; we
# approximate that by switching on a per-message basis (a session
# that drifts above 272K will mostly stay there).
_OPENAI_LONG_CONTEXT_THRESHOLD = 272_000


# Pricing tables transcribed from Anthropic + OpenAI rate cards.
# Sources should be re-checked when new model families ship.
_PRICING: list[tuple[str, ModelPricing]] = [
    # ---- Anthropic ----
    # claude-opus-4.5 / 4.6 / 4.7 share a rate card; older 4 / 4.1
    # are pricier. Matching by prefix-of-prefix so "claude-opus-4-7"
    # and "claude-opus-4-7-20260301" both hit the right entry.
    ("claude-opus-4-7", ModelPricing(short=PriceTier(5.00, 25.00, 0.50, 6.25, 10.00))),
    ("claude-opus-4-6", ModelPricing(short=PriceTier(5.00, 25.00, 0.50, 6.25, 10.00))),
    ("claude-opus-4-5", ModelPricing(short=PriceTier(5.00, 25.00, 0.50, 6.25, 10.00))),
    ("claude-opus-4-1", ModelPricing(short=PriceTier(15.00, 75.00, 1.50, 18.75, 30.00))),
    ("claude-opus-4",   ModelPricing(short=PriceTier(15.00, 75.00, 1.50, 18.75, 30.00))),

    ("claude-sonnet-4-6", ModelPricing(short=PriceTier(3.00, 15.00, 0.30, 3.75, 6.00))),
    ("claude-sonnet-4-5", ModelPricing(short=PriceTier(3.00, 15.00, 0.30, 3.75, 6.00))),
    ("claude-sonnet-4",   ModelPricing(short=PriceTier(3.00, 15.00, 0.30, 3.75, 6.00))),

    ("claude-haiku-4-5", ModelPricing(short=PriceTier(1.00, 5.00, 0.10, 1.25, 2.00))),

    # ---- OpenAI ----
    # gpt-5.5 / 5.4 publish short (≤272K input) and long (>272K) tiers.
    # Pro variants don't publish a cached-in discount (cache_read =
    # base input). Mini/nano don't publish a long tier; we use short
    # for everything on those. OpenAI has no separate cache-write rate,
    # so cache_write_1h mirrors cache_write on every OpenAI entry.
    ("gpt-5.5-pro", ModelPricing(
        short=PriceTier(30.00, 180.00, 30.00, 30.00, 30.00),
        long=PriceTier(60.00, 270.00, 60.00, 60.00, 60.00),
    )),
    ("gpt-5.5", ModelPricing(
        short=PriceTier(5.00, 30.00, 0.50, 5.00, 5.00),
        long=PriceTier(10.00, 45.00, 1.00, 10.00, 10.00),
    )),
    ("gpt-5.4-pro", ModelPricing(
        short=PriceTier(30.00, 180.00, 30.00, 30.00, 30.00),
        long=PriceTier(60.00, 270.00, 60.00, 60.00, 60.00),
    )),
    ("gpt-5.4-mini", ModelPricing(short=PriceTier(0.75, 4.50, 0.075, 0.75, 0.75))),
    ("gpt-5.4-nano", ModelPricing(short=PriceTier(0.20, 1.25, 0.02, 0.20, 0.20))),
    ("gpt-5.4", ModelPricing(
        short=PriceTier(2.50, 15.00, 0.25, 2.50, 2.50),
        long=PriceTier(5.00, 22.50, 0.50, 5.00, 5.00),
    )),
]


def price_per_mtok(model: str | None, input_tokens: int) -> PriceTier | None:
    """Look up per-MTok pricing by model name and call size. Returns
    ``None`` for models with no table entry (cost contribution becomes
    zero — better than guessing).

    Matched by prefix on the lowercased model name so dated revisions
    (``claude-opus-4-7-20260301``, ``gpt-5.5-2026-...``) collapse into
    the same family entry. Table is searched in order, so list more
    specific prefixes (``gpt-5.5-pro``) before less specific
    (``gpt-5.5``). For OpenAI models with a long tier, ``input_tokens``
    chooses short vs. long; Anthropic always uses the short tier."""
    if model is None:
        return None
    m = model.lower()
    for prefix, pricing in _PRICING:
        if m.startswith(prefix):
            if pricing.long is not None and input_tokens > _OPENAI_LONG_CONTEXT_THRESHOLD:
                return pricing.long
            return pricing.short
    return None


# A model's pricing curve as a function of a single call's input-token count:
# ``price_per_mtok`` with the model name curried away. The remaining argument
# picks the short/long context tier (OpenAI); Anthropic ignores it.
type PriceProvider = Callable[[int], PriceTier | None]


def price_provider_for(model: str | None) -> PriceProvider:
    """Curry :func:`price_per_mtok` on ``model``: returns a callable mapping a
    call's input-token count to its :class:`PriceTier` (or ``None`` when the
    model has no table entry, so its cost contribution is zero)."""
    def provider(input_tokens: int) -> PriceTier | None:
        return price_per_mtok(model, input_tokens)
    return provider
