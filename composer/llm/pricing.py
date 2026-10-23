from dataclasses import dataclass
from typing import Callable
import re

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
    conversation's cache TTL. OpenAI has a single cache-write rate
    (1.25× input from gpt-5.6 on, none before it), so both fields
    carry the same value there."""
    input: float
    output: float
    cache_read: float
    cache_write: float
    cache_write_1h: float


# OpenAI's published threshold for long-context pricing: a call whose
# input exceeds 272K tokens bills at the long tier.
_OPENAI_LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True)
class ModelPricing:
    """Pricing entry for one model family. ``long`` is the
    long-context tier, used when a call's input token count exceeds
    ``long_threshold``. Models billed at one rate across their whole
    window keep ``long = None``."""
    short: PriceTier
    long: PriceTier | None = None
    long_threshold: int = _OPENAI_LONG_CONTEXT_THRESHOLD


# Pricing tables transcribed from the Anthropic and OpenAI rate cards
# (platform.claude.com/docs/en/about-claude/pricing,
# developers.openai.com/api/docs/pricing), last checked 2026-10-19.
# Re-check when new model families ship or their costs update.
#
# The table keeps the two latest models of each family (Opus, Sonnet,
# Sol, Luna, ...) plus any model the code names as a default. A model
# not listed prices at $0, and a budgeted run on it is refused.
#
_PRICING: list[tuple[str, ModelPricing]] = [
    # ---- Anthropic ----
    # Fable 5.1 reads its cache at 0.025× input; Opus 5.5 and Sonnet 5.5
    # at 0.05×; everything else at the standard 0.1×.
    ("claude-fable-5-1", ModelPricing(short=PriceTier(10.00, 50.00, 0.25, 12.50, 20.00))),
    ("claude-fable-5",   ModelPricing(short=PriceTier(10.00, 50.00, 1.00, 12.50, 20.00))),

    ("claude-opus-5-5", ModelPricing(short=PriceTier(4.00, 20.00, 0.20, 5.00, 8.00))),
    ("claude-opus-5",   ModelPricing(short=PriceTier(5.00, 25.00, 0.50, 6.25, 10.00))),

    ("claude-sonnet-5-5", ModelPricing(short=PriceTier(2.00, 10.00, 0.10, 2.50, 4.00))),
    ("claude-sonnet-5",   ModelPricing(short=PriceTier(2.00, 10.00, 0.20, 2.50, 4.00))),

    # Haiku 5.5 is the one Claude model priced by prompt length: over
    # 100K input tokens (cache reads and writes included) a call pays 5×.
    ("claude-haiku-5-5", ModelPricing(
        short=PriceTier(0.10, 0.50, 0.01, 0.125, 0.20),
        long=PriceTier(0.50, 2.50, 0.05, 0.625, 1.00),
        long_threshold=100_000,
    )),
    ("claude-haiku-4-5", ModelPricing(short=PriceTier(1.00, 5.00, 0.10, 1.25, 2.00))),

    # ---- OpenAI ----
    # Short (≤272K input) and long (>272K) tiers where the rate card
    # publishes both; the rest bill everything at the short rate.
    # From gpt-5.6 on, cache writes cost 1.25× input. Before it there
    # is no cache-write charge, so cache_write is the plain input rate.
    # Pro variants don't publish a cached-input discount (cache_read =
    # input).
    ("gpt-6.1-sol", ModelPricing(
        short=PriceTier(2.00, 10.00, 0.10, 2.50, 2.50),
        long=PriceTier(4.00, 15.00, 0.20, 5.00, 5.00),
    )),
    ("gpt-6-astra", ModelPricing(
        short=PriceTier(10.00, 50.00, 1.00, 12.50, 12.50),
        long=PriceTier(20.00, 75.00, 2.00, 25.00, 25.00),
    )),
    ("gpt-6-sol", ModelPricing(
        short=PriceTier(2.00, 10.00, 0.20, 2.50, 2.50),
        long=PriceTier(4.00, 15.00, 0.40, 5.00, 5.00),
    )),
    ("gpt-6-luna", ModelPricing(
        short=PriceTier(0.10, 0.50, 0.01, 0.125, 0.125),
        long=PriceTier(0.20, 0.75, 0.02, 0.25, 0.25),
    )),
    ("gpt-5.6-terra", ModelPricing(
        short=PriceTier(2.00, 12.00, 0.20, 2.50, 2.50),
        long=PriceTier(4.00, 18.00, 0.40, 5.00, 5.00),
    )),
    ("gpt-5.6-luna", ModelPricing(
        short=PriceTier(0.20, 1.20, 0.02, 0.25, 0.25),
        long=PriceTier(0.40, 1.80, 0.04, 0.50, 0.50),
    )),
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


# A dated snapshot suffix: ``-20251001`` (Anthropic) or ``-2026-04-01`` (OpenAI).
_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})")


def _matches(model: str, name: str) -> bool:
    """Whether ``model`` is the entry ``name`` itself or a dated snapshot of
    it (``claude-haiku-4-5-20251001``). Any other suffix is a different model
    and does not match: ``claude-opus-5-5`` is not ``claude-opus-5``."""
    if not model.startswith(name):
        return False
    rest = model[len(name):]
    return rest == "" or _DATE_SUFFIX.fullmatch(rest) is not None


def price_per_mtok(model: str | None, input_tokens: int) -> PriceTier | None:
    """Look up per-MTok pricing by model name and call size. Returns
    ``None`` for models with no table entry (cost contribution becomes
    zero — better than guessing).

    Matched on the lowercased model name so dated snapshots
    (``claude-haiku-4-5-20251001``, ``gpt-5.5-2026-...``) collapse into
    their model's entry. For models with a long tier,
    ``input_tokens`` chooses short vs. long."""
    if model is None:
        return None
    m = model.lower()
    for name, pricing in _PRICING:
        if _matches(m, name):
            if pricing.long is not None and input_tokens > pricing.long_threshold:
                return pricing.long
            return pricing.short
    return None


# A model's pricing curve as a function of a single call's input-token count:
# ``price_per_mtok`` with the model name curried away. The remaining argument
# picks the short/long context tier for models that have one.
type PriceProvider = Callable[[int], PriceTier | None]


def price_provider_for(model: str | None) -> PriceProvider:
    """Curry :func:`price_per_mtok` on ``model``: returns a callable mapping a
    call's input-token count to its :class:`PriceTier` (or ``None`` when the
    model has no table entry, so its cost contribution is zero)."""
    def provider(input_tokens: int) -> PriceTier | None:
        return price_per_mtok(model, input_tokens)
    return provider
