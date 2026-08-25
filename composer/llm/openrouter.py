"""OpenRouter LLM backend: metadata probing, an inlining "uploader", and the
``ModelProvider`` that mints ``ChatOpenAI`` instances pointed at OpenRouter's
OpenAI-compatible API. Its own backend rather than a flag on ``openai.py`` because
little carries over: vendor-qualified ids, no Files API, a different request shape.

Three things differ from the OpenAI backend:

* **Probing is live, not parsed.** OpenRouter fronts 400+ models whose names encode
  nothing and whose roster turns over weekly, so ``GET /api/v1/models`` supplies the
  window, output cap, reasoning support and price card instead of a name parser and
  a hand-maintained table. See :func:`_catalog`.
* **Files are inlined.** There is no Files API — see :class:`InlineFileUploader`.
* **Every route goes through the Responses API**, not Chat Completions — see
  ``builder_for``.

Requires ``OPENROUTER_API_KEY``.
"""
from typing import Any, TYPE_CHECKING, Mapping, AsyncIterator, override
from dataclasses import dataclass, field
from functools import cache
import asyncio
import base64
import json
import logging
import os
import urllib.request

import httpx
import openai

from composer.input.files import UploaderBase, ContentRenderer
from composer.input.types import ModelConfiguration
from .provider import (
    ProviderServiceBase, ProviderSpec, compaction_threshold, reasoning_effort,
    standard_callbacks
)
from .openai import OpenAIRenderer
from .pricing import PriceProvider, PriceTier, price_provider_for
from .types import CacheLevel

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGenerationChunk
    from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


BASE_URL = "https://openrouter.ai/api/v1"

_MODELS_URL = f"{BASE_URL}/models"
_API_KEY_ENV = "OPENROUTER_API_KEY"

_PROBE_TIMEOUT_SECONDS = 15

# The only fields read off a roster record.
_PROBED_KEYS = ("context_length", "top_provider", "supported_parameters", "pricing")

# Transient stream failures and how many times to re-issue the request. The SDK's
# own `max_retries` covers *establishing* a request; these surface while the SSE
# body is being consumed, by which point the request has already succeeded, so
# nothing below us retries them and they take the whole run down. `TimeoutError`
# covers langchain's `StreamChunkTimeoutError` (a subclass) for a content stall;
# `httpx.TransportError` covers the dropped-connection family.
_RETRYABLE_STREAM_ERRORS = (httpx.TransportError, TimeoutError, openai.APIConnectionError)
_STREAM_ATTEMPTS = 3
_STREAM_RETRY_BACKOFF_SECONDS = 2.0


def matches(model: str) -> bool:
    """OpenRouter ids are vendor-qualified (``moonshotai/kimi-k2.5``,
    ``openrouter/auto``), and the slash is what separates them from a native name. No
    overlap with the other predicates: those split on "-", so
    ``anthropic/claude-sonnet-5`` heads at ``anthropic/claude``, never ``claude``."""
    return "/" in model


# --- live model metadata ---------------------------------------------------

# What to assume when the roster fetch failed. The window understates
# nearly every current model, which costs earlier compaction rather than a failed
# request; overstating it would hard-fail mid-run on a context-length error.
_FALLBACK_CONTEXT_WINDOW = 128_000


@dataclass(frozen=True)
class OpenRouterModelFeatures:
    """What the request shape needs to know about one route."""

    context_window: int
    # Output-token ceiling the route advertises, or None if it publishes none.
    max_output_tokens: int | None
    # Route accepts the ``reasoning`` knob at all. Assumed True when the fetch
    # failed: OpenRouter drops a parameter the route doesn't support, so guessing
    # "reasoning" wrongly costs nothing, while guessing "no reasoning" wrongly
    # disables thinking silently.
    reasoning: bool


@cache
def _catalog() -> dict[str, Mapping[str, Any]]:
    """OpenRouter's model roster, by id: one blocking unauthenticated GET, no retry,
    from :meth:`OpenRouterModelProvider.create` at startup. Any failure yields an
    empty catalog and a run on conservative defaults."""
    try:
        with urllib.request.urlopen(_MODELS_URL, timeout=_PROBE_TIMEOUT_SECONDS) as resp:
            payload = json.load(resp)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not fetch OpenRouter model metadata from %s (%s); falling back to "
            "a %d-token context window and no price card.",
            _MODELS_URL, exc, _FALLBACK_CONTEXT_WINDOW,
        )
        return {}
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        logger.warning("Unexpected OpenRouter /models payload shape; ignoring it.")
        return {}
    # Projected to what's actually read: the full roster is ~690KB on the wire and
    # ~2.3MB of retained objects, nearly all of it prose this module never touches.
    return {
        rec["id"]: {k: rec[k] for k in _PROBED_KEYS if k in rec}
        for rec in records
        if isinstance(rec, dict) and isinstance(rec.get("id"), str)
    }


