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
from typing import Any, TYPE_CHECKING, override
from dataclasses import dataclass, field
from functools import cache
import base64
import logging
import os

import httpx
import openai
from pydantic import BaseModel, Field, PositiveInt, SecretStr, ValidationError

from composer.input.files import (
    ContentRenderer, Document, FileData, InMemoryBytesFile, InMemoryTextFile,
    TextDocument, UploaderBase
)
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

logger = logging.getLogger(__name__)


BASE_URL = "https://openrouter.ai/api/v1"

_MODELS_URL = f"{BASE_URL}/models"
_API_KEY_ENV = "OPENROUTER_API_KEY"

_PROBE_TIMEOUT_SECONDS = 15

# How long a stream may go quiet before it is treated as dead. langchain's guard
# measures the gap between chunks *it emits* and defaults to 120s. OpenRouter streams
# reasoning as `response.reasoning_text.delta`, where OpenAI sends the summary event
# `response.reasoning_summary_text.delta` — the only reasoning event langchain turns
# into a chunk. So a thinking route's reasoning phase yields no chunks at all (over
# 200s of it on a kimi-k3 burst) and a healthy request looks stalled.
_STREAM_QUIET_SECONDS = 900.0


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


# --- the `GET /api/v1/models` schema ---------------------------------------
#
# https://openrouter.ai/docs/api-reference/list-available-models. Only the fields
# this module reads are modelled; `extra="ignore"` (pydantic's default) drops the
# rest, which is most of the ~690KB payload. Prices arrive as decimal *strings*,
# which pydantic coerces to float on the way in.

class _TopProvider(BaseModel):
    context_length: PositiveInt | None = None
    max_completion_tokens: PositiveInt | None = None


class _PriceCard(BaseModel):
    """Per-token USD prices. A missing bucket is not a zero — it means the route
    publishes no separate rate for it (see :func:`_price_tier`)."""

    prompt: float | None = None
    completion: float | None = None
    input_cache_read: float | None = None
    input_cache_write: float | None = None
    input_cache_write_1h: float | None = None


class _PriceOverride(_PriceCard):
    """A conditional price, restating only the fields it changes. Only prompt-size
    floors are modelled; OpenRouter also publishes time-of-day windows
    (``utc_start``/``utc_end``), which a per-call price curve cannot express, so
    those arrive with ``min_prompt_tokens`` unset and are skipped."""

    min_prompt_tokens: PositiveInt | None = None


class _Pricing(_PriceCard):
    overrides: list[_PriceOverride] = Field(default_factory=list)


class _ModelRecord(BaseModel):
    id: str
    context_length: PositiveInt | None = None
    top_provider: _TopProvider = Field(default_factory=_TopProvider)
    supported_parameters: set[str] = Field(default_factory=set)
    pricing: _Pricing | None = None


class _ModelsEnvelope(BaseModel):
    """Records stay raw here so one unreadable model can't take the roster with it
    — 400+ vendors publish into this feed, and a single odd record blanking the
    catalog would silently downgrade every route to the fallback window."""

    data: list[Any]


@cache
def _catalog() -> dict[str, _ModelRecord]:
    """OpenRouter's model roster, by id: one blocking unauthenticated GET per
    process, no retry, from :meth:`OpenRouterModelProvider.create` at startup. Any
    failure yields an empty catalog and a run on conservative defaults."""
    return _fetch_catalog()


def _fetch_catalog() -> dict[str, _ModelRecord]:
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = client.get(_MODELS_URL)
            response.raise_for_status()
            envelope = _ModelsEnvelope.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as exc:
        logger.warning(
            "Could not fetch OpenRouter model metadata from %s (%s); falling back to "
            "a %d-token context window and no price card.",
            _MODELS_URL, exc, _FALLBACK_CONTEXT_WINDOW,
        )
        return {}

    catalog: dict[str, _ModelRecord] = {}
    unreadable = 0
    for raw in envelope.data:
        try:
            record = _ModelRecord.model_validate(raw)
        except ValidationError:
            unreadable += 1
            continue
        catalog[record.id] = record
    if unreadable:
        logger.warning(
            "Skipped %d OpenRouter roster record(s) that did not match the expected "
            "schema; those routes fall back to defaults.", unreadable,
        )
    return catalog