def _record_for(model_name: str) -> Mapping[str, Any] | None:
    catalog = _catalog()
    if (exact := catalog.get(model_name)) is not None:
        return exact
    # Variant suffixes (`:free`, `:nitro`, `:floor`) select a routing policy, not a
    # different model; most are absent from the roster under their suffixed id.
    base, _, variant = model_name.partition(":")
    return catalog.get(base) if variant else None


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _features_from(record: Mapping[str, Any] | None) -> OpenRouterModelFeatures:
    if record is None:
        # Only reachable when the fetch itself failed — an id absent from a roster
        # that did load is rejected in `create`.
        return OpenRouterModelFeatures(
            context_window=_FALLBACK_CONTEXT_WINDOW,
            max_output_tokens=None,
            reasoning=True,
        )
    top = record.get("top_provider")
    top = top if isinstance(top, Mapping) else {}
    supported = record.get("supported_parameters")
    supported = set(supported) if isinstance(supported, list) else set()
    return OpenRouterModelFeatures(
        # Two windows are published: the model's own and the serving provider's. The
        # smaller is the one a request actually has to fit in.
        context_window=min(
            (
                w for w in (
                    _positive_int(record.get("context_length")),
                    _positive_int(top.get("context_length")),
                ) if w is not None
            ),
            default=_FALLBACK_CONTEXT_WINDOW,
        ),
        max_output_tokens=_positive_int(top.get("max_completion_tokens")),
        reasoning="reasoning" in supported,
    )


# --- pricing ---------------------------------------------------------------

# OpenRouter quotes USD per token; PriceTier is USD per million.
_TOKENS_PER_MTOK = 1_000_000


def _per_mtok(raw: Any) -> float | None:
    """One price field, converted to per-MTok. A "0" is a real zero (a free route);
    an absent field is a missing key, which comes back as None."""
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return None
    try:
        return float(raw) * _TOKENS_PER_MTOK
    except ValueError:
        return None


def _price_tier(prices: Mapping[str, Any]) -> PriceTier | None:
    prompt = _per_mtok(prices.get("prompt"))
    completion = _per_mtok(prices.get("completion"))
    if prompt is None or completion is None:
        return None
    # A route that publishes no cache bucket bills those tokens as fresh input, and
    # one with no separate 1h rate charges the 5m one. `or` would not do here: a
    # real 0.0 is a free route.
    if (cache_read := _per_mtok(prices.get("input_cache_read"))) is None:
        cache_read = prompt
    if (cache_write := _per_mtok(prices.get("input_cache_write"))) is None:
        cache_write = prompt
    if (cache_write_1h := _per_mtok(prices.get("input_cache_write_1h"))) is None:
        cache_write_1h = cache_write
    return PriceTier(
        input=prompt,
        output=completion,
        cache_read=cache_read,
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
    )


def _bare_model_name(model_name: str) -> str:
    """The vendor's own name for a route, which is what the static price table in
    ``composer.llm.pricing`` is keyed by: ``openai/gpt-5.5:floor`` -> ``gpt-5.5``."""
    _, _, rest = model_name.partition("/")
    return rest.partition(":")[0]


def _price_provider_from(
    record: Mapping[str, Any] | None, model_name: str
) -> PriceProvider:
    """The route's pricing curve, live from the catalog where possible, else the
    static table on the vendor's bare model name — which covers ``openai/*`` and
    ``anthropic/*``, and yields None (an uncosted run, not a wrong one) elsewhere."""
    prices = (record or {}).get("pricing")
    if not isinstance(prices, Mapping) or (short := _price_tier(prices)) is None:
        return price_provider_for(_bare_model_name(model_name))

    # Long-context surcharges arrive as overrides keyed by a prompt-size floor, each
    # restating only the fields it changes (see openai/gpt-5.5's >272K tier).
    raw_overrides = prices.get("overrides")
    tiers: list[tuple[int, PriceTier]] = []
    if isinstance(raw_overrides, list):
        for override in raw_overrides:
            if not isinstance(override, Mapping):
                continue
            floor = _positive_int(override.get("min_prompt_tokens"))
            tier = _price_tier({**prices, **override})
            if floor is not None and tier is not None:
                tiers.append((floor, tier))
    tiers.sort(key=lambda t: t[0], reverse=True)

    def provider(input_tokens: int) -> PriceTier | None:
        for floor, tier in tiers:
            if input_tokens > floor:
                return tier
        return short

    return provider


# --- request shape ---------------------------------------------------------

def _api_key() -> str:
    if not (key := os.environ.get(_API_KEY_ENV)):
        raise ValueError(
            f"{_API_KEY_ENV} is not set, and an OpenRouter model was requested. "
            f"Get a key at https://openrouter.ai/keys."
        )
    return key


@dataclass
class OpenRouterRenderer(OpenAIRenderer):
    """Content blocks in the Chat-Completions shape, which is what langchain converts
    from: ``_convert_chat_completions_blocks_to_responses`` turns ``file`` into
    ``input_file`` and ``image_url`` into ``input_image`` on the way out. Only the
    file block differs from OpenAI's; the text block is inherited.""" 

    @override
    def file_block(
        self, file_id: str, *, filename: str, cache_level: CacheLevel = CacheLevel.NONE
    ) -> dict:
        # `file_id` is a `data:` URL rather than a remote id: OpenRouter has no
        # Files API, so InlineFileUploader carries the bytes here instead.
        if file_id.startswith("data:image/"):
            return {"type": "image_url", "image_url": {"url": file_id}}
        return {"type": "file", "file": {"filename": filename, "file_data": file_id}}


@dataclass
class InlineFileUploader(UploaderBase):
    """``FileUploader`` impl for a provider with no Files API: the "upload" is a
    ``data:`` URL built in memory, which the renderer inlines into the request. So a
    large binary is re-sent with every request carrying it, and a PDF no route reads
    natively goes through OpenRouter's ``file-parser`` plugin, which falls back to
    ``mistral-ocr`` at $2/1K pages (pin an engine via ``plugins`` to avoid that)."""

    renderer: ContentRenderer = field(default_factory=OpenRouterRenderer)

    async def _upload_bytes(
        self, crc_basename: str, file_data: bytes, mime: str
    ) -> str:
        # No dedup cache: the "id" *is* the content, so there is nothing to reuse.
        # Off-thread because the encode is ~1.5ms/MB of blocked loop, matching how
        # `composer.input.files` already offloads the read.
        encoded = await asyncio.to_thread(base64.b64encode, file_data)
        return f"data:{mime};base64,{encoded.decode('ascii')}"


class OpenRouterService(ProviderServiceBase):
    """Provider services for OpenRouter. ``cache_marker`` stays the base no-op:
    OpenRouter caches prompts itself on the Responses API, so an explicit
    ``cache_control`` breakpoint would add nothing."""

    def __init__(self):
        from graphcore.tools.memory import openai_async_memory_tool
        super().__init__(
            # The OpenAI-flavored memory tool is a plain client-side function tool
            # differing only in its args schema, so it is portable to any route.
            openai_async_memory_tool,
            InlineFileUploader,
        )


@cache
def _openrouter_service():
    return OpenRouterService()


# --- chat model ------------------------------------------------------------