def _record_for(model_name: str) -> _ModelRecord | None:
    catalog = _catalog()
    if (exact := catalog.get(model_name)) is not None:
        return exact
    # Variant suffixes (`:free`, `:nitro`, `:floor`) select a routing policy, not a
    # different model; most are absent from the roster under their suffixed id.
    base, _, variant = model_name.partition(":")
    return catalog.get(base) if variant else None


def _features_from(record: _ModelRecord | None) -> OpenRouterModelFeatures:
    if record is None:
        # Only reachable when the fetch itself failed — an id absent from a roster
        # that did load is rejected in `create`.
        return OpenRouterModelFeatures(
            context_window=_FALLBACK_CONTEXT_WINDOW,
            max_output_tokens=None,
            reasoning=True,
        )
    return OpenRouterModelFeatures(
        # Two windows are published: the model's own and the serving provider's. The
        # smaller is the one a request actually has to fit in.
        context_window=min(
            (
                w for w in (record.context_length, record.top_provider.context_length)
                if w is not None
            ),
            default=_FALLBACK_CONTEXT_WINDOW,
        ),
        max_output_tokens=record.top_provider.max_completion_tokens,
        reasoning="reasoning" in record.supported_parameters,
    )


# --- pricing ---------------------------------------------------------------

# OpenRouter quotes USD per token; PriceTier is USD per million tokens.
_TOKENS_PER_MILLION = 1_000_000


def _price_tier(card: _PriceCard) -> PriceTier | None:
    """One published price card as a :class:`PriceTier`, or None if the route
    publishes no prompt/completion rate at all (leaving it uncosted).

    An unpublished *cache* bucket is not a guess: on OpenRouter it means the route
    offers no separate rate for those tokens, so they bill at the ordinary prompt
    rate — which is what falls through here. The only real inference is
    ``cache_write_1h``, where a route with no 1-hour rate is assumed to charge its
    5-minute one; no route this backend has seen publishes the second without the
    first."""
    if card.prompt is None or card.completion is None:
        return None
    per_million = _TOKENS_PER_MILLION
    cache_write = card.input_cache_write if card.input_cache_write is not None else card.prompt
    return PriceTier(
        input=card.prompt * per_million,
        output=card.completion * per_million,
        cache_read=(
            card.input_cache_read if card.input_cache_read is not None else card.prompt
        ) * per_million,
        cache_write=cache_write * per_million,
        cache_write_1h=(
            card.input_cache_write_1h
            if card.input_cache_write_1h is not None
            else cache_write
        ) * per_million,
    )


def _bare_model_name(model_name: str) -> str:
    """The vendor's own name for a route, which is what the static price table in
    ``composer.llm.pricing`` is keyed by: ``openai/gpt-5.5:floor`` -> ``gpt-5.5``."""
    _, _, rest = model_name.partition("/")
    return rest.partition(":")[0]


def _price_provider_from(
    record: _ModelRecord | None, model_name: str
) -> PriceProvider:
    """The route's pricing curve, live from the catalog where possible, else the
    static table on the vendor's bare model name — which covers ``openai/*`` and
    ``anthropic/*``, and yields None (an uncosted run, not a wrong one) elsewhere."""
    pricing = record.pricing if record is not None else None
    if pricing is None or (short := _price_tier(pricing)) is None:
        return price_provider_for(_bare_model_name(model_name))

    # Long-context surcharges are keyed by a prompt-size floor and restate only the
    # fields they change (see openai/gpt-5.5's >272K tier), so each one is merged
    # over the base card before becoming a tier. Highest floor first, so the first
    # match wins.
    tiers = [
        (override.min_prompt_tokens, tier)
        for override in pricing.overrides
        if override.min_prompt_tokens is not None
        and (tier := _price_tier(
            pricing.model_copy(update=override.model_dump(exclude_none=True))
        )) is not None
    ]
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
    ``input_file`` and ``image_url`` into ``input_image`` on the way out. The text
    block is inherited from OpenAI's renderer."""

    @override
    def file_block(
        self, file_id: str, *, cache_level: CacheLevel = CacheLevel.NONE
    ) -> dict:
        raise NotImplementedError(
            "OpenRouter has no Files API; binary content is inlined by "
            "InlineFileUploader as an InMemoryBytesFile."
        )

    @override
    def inline_file_block(
        self, basename: str, contents: bytes, mime: str,
        *, cache_level: CacheLevel = CacheLevel.NONE
    ) -> dict:
        url = f"data:{mime};base64,{base64.b64encode(contents).decode('ascii')}"
        if mime.startswith("image/"):
            return {"type": "image_url", "image_url": {"url": url}}
        return {"type": "file", "file": {"filename": basename, "file_data": url}}