@cache
def _chat_model_cls() -> type["ChatOpenAI"]:
    """``ChatOpenAI`` that re-issues a streamed request when the stream breaks.

    Defined behind a cached factory so ``langchain_openai`` stays a lazy import,
    as it is in the sibling backends."""
    from langchain_openai import ChatOpenAI

    class RetryingChatOpenAI(ChatOpenAI):
        @override
        async def _astream(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator["ChatGenerationChunk"]:
            for attempt in range(1, _STREAM_ATTEMPTS + 1):
                # Buffered, not forwarded as they arrive: a retry must not emit a
                # partial response twice. Callers aggregate through `ainvoke`
                # anyway, so this costs nothing today — an incremental consumer
                # would lose its incrementality.
                chunks: list["ChatGenerationChunk"] = []
                try:
                    async for chunk in super()._astream(*args, **kwargs):
                        chunks.append(chunk)
                except _RETRYABLE_STREAM_ERRORS as exc:
                    if attempt == _STREAM_ATTEMPTS:
                        raise
                    delay = _STREAM_RETRY_BACKOFF_SECONDS * attempt
                    logger.warning(
                        "OpenRouter stream failed after %d chunk(s) (%s: %s); "
                        "re-issuing in %.0fs (attempt %d/%d).",
                        len(chunks), type(exc).__name__, exc, delay,
                        attempt + 1, _STREAM_ATTEMPTS,
                    )
                    await asyncio.sleep(delay)
                    continue
                for chunk in chunks:
                    yield chunk
                return

    return RetryingChatOpenAI


# --- ModelProvider ---------------------------------------------------------

@dataclass
class OpenRouterModelProvider:
    """``ModelProvider`` for OpenRouter. Probes the route's metadata once at
    construction and shapes the request from it. ``cache_level`` picks the
    cache-write rate for costing only — OpenRouter has no cache-TTL knob."""

    model_name: str
    options: ModelConfiguration
    features: OpenRouterModelFeatures
    price_provider: PriceProvider
    api_key: str
    provider: OpenRouterService = field(default_factory=_openrouter_service)

    @staticmethod
    def create(model_name: str, options: ModelConfiguration) -> "OpenRouterModelProvider":
        # Before the probe: a run with no key can't start, so it shouldn't pay for a
        # round-trip first. Reading it here rather than in `builder_for` is what
        # makes that failure a startup one instead of a first-LLM-call one.
        api_key = _api_key()
        record = _record_for(model_name)
        if record is None and _catalog():
            # The roster loaded and this route isn't on it.
            raise ValueError(
                f"{model_name!r} is not an OpenRouter model; see "
                f"https://openrouter.ai/models for the roster."
            )
        return OpenRouterModelProvider(
            model_name=model_name,
            options=options,
            features=_features_from(record),
            price_provider=_price_provider_from(record, model_name),
            api_key=api_key,
        )

    @property
    def max_prompt_tokens(self) -> int:
        return compaction_threshold(self.features.context_window)

    def _output_token_cap(self) -> int:
        """The response budget to ask for, clamped to what the route allows — an
        ``opts.tokens`` above the route's ceiling is a 400, not a truncation."""
        requested = self.options.tokens
        ceiling = self.features.max_output_tokens
        return requested if ceiling is None else min(requested, ceiling)

    def builder_for(
        self, *, cache_level: CacheLevel = CacheLevel.NONE, disable_thinking: bool = False
    ) -> "BaseChatModel":
        from pydantic import SecretStr

        opts = self.options
        kwargs: dict[str, Any] = {}

        if opts.thinking_tokens is not None and not disable_thinking and self.features.reasoning:
            # OpenRouter's unified reasoning knob, which it translates per route:
            # passed straight through to the families whose native knob is also an
            # effort level, converted to a token budget for the rest.
            kwargs["reasoning"] = {
                # OpenRouter converts effort to a token budget for the vendors
                # whose native knob is one.
                "effort": reasoning_effort(opts.thinking_tokens),
                "summary": "auto",
            }
            # Ask for the encrypted chain of thought, which is what langchain echoes
            # back with the next tool result so the model can resume it.
            kwargs["include"] = ["reasoning.encrypted_content"]

        return _chat_model_cls()(
            model=self.model_name,
            base_url=BASE_URL,
            api_key=SecretStr(self.api_key),
            # Load-bearing, not a default: the Responses API is the only surface on
            # which reasoning survives a tool round-trip, because langchain echoes a
            # prior turn's reasoning items back into the next request. Chat
            # Completions drops them, so a long tool loop re-derives its reasoning
            # every round at the output token rate.
            use_responses_api=True,
            # Unstreamed, OpenRouter holds the whole generation and an upstream
            # pause trips its gateway idle timeout, failing the request.
            streaming=True,
            # OpenRouter is stateless: store=True is a 400, not a no-op.
            store=False,
            # langchain renames this to `max_output_tokens` for the Responses API.
            max_completion_tokens=self._output_token_cap(),
            timeout=None,
            max_retries=2,
            # Names the run in OpenRouter's activity dashboard.
            default_headers={"X-Title": "AutoProver"},
            callbacks=standard_callbacks(
                self.price_provider, long_cache=cache_level == CacheLevel.LONG
            ),
            **kwargs,
        )


OPENROUTER_SPEC = ProviderSpec(
    matches=matches,
    build=OpenRouterModelProvider.create
)