@dataclass
class InlineFileUploader(UploaderBase):
    """``FileUploader`` for a provider with no Files API: nothing is uploaded, so
    binary content becomes an :class:`InMemoryBytesFile` and text destined for
    upload simply stays in the prompt.

    A large binary therefore rides along in every request that carries it, and a
    PDF no route reads natively goes through OpenRouter's ``file-parser`` plugin,
    which falls back to ``mistral-ocr`` at $2/1K pages (pin an engine via
    ``plugins`` to avoid that)."""

    renderer: ContentRenderer = field(default_factory=OpenRouterRenderer)

    @override
    async def _upload_bytes(
        self, crc_basename: str, file_data: bytes, mime: str
    ) -> str:
        raise NotImplementedError("OpenRouter has no Files API.")

    @override
    async def _binary_document(self, data: FileData) -> Document:
        return InMemoryBytesFile(
            basename=data.basename,
            contents=data.raw_data,
            mime=data.mime,
            renderer=self.renderer,
        )

    @override
    async def _text_upload_document(self, data: FileData) -> TextDocument:
        # There is no upload to make it smaller than the prompt, so the
        # very-large-text case this exists for collapses into the ordinary one.
        return InMemoryTextFile(
            basename=data.basename,
            string_contents=data.raw_data.decode("utf-8"),
            renderer=self.renderer,
        )


class OpenRouterService(ProviderServiceBase):
    """Provider services for OpenRouter. ``cache_marker`` stays the base no-op:
    OpenRouter caches prompts itself on the Responses API, so an explicit
    ``cache_control`` breakpoint would add nothing."""

    def __init__(self):
        from graphcore.tools.memory import openai_async_memory_tool
        super().__init__(
            # The memory tool differs from the Anthropic one only in its args
            # schema, so it is portable to any route.
            openai_async_memory_tool,
            InlineFileUploader,
        )

    @override
    def should_retry(self, exc: Exception) -> bool:
        """The OpenAI taxonomy, plus the two failures a *streamed* request adds.
        Both surface while the SSE body is being read, when the request has already
        succeeded, so the SDK's own ``max_retries`` never sees them: a dropped
        connection (``httpx.TransportError``) and a content stall (langchain's
        ``StreamChunkTimeoutError``, a ``TimeoutError`` subclass)."""
        if isinstance(exc, (httpx.TransportError, TimeoutError, openai.APIConnectionError)):
            return True
        if isinstance(exc, openai.APIStatusError):
            return exc.status_code in (408, 409, 429) or exc.status_code >= 500
        return False


@cache
def _openrouter_service():
    return OpenRouterService()


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
        from langchain_openai import ChatOpenAI

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

        return ChatOpenAI(
            model=self.model_name,
            base_url=BASE_URL,
            api_key=SecretStr(self.api_key),
            # Only surface on which langchain echoes a prior turn's reasoning back,
            # so a tool loop doesn't re-derive it every round.
            use_responses_api=True,
            # Unstreamed, OpenRouter holds the whole generation and an upstream
            # pause trips its gateway idle timeout, failing the request.
            streaming=True,
            stream_chunk_timeout=_STREAM_QUIET_SECONDS,
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
