#!/usr/bin/env python3
"""
LLM Utility Functions

This module provides utility functions for making LLM API calls,
useful for testing and standalone prompting.
"""

import json
import os
import sys
import time
import logging
import logging.handlers
import threading
import contextlib
import contextvars
from pydantic import BaseModel, ValidationError
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Optional, overload, Literal, Generator, TypeVar, cast

from certora_autosetup.utils.constants import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_MODEL_ENV,
    CUSTOM_ON_CLOUD_API_KEY_ENV,
    CUSTOM_ON_CLOUD_BASE_URL_ENV,
    CUSTOM_ON_CLOUD_MODEL_ENV,
    DEFAULT_LOCAL_LLM_BASE_URL,
    DEFAULT_LOCAL_LLM_MODEL,
    LLM_BACKEND_ENV,
    LLMBackend,
    LOCAL_LLM_BASE_URL_ENV,
    LOCAL_LLM_MODEL_ENV,
)

import anthropic
import anthropic.types
import openai as openai_sdk
from dotenv import load_dotenv

# Auto-load .env so API keys are available without explicit shell exports.
load_dotenv()

# Default Anthropic model identifier. Use `default_anthropic_model()` everywhere
# instead of referencing this constant directly — that helper consults the
# PREAUDIT_ANTHROPIC_MODEL env var first, so operators can override per-run without
# editing code. This value is the fallback when the env var is unset.
_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"


def default_anthropic_model() -> str:
    """Return the Anthropic model identifier the public call_llm_* functions
    use when no explicit `model=` is passed.

    Resolved from PREAUDIT_ANTHROPIC_MODEL env var at call time so test fixtures that
    `patch.dict(os.environ, ...)` take effect and operators can swap models
    on a per-run basis without code changes. Falls back to
    `_DEFAULT_ANTHROPIC_MODEL` (currently Sonnet 4.5) when the env var is
    unset.
    """
    return os.environ.get(ANTHROPIC_MODEL_ENV, _DEFAULT_ANTHROPIC_MODEL)

# Cache for Anthropic clients to avoid recreating them
_client_cache = {}


@overload
def _get_cached_client(api_key: str) -> Optional["anthropic.Anthropic"]: ...


@overload
def _get_cached_client(api_key: str, async_client: Literal[True]) -> Optional["anthropic.AsyncAnthropic"]: ...


def _get_cached_client(
    api_key: str, async_client: Literal[True] | None = None
) -> Optional["anthropic.Anthropic"] | Optional["anthropic.AsyncAnthropic"]:
    """Get or create a cached Anthropic client for the given API key."""
    # Use a hash of the API key as the cache key for security
    import hashlib

    is_async = async_client is not None
    cache_key = hashlib.sha256((api_key + ("async" if is_async else "")).encode()).hexdigest()[:16]

    if cache_key not in _client_cache:
        try:
            if is_async:
                real = anthropic.AsyncAnthropic(api_key=api_key)
            else:
                real = anthropic.Anthropic(api_key=api_key)
            # Wrap so every .messages.create response is recorded into the usage
            # ledger before it reaches any caller — the single funnel.
            _client_cache[cache_key] = _UsageRecordingAnthropic(real, is_async=is_async)
        except Exception:
            return None

    return _client_cache[cache_key]


def clear_client_cache():
    """Clear the Anthropic client cache. Useful for testing or when API keys change."""
    global _client_cache
    _client_cache.clear()


M = TypeVar("M", bound=BaseModel)

# ---------------------------------------------------------------------------
# Local LLM backend support
# ---------------------------------------------------------------------------

_local_client_cache: dict[str, Any] = {}


def _get_backend() -> LLMBackend:
    """Return the active LLM backend based on the PREAUDIT_LLM_BACKEND env var.

    Unknown values fall back to the default (anthropic) — preserves the
    historical behavior so a typo in a long-running env doesn't crash.
    """
    raw = os.environ.get(LLM_BACKEND_ENV, LLMBackend.ANTHROPIC.value).lower()
    try:
        return LLMBackend(raw)
    except ValueError:
        return LLMBackend.ANTHROPIC


def is_local_backend() -> bool:
    """Return True when the local LLM backend is active."""
    return _get_backend() == LLMBackend.LOCAL


def is_mock_backend() -> bool:
    """Return True when the mock LLM backend is active (for CI testing)."""
    return _get_backend() == LLMBackend.MOCK


def is_custom_on_cloud_backend() -> bool:
    """Return True when the custom_on_cloud LLM backend is active."""
    return _get_backend() == LLMBackend.CUSTOM_ON_CLOUD


def is_openai_compatible_backend() -> bool:
    """Return True when the active backend speaks the OpenAI-compatible protocol
    (local self-hosted server, or a custom_on_cloud provider like Together AI)."""
    return _get_backend() in (LLMBackend.LOCAL, LLMBackend.CUSTOM_ON_CLOUD)


def _get_local_model() -> str:
    return os.environ.get(LOCAL_LLM_MODEL_ENV, DEFAULT_LOCAL_LLM_MODEL)


def _get_local_base_url() -> str:
    return os.environ.get(LOCAL_LLM_BASE_URL_ENV, DEFAULT_LOCAL_LLM_BASE_URL)


def _require_env(name: str) -> str:
    """Read a required env var; raise RuntimeError if unset or empty."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"PREAUDIT_LLM_BACKEND=custom_on_cloud requires {name} to be set. "
            "See CLAUDE.md for the full env var list."
        )
    return value


def _get_custom_on_cloud_api_key() -> str:
    return _require_env(CUSTOM_ON_CLOUD_API_KEY_ENV)


def _get_custom_on_cloud_base_url() -> str:
    return _require_env(CUSTOM_ON_CLOUD_BASE_URL_ENV)


def _get_custom_on_cloud_model() -> str:
    return _require_env(CUSTOM_ON_CLOUD_MODEL_ENV)


def _init_openai_compatible_client(base_url: str, api_key: str, *, async_client: bool) -> Any:
    """Return a cached OpenAI-compatible client keyed by (mode, base_url, api_key)."""
    mode = "async" if async_client else "sync"
    cache_key = f"{mode}:{base_url}:{hash(api_key)}"
    if cache_key not in _local_client_cache:
        if async_client:
            real = openai_sdk.AsyncOpenAI(base_url=base_url, api_key=api_key)
        else:
            real = openai_sdk.OpenAI(base_url=base_url, api_key=api_key)
        # Wrap so every .chat.completions.create response is recorded into the
        # usage ledger before it reaches any caller.
        _local_client_cache[cache_key] = _UsageRecordingOpenAI(real, is_async=async_client)
    return _local_client_cache[cache_key]


def _init_local_client() -> Any:
    """Return a cached sync OpenAI client for the local server."""
    return _init_openai_compatible_client(_get_local_base_url(), "not-needed", async_client=False)


def _init_async_local_client() -> Any:
    """Return a cached async OpenAI client for the local server."""
    return _init_openai_compatible_client(_get_local_base_url(), "not-needed", async_client=True)


def _init_custom_on_cloud_client() -> Any:
    """Return a cached sync OpenAI-compatible client for the custom_on_cloud backend."""
    return _init_openai_compatible_client(
        _get_custom_on_cloud_base_url(), _get_custom_on_cloud_api_key(), async_client=False
    )


def _init_async_custom_on_cloud_client() -> Any:
    """Return a cached async OpenAI-compatible client for the custom_on_cloud backend."""
    return _init_openai_compatible_client(
        _get_custom_on_cloud_base_url(), _get_custom_on_cloud_api_key(), async_client=True
    )


def _pick_openai_compatible_client(*, async_client: bool) -> Any:
    """Pick the right OpenAI-compatible client based on the active backend."""
    if is_custom_on_cloud_backend():
        return _init_async_custom_on_cloud_client() if async_client else _init_custom_on_cloud_client()
    return _init_async_local_client() if async_client else _init_local_client()


def _pick_openai_compatible_model() -> str:
    """Pick the right model identifier based on the active backend."""
    if is_custom_on_cloud_backend():
        return _get_custom_on_cloud_model()
    return _get_local_model()


def get_active_model_for_tracking() -> str:
    """Return the model identifier that the active backend will send to the API.

    Public helper for callers (e.g. PreAudit's per-rule analyzer) that need to
    attribute token usage to the correct model in their cost report.
    """
    if is_custom_on_cloud_backend():
        return _get_custom_on_cloud_model()
    if is_local_backend():
        return _get_local_model()
    return default_anthropic_model()  # anthropic default, env-var-overridable


# ---------------------------------------------------------------------------
# Normalized usage callback (works for all backends; carries only the numbers
# PreAudit's token tracker needs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMUsageEvent:
    """Normalized per-call usage numbers, backend-agnostic.

    Cache fields stay 0 for openai-compatible backends since the OpenAI
    chat-completions API doesn't expose prompt caching today.
    """
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


UsageCallback = Callable[[LLMUsageEvent], None]


# ---------------------------------------------------------------------------
# Unified usage ledger
#
# Single source of truth for "how many tokens did this process spend". Every
# LLM response is recorded here at the transport layer (the recording client
# wrappers below), so a caller cannot forget to track usage: there is no way to
# obtain an SDK client except through the two factories that wrap it. The legacy
# ``on_token_usage`` callback path is left intact and independent — it exists for
# external consumers (PreAudit's per-rule attribution) and is NOT the source for
# AutoSetup's own reporting.
# ---------------------------------------------------------------------------


@dataclass
class UsageRow:
    """One normalized record per LLM API round-trip. The cache_* fields are
    Anthropic-only (0 for OpenAI-compatible backends, which expose no caching).
    The token field names match the Anthropic SDK and PreAudit's compute_llm_cost,
    which reads these rows from orchestrator_results.json["llm_usage"]."""
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    component: str = "unknown"
    backend: str = "anthropic"
    ts: float = 0.0
    contract: str | None = None


_ledger_lock = threading.Lock()
_ledger_rows: list[UsageRow] = []
# Optional per-phase attribution tag. Counting NEVER depends on this — only the
# by_component breakdown does. Defaults to "unknown" when no scope is active.
_component: contextvars.ContextVar[str] = contextvars.ContextVar("llm_component", default="unknown")


def ledger_reset() -> None:
    """Clear the process-wide ledger. Call once at process startup (and between
    tests via an autouse fixture)."""
    global _ledger_rows
    with _ledger_lock:
        _ledger_rows = []


def _ledger_append(row: UsageRow) -> None:
    with _ledger_lock:
        _ledger_rows.append(row)


def get_ledger_rows() -> list[UsageRow]:
    """Return a snapshot of every recorded usage row."""
    with _ledger_lock:
        return list(_ledger_rows)


@contextlib.contextmanager
def ledger_component(name: str) -> Generator[None, None, None]:
    """Tag rows recorded within the block with a component label, e.g.
    ``with ledger_component("proxy_detection"):``. Purely for the by_component
    breakdown — tokens are counted regardless of the label."""
    token = _component.set(name)
    try:
        yield
    finally:
        _component.reset(token)


def _anthropic_usage_fields(usage: Any) -> dict:
    """Extract Anthropic token counters from a response.usage block. The keys match
    both UsageRow and LLMUsageEvent — one source of truth for extraction."""
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def _openai_usage_fields(usage: Any) -> dict:
    """Extract OpenAI-compatible token counters (this API exposes no cache counters)."""
    return {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }


def _record_anthropic_response(response: Any, model: str) -> None:
    """Record one Anthropic Message response into the ledger."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    _ledger_append(UsageRow(
        model=model, backend="anthropic", component=_component.get(), ts=time.time(),
        **_anthropic_usage_fields(usage),
    ))


def _record_openai_response(response: Any, model: str) -> None:
    """Record one OpenAI-compatible ChatCompletion response into the ledger."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    _ledger_append(UsageRow(
        model=model, backend="openai_compatible", component=_component.get(), ts=time.time(),
        **_openai_usage_fields(usage),
    ))


# --- transparent recording proxies: the single funnel ----------------------
# Every SDK client is born inside _get_cached_client / _init_openai_compatible_client
# and wrapped here, so every `.create` response is recorded before it returns to
# any caller — wrappers and the raw proxy-detection agent loop alike. Non-create
# attributes (.batches, .beta, .close, count_tokens, ...) pass straight through.


class _RecordingCreateProxy:
    """Wraps a sync ``.messages`` / ``.chat.completions`` object, recording every
    ``.create`` response via ``record_fn(response, model)`` before returning it
    untouched. Other attributes delegate to the real object."""

    def __init__(self, real: Any, record_fn: Callable[[Any, str], None]):
        self._real = real
        self._record_fn = record_fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        result = self._real.create(*args, **kwargs)
        self._record_fn(result, kwargs.get("model", "?"))
        return result


class _AsyncRecordingCreateProxy:
    """Async counterpart of _RecordingCreateProxy: awaits the real coroutine, then
    records. The factory selects sync vs async at wrap time."""

    def __init__(self, real: Any, record_fn: Callable[[Any, str], None]):
        self._real = real
        self._record_fn = record_fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._real.create(*args, **kwargs)
        self._record_fn(result, kwargs.get("model", "?"))
        return result


def _create_proxy(real: Any, record_fn: Callable[[Any, str], None], *, is_async: bool) -> Any:
    cls = _AsyncRecordingCreateProxy if is_async else _RecordingCreateProxy
    return cls(real, record_fn)


class _UsageRecordingAnthropic:
    """Transparent wrapper around anthropic.Anthropic / AsyncAnthropic."""

    def __init__(self, real: Any, *, is_async: bool):
        self._real = real
        self._is_async = is_async

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @property
    def messages(self) -> Any:
        return _create_proxy(self._real.messages, _record_anthropic_response, is_async=self._is_async)


class _RecordingOpenAIChat:
    def __init__(self, real: Any, *, is_async: bool):
        self._real = real
        self._is_async = is_async

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @property
    def completions(self) -> Any:
        return _create_proxy(self._real.completions, _record_openai_response, is_async=self._is_async)


class _UsageRecordingOpenAI:
    """Transparent wrapper around openai.OpenAI / AsyncOpenAI. Intercepts the
    two-level ``client.chat.completions.create`` chain."""

    def __init__(self, real: Any, *, is_async: bool):
        self._real = real
        self._is_async = is_async

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @property
    def chat(self) -> _RecordingOpenAIChat:
        return _RecordingOpenAIChat(self._real.chat, is_async=self._is_async)


def _unwrap_client(client: Any) -> Any:
    """Return the raw SDK client underneath a recording wrapper (or the client
    unchanged if it is not wrapped)."""
    if isinstance(client, (_UsageRecordingAnthropic, _UsageRecordingOpenAI)):
        return client._real
    return client


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def rollup_llm_usage(rows: list[UsageRow]) -> dict:
    """Aggregate usage rows into token totals + by_model / by_component /
    by_contract breakdowns."""
    def bucket() -> dict:
        return {"calls": 0, **{f: 0 for f in _TOKEN_FIELDS}}

    totals = bucket()
    by_model: dict[str, dict] = {}
    by_component: dict[str, dict] = {}
    by_contract: dict[str, dict] = {}

    for r in rows:
        targets = [
            totals,
            by_model.setdefault(r.model, bucket()),
            by_component.setdefault(r.component, bucket()),
        ]
        if r.contract is not None:
            targets.append(by_contract.setdefault(r.contract, bucket()))
        for b in targets:
            b["calls"] += 1
            for f in _TOKEN_FIELDS:
                b[f] += getattr(r, f)

    return {
        "totals": totals,
        "by_model": by_model,
        "by_component": by_component,
        "by_contract": by_contract,
    }


@dataclass
class LlmUsageReport:
    """The token-usage payload written to llm_usage.json and embedded in
    orchestrator_results.json: the recorded rows plus their rollup. Centralizes the
    JSON key names so emitters/readers don't repeat them. ``to_dict`` is the single
    point where UsageRow becomes JSON dicts."""
    llm_usage: list[UsageRow]
    llm_usage_totals: dict

    @classmethod
    def from_rows(cls, rows: list[UsageRow]) -> "LlmUsageReport":
        return cls(llm_usage=rows, llm_usage_totals=rollup_llm_usage(rows))

    @classmethod
    def from_dict(cls, data: dict) -> "LlmUsageReport":
        return cls(
            llm_usage=[UsageRow(**r) for r in data.get("llm_usage", [])],
            llm_usage_totals=data.get("llm_usage_totals", {}),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _anthropic_messages_to_openai(
    system: list[Any] | None,
    messages: list[Any],
) -> list[dict[str, str]]:
    """Convert Anthropic system blocks + messages to OpenAI chat message format.

    Strips cache_control fields and flattens content blocks to plain text.
    """
    openai_messages: list[dict[str, str]] = []

    if system:
        parts = [block["text"] for block in system if isinstance(block, dict) and "text" in block]
        if parts:
            openai_messages.append({"role": "system", "content": "\n\n".join(parts)})

    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            openai_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
                elif isinstance(block, str):
                    text_parts.append(block)
            if text_parts:
                openai_messages.append({"role": role, "content": "\n\n".join(text_parts)})
        else:
            openai_messages.append({"role": role, "content": str(content)})

    return openai_messages


def _local_retry_transient(api_call: Callable[[], Any], max_retries: int, verbose: bool) -> Any:
    """Retry transient errors for OpenAI-compatible local servers."""
    last_exception: Exception | None = None
    for attempt in range(max_retries):
        try:
            return api_call()
        except Exception as e:
            last_exception = e
            if verbose:
                print(f"Local LLM error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise last_exception  # type: ignore[misc]


async def _local_retry_transient_async(api_call: Callable[[], Any], max_retries: int, verbose: bool) -> Any:
    """Async retry for local servers."""
    import asyncio

    last_exception: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await api_call()
        except Exception as e:
            last_exception = e
            if verbose:
                print(f"Local LLM error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)
    raise last_exception  # type: ignore[misc]


def _emit_openai_usage(response: Any, on_usage: Optional[UsageCallback]) -> None:
    """Extract usage from an OpenAI-compatible ChatCompletion and fire the callback.

    Silently skips if the response or its usage block is missing — older
    self-hosted servers (some Ollama versions) don't always include `usage`.
    """
    if on_usage is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    on_usage(LLMUsageEvent(**_openai_usage_fields(usage)))


def _emit_anthropic_usage(response: Any, on_usage: Optional[UsageCallback]) -> None:
    """Extract usage from one Anthropic Message response and fire on_usage exactly once.

    Emits a single `LLMUsageEvent` per API response — no internal accumulation,
    no buffering.
    """
    if on_usage is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    on_usage(LLMUsageEvent(**_anthropic_usage_fields(usage)))


def _local_call_text(
    client: Any,
    model: str,
    openai_messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    max_retries: int,
    verbose: bool,
    on_usage: Optional[UsageCallback] = None,
) -> str:
    """Call local LLM and return plain text."""
    response = _local_retry_transient(
        lambda: client.chat.completions.create(
            model=model, messages=openai_messages, max_tokens=max_tokens, temperature=temperature
        ),
        max_retries,
        verbose,
    )
    _emit_openai_usage(response, on_usage)
    return (response.choices[0].message.content or "").strip()


async def _local_call_text_async(
    client: Any,
    model: str,
    openai_messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    max_retries: int,
    verbose: bool,
    on_usage: Optional[UsageCallback] = None,
) -> str:
    """Async call to local LLM returning plain text."""
    response = await _local_retry_transient_async(
        lambda: client.chat.completions.create(
            model=model, messages=openai_messages, max_tokens=max_tokens, temperature=temperature
        ),
        max_retries,
        verbose,
    )
    _emit_openai_usage(response, on_usage)
    return (response.choices[0].message.content or "").strip()


def _local_call_structured(
    client: Any,
    model: str,
    openai_messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    output_type: type[M],
    max_retries: int,
    verbose: bool,
    on_usage: Optional[UsageCallback] = None,
) -> M:
    """Call local LLM with structured JSON output.

    Tries JSON schema mode first (Ollama 0.5+, vLLM 0.6+, llama.cpp),
    then falls back to prompt-based JSON extraction.
    """
    schema = output_type.model_json_schema()

    # Strategy 1: JSON schema mode
    try:
        response = _local_retry_transient(
            lambda: client.chat.completions.create(
                model=model,
                messages=openai_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "output", "strict": True, "schema": schema},
                },
            ),
            max_retries,
            verbose,
        )
        _emit_openai_usage(response, on_usage)
        return output_type.model_validate_json(response.choices[0].message.content)
    except Exception:
        if verbose:
            print("JSON schema mode not supported, falling back to prompt-based extraction")

    # Strategy 2: prompt-based JSON extraction
    fallback_messages = list(openai_messages)
    fallback_messages.append(
        {
            "role": "user",
            "content": (
                "You MUST respond with ONLY valid JSON matching this schema:\n"
                f"{json.dumps(schema, indent=2)}\n"
                "Do not include any text before or after the JSON."
            ),
        }
    )
    response = _local_retry_transient(
        lambda: client.chat.completions.create(
            model=model, messages=fallback_messages, max_tokens=max_tokens, temperature=temperature
        ),
        max_retries,
        verbose,
    )
    _emit_openai_usage(response, on_usage)
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return output_type.model_validate_json(raw)


async def _local_call_structured_async(
    client: Any,
    model: str,
    openai_messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    output_type: type[M],
    max_retries: int,
    verbose: bool,
    on_usage: Optional[UsageCallback] = None,
) -> M:
    """Async version of _local_call_structured."""
    schema = output_type.model_json_schema()

    try:
        response = await _local_retry_transient_async(
            lambda: client.chat.completions.create(
                model=model,
                messages=openai_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "output", "strict": True, "schema": schema},
                },
            ),
            max_retries,
            verbose,
        )
        _emit_openai_usage(response, on_usage)
        return output_type.model_validate_json(response.choices[0].message.content)
    except Exception:
        if verbose:
            print("JSON schema mode not supported, falling back to prompt-based extraction")

    fallback_messages = list(openai_messages)
    fallback_messages.append(
        {
            "role": "user",
            "content": (
                "You MUST respond with ONLY valid JSON matching this schema:\n"
                f"{json.dumps(schema, indent=2)}\n"
                "Do not include any text before or after the JSON."
            ),
        }
    )
    response = await _local_retry_transient_async(
        lambda: client.chat.completions.create(
            model=model, messages=fallback_messages, max_tokens=max_tokens, temperature=temperature
        ),
        max_retries,
        verbose,
    )
    _emit_openai_usage(response, on_usage)
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return output_type.model_validate_json(raw)


@dataclass
class LLMCall:
    model: str
    max_tokens: int
    temperature: float
    messages: list["anthropic.types.MessageParam"]
    system: list["anthropic.types.TextBlockParam"] | None = None


T = TypeVar("T")


def _setup_file_logger(log_path: Optional[str]) -> logging.Logger:
    """Configure and return a rotating file logger for LLM calls."""
    log_path_final = Path(log_path) if log_path else Path(".certora_internal/llm_util.log")
    log_path_final.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("llm_util")
    logger.setLevel(logging.INFO)

    for h in logger.handlers[:]:
        logger.removeHandler(h)

    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_path_final),
        maxBytes=20 * 1024 * 1024,  # 20MB
        backupCount=1,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _call_llm_pure(
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
    max_retries: int,
    log_to_file: bool,
    log_path: Optional[str],
    verbose: bool,
    system_prompt: Optional[str] = None,
) -> Generator[LLMCall, T, Optional[T]]:
    """Generator that yields LLMCall requests and receives responses.

    When *system_prompt* is provided the prompt is sent with cache_control=ephemeral
    and the user text is wrapped in a content block (used by the cached-prompt path).
    """
    logger: Optional[logging.Logger] = None
    if log_to_file:
        logger = _setup_file_logger(log_path)
        log_entry = f"\n{'='*80}\n"
        log_entry += f"Timestamp: {datetime.now().isoformat()}\n"
        log_entry += f"Model: {model}\n"
        log_entry += f"Max Tokens: {max_tokens}\n"
        log_entry += f"Temperature: {temperature}\n"
        if system_prompt is not None:
            log_entry += f"System Prompt:\n{system_prompt}\n"
            log_entry += f"User Prompt:\n{prompt}\n"
        else:
            log_entry += f"Prompt:\n{prompt}\n"
        logger.info(log_entry)

    if system_prompt is not None:
        system: list[anthropic.types.TextBlockParam] | None = [
            cast(
                "anthropic.types.TextBlockParam",
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            )
        ]
        messages: list[anthropic.types.MessageParam] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    else:
        system = None
        messages = cast(list[anthropic.types.MessageParam], [{"role": "user", "content": prompt}])

    for attempt in range(max_retries):
        try:
            if verbose and attempt > 0:
                print(f"Retry attempt {attempt + 1}/{max_retries}...")

            response = yield LLMCall(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=messages,
                system=system,
            )

            if logger is not None:
                logger.info(f"Response:\n{response}\n{'='*80}\n")

            if verbose:
                print(f"✅ LLM call successful")

            return response

        except anthropic.RateLimitError as e:
            if verbose:
                print(f"Rate limit hit: {e}")
            if attempt < max_retries - 1:
                wait_time = 2**attempt
                if verbose:
                    print(f"Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
            else:
                if verbose:
                    print("Max retries reached")
                if logger is not None:
                    logger.error(f"Error: Rate limit after {max_retries} attempts\n{'='*80}\n")
                return None

        except anthropic.AuthenticationError:
            raise  # Never silently swallow auth errors
        except Exception as e:
            if verbose:
                print(f"Error calling LLM: {e}")
            if logger is not None:
                logger.error(f"Error: {e}\n{'='*80}\n")
            return None

    return None


def _load_anthropic_key(verbose: bool, api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        api_key = os.getenv(ANTHROPIC_API_KEY_ENV)

    if not api_key:
        if verbose:
            print(f"Error: No API key provided and {ANTHROPIC_API_KEY_ENV} environment variable not set")
        return None
    return api_key


@overload
def _init_client(verbose: bool, api_key: Optional[str]) -> Optional["anthropic.Anthropic"]: ...
@overload
def _init_client(
    verbose: bool, api_key: Optional[str], *, raise_on_failure: Literal[True]
) -> "anthropic.Anthropic": ...


def _init_client(
    verbose: bool, api_key: Optional[str], *, raise_on_failure: bool = False
) -> Optional["anthropic.Anthropic"]:
    """Load API key, return a cached sync client. Raises or returns None on failure."""
    loaded_key = _load_anthropic_key(verbose, api_key)
    if loaded_key is None:
        if raise_on_failure:
            raise RuntimeError(f"{ANTHROPIC_API_KEY_ENV} environment variable not set")
        return None
    client = _get_cached_client(loaded_key)
    if client is None:
        if raise_on_failure:
            raise RuntimeError("Failed to initialize Anthropic client")
        if verbose:
            print("Error: Failed to initialize Anthropic client")
    return client


def _init_async_client(verbose: bool, api_key: Optional[str]) -> Optional["anthropic.AsyncAnthropic"]:
    """Load API key, return a cached async client. Returns None on failure."""
    loaded_key = _load_anthropic_key(verbose, api_key)
    if loaded_key is None:
        if verbose:
            print("Failed to load Anthropic API key")
        return None
    client = _get_cached_client(loaded_key, async_client=True)
    if client is None and verbose:
        print("Error: Failed to initialize Anthropic client")
    return client


def _tool_use_params(output_type: type[BaseModel]) -> dict:
    """Return the tools + tool_choice kwargs for forced structured output."""
    return {
        "tools": [
            {
                "name": "output",
                "description": output_type.__doc__ or "Provide your output in structured format",
                "input_schema": output_type.model_json_schema(),
            }
        ],
        "tool_choice": {"type": "tool", "name": "output"},
    }


def _parse_tool_use_block(response: "anthropic.types.Message", output_type: type[M]) -> M:
    """Extract the ToolUseBlock from a forced-tool-use response and validate it."""
    if not isinstance(response.content, list):
        raise ValueError("Expected list response from Claude")
    last = response.content[-1]
    if not isinstance(last, anthropic.types.ToolUseBlock):
        raise ValueError("Forced tool calling did not produce ToolUseBlock")
    return output_type.model_validate(last.input)


def tool_use_params(output_type: type[BaseModel]) -> dict:
    """Return the tools + tool_choice kwargs for forced structured output (public API)."""
    return _tool_use_params(output_type)


def parse_tool_use_block(response: "anthropic.types.Message", output_type: type[M]) -> M:
    """Extract and validate the ToolUseBlock from a forced-tool-use response (public API)."""
    return _parse_tool_use_block(response, output_type)


def get_anthropic_client() -> "anthropic.Anthropic":
    """Return an initialized RAW (unwrapped) Anthropic client, raising on failure.

    Returns the underlying SDK client for external callers (e.g. PreAudit) that do
    their own token-usage tracking. AutoSetup's own code uses the ``call_llm_*``
    wrappers / cached factories, whose usage is auto-recorded into the ledger.
    """
    return _unwrap_client(_init_client(verbose=False, api_key=None, raise_on_failure=True))


def build_batch_request_params(
    system: list,
    messages: list,
    output_type: type[BaseModel],
    model: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> dict:
    """Build params dict for an Anthropic batch API request with forced tool use."""
    model = model or default_anthropic_model()
    params = _tool_use_params(output_type)
    params.update({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": messages,
    })
    return params


def build_openai_batch_request_params(
    system: list,
    messages: list,
    output_type: type[BaseModel],
    model: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> dict:
    """Build the inner request body for one OpenAI-compatible batch line.

    Sibling of ``build_batch_request_params`` for the OpenAI Batches API
    (e.g. Together AI). The caller is responsible for wrapping this body
    with ``{"custom_id": ..., "method": "POST", "url": "/v1/chat/completions",
    "body": <result>}`` before serializing to JSONL.

    Uses ``response_format={"type": "json_schema", ...}`` for structured
    output — providers that don't honor JSON-schema response_format won't
    work here (Together AI / vLLM do; raw Ollama doesn't).
    """
    model = model or _pick_openai_compatible_model()
    return {
        "model": model,
        "messages": _anthropic_messages_to_openai(system, messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "output",
                "schema": output_type.model_json_schema(),
                "strict": True,
            },
        },
    }


def _retry_transient(
    api_call: Callable[[], "anthropic.types.Message"],
    max_retries: int,
    verbose: bool,
) -> "anthropic.types.Message":
    """Call *api_call* with retries on transient API errors. Raises on fatal errors."""
    last_exception: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return api_call()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            last_exception = e
            if verbose:
                print(f"Transient API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                last_exception = e
                if verbose:
                    print(f"API overloaded (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2**attempt)
            else:
                raise
    raise last_exception  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def call_llm_structured(
    prompt: str,
    ty: type[M],
    model: Optional[str] = None,
    max_tokens: int = 1000,
    temperature: float = 0.0,
    max_retries: int = 10,
    log_to_file: bool = True,
    log_path: Optional[str] = None,
    api_key: Optional[str] = None,
    verbose: bool = False,
    on_token_usage: Optional[UsageCallback] = None,
) -> Optional[M]:
    model = model or default_anthropic_model()
    if _get_backend() == LLMBackend.MOCK:
        from certora_autosetup.utils.llm_mock import generate_mock_structured
        return generate_mock_structured(ty)

    if is_openai_compatible_backend():
        client = _pick_openai_compatible_client(async_client=False)
        openai_messages = [{"role": "user", "content": prompt}]
        return _local_call_structured(
            client, _pick_openai_compatible_model(), openai_messages, max_tokens, temperature, ty,
            max_retries, verbose, on_usage=on_token_usage,
        )

    client = _init_client(verbose, api_key)
    if client is None:
        return None

    gen: Generator[LLMCall, M, Optional[M]] = _call_llm_pure(
        prompt, model, max_tokens, temperature, max_retries, log_to_file, log_path, verbose
    )
    try:
        msg = next(gen)
        while True:
            try:
                response = client.messages.create(
                    model=msg.model,
                    max_tokens=msg.max_tokens,
                    messages=msg.messages,
                    temperature=msg.temperature,
                    **_tool_use_params(ty),
                )
                _emit_anthropic_usage(response, on_token_usage)
                msg = gen.send(_parse_tool_use_block(response, ty))
            except Exception as e:
                msg = gen.throw(e)
    except StopIteration as e:
        return e.value


def call_llm(
    prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 1000,
    temperature: float = 0.0,
    max_retries: int = 10,
    log_to_file: bool = True,
    log_path: Optional[str] = None,
    api_key: Optional[str] = None,
    verbose: bool = False,
    on_token_usage: Optional[UsageCallback] = None,
) -> Optional[str]:
    """
    Make a call to the LLM API with the given prompt.

    Args:
        prompt: The prompt to send to the LLM
        model: The model to use (default: latest Claude Sonnet)
        max_tokens: Maximum tokens in response
        temperature: Temperature for response generation (0.0 = deterministic)
        max_retries: Number of retries for rate limiting
        log_to_file: Whether to log to a file
        log_path: Path to log file (default: .certora_internal/llm_util.log)
        api_key: API key (if not provided, uses ANTHROPIC_API_KEY env var)
        verbose: Whether to print verbose output

    Returns:
        The LLM response text, or None if failed

    Example:
        >>> from utils.llm_util import call_llm
        >>> response = call_llm("What is 2+2?")
        >>> print(response)
        4
    """
    model = model or default_anthropic_model()
    if _get_backend() == LLMBackend.MOCK:
        from certora_autosetup.utils.llm_mock import generate_mock_text
        return generate_mock_text()

    if is_openai_compatible_backend():
        client = _pick_openai_compatible_client(async_client=False)
        openai_messages = [{"role": "user", "content": prompt}]
        return _local_call_text(
            client, _pick_openai_compatible_model(), openai_messages, max_tokens, temperature,
            max_retries, verbose, on_usage=on_token_usage,
        )

    client = _init_client(verbose, api_key)
    if client is None:
        return None

    gen: Generator[LLMCall, str, Optional[str]] = _call_llm_pure(
        prompt, model, max_tokens, temperature, max_retries, log_to_file, log_path, verbose
    )
    try:
        msg = next(gen)
        while True:
            try:
                response = client.messages.create(
                    model=msg.model,
                    max_tokens=msg.max_tokens,
                    messages=msg.messages,
                    temperature=msg.temperature,
                )
                _emit_anthropic_usage(response, on_token_usage)
                content_block = response.content[0]
                if hasattr(content_block, "text"):
                    answer = content_block.text.strip()  # type: ignore[union-attr]
                else:
                    answer = str(content_block)
                msg = gen.send(answer)
            except anthropic.AuthenticationError:
                raise
            except Exception as e:
                msg = gen.throw(e)
    except StopIteration as e:
        return e.value


async def call_llm_async_structured_cached(
    system_prompt: str,
    user_prompt: str,
    ty: type[M],
    model: Optional[str] = None,
    max_tokens: int = 1000,
    temperature: float = 0.0,
    max_retries: int = 10,
    log_to_file: bool = True,
    log_path: Optional[str] = None,
    api_key: Optional[str] = None,
    verbose: bool = False,
    on_token_usage: Optional[UsageCallback] = None,
) -> Optional[M]:
    """Like call_llm_async_structured but with a separate system prompt that gets cache_control."""
    model = model or default_anthropic_model()
    if _get_backend() == LLMBackend.MOCK:
        from certora_autosetup.utils.llm_mock import generate_mock_structured
        return generate_mock_structured(ty)

    if is_openai_compatible_backend():
        client = _pick_openai_compatible_client(async_client=True)
        openai_messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        return await _local_call_structured_async(
            client, _pick_openai_compatible_model(), openai_messages, max_tokens, temperature, ty,
            max_retries, verbose, on_usage=on_token_usage,
        )

    client = _init_async_client(verbose, api_key)
    if client is None:
        return None

    gen: Generator[LLMCall, M, Optional[M]] = _call_llm_pure(
        user_prompt,
        model,
        max_tokens,
        temperature,
        max_retries,
        log_to_file,
        log_path,
        verbose,
        system_prompt=system_prompt,
    )
    try:
        msg = next(gen)
        while True:
            try:
                create_kwargs: dict[str, Any] = {
                    "model": msg.model,
                    "max_tokens": msg.max_tokens,
                    "messages": msg.messages,
                    "temperature": msg.temperature,
                    **_tool_use_params(ty),
                }
                if msg.system is not None:
                    create_kwargs["system"] = msg.system
                response = await client.messages.create(**create_kwargs)  # type: ignore[arg-type]
                _emit_anthropic_usage(response, on_token_usage)
                if log_to_file:
                    usage = response.usage
                    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                    logging.getLogger("llm_util").info(
                        f"Usage: input_tokens={usage.input_tokens}, output_tokens={usage.output_tokens}, "
                        f"cache_creation_input_tokens={cache_creation}, cache_read_input_tokens={cache_read}"
                    )
                msg = gen.send(_parse_tool_use_block(response, ty))
            except anthropic.AuthenticationError:
                raise
            except Exception as e:
                msg = gen.throw(e)
    except StopIteration as e:
        return e.value


def call_llm_messages_structured(
    system: list["anthropic.types.TextBlockParam"],
    messages: list["anthropic.types.MessageParam"],
    output_type: type[M],
    model: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    max_retries: int = 3,
    max_validation_retries: int = 2,
    on_token_usage: Optional[UsageCallback] = None,
    api_key: Optional[str] = None,
    verbose: bool = False,
) -> M:
    """Call Claude with pre-built system/messages, forced tool use, and validation retry.

    On Pydantic ValidationError, feeds the error back to the LLM as a tool_result(is_error=True)
    and retries, giving the model a chance to self-correct.

    Args:
        system: Pre-built system content blocks (with cache_control as needed).
        messages: Pre-built message list.
        output_type: Pydantic model class for structured output.
        model: Claude model identifier.
        max_tokens: Maximum tokens in response.
        temperature: Sampling temperature.
        max_retries: Retries for transient API errors per validation attempt.
        max_validation_retries: How many times to retry on ValidationError.
        on_token_usage: Normalized per-response usage callback (LLMUsageEvent).
        api_key: Anthropic API key (falls back to env var).
        verbose: Print verbose output.

    Returns:
        Parsed Pydantic model instance.

    Raises:
        RuntimeError: If API key or client cannot be initialized.
        anthropic.AuthenticationError, anthropic.PermissionDeniedError: Fatal, raised immediately.
        Exception: Last transient error if all retries exhausted.
        ValidationError: If all validation retries exhausted.
    """
    model = model or default_anthropic_model()
    if _get_backend() == LLMBackend.MOCK:
        from certora_autosetup.utils.llm_mock import generate_mock_structured
        return generate_mock_structured(output_type)

    if is_openai_compatible_backend():
        client = _pick_openai_compatible_client(async_client=False)
        openai_messages = _anthropic_messages_to_openai(system, messages)
        return _local_call_structured(
            client, _pick_openai_compatible_model(), openai_messages, max_tokens, temperature, output_type,
            max_retries, verbose, on_usage=on_token_usage,
        )

    client = _init_client(verbose, api_key, raise_on_failure=True)

    tool_params = _tool_use_params(output_type)
    conversation = list(messages)  # never mutate caller's list
    last_validation_error: Optional[ValidationError] = None

    for _validation_attempt in range(1 + max_validation_retries):
        response = _retry_transient(
            lambda: client.messages.create(  # type: ignore[arg-type]
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=conversation,
                **tool_params,
            ),
            max_retries,
            verbose,
        )
        _emit_anthropic_usage(response, on_token_usage)

        try:
            return _parse_tool_use_block(response, output_type)
        except ValidationError as ve:
            last_validation_error = ve
            if verbose:
                print(f"Validation error, retrying: {ve}")
            last_block = response.content[-1]
            assert isinstance(last_block, anthropic.types.ToolUseBlock)
            conversation.append({"role": "assistant", "content": response.content})
            conversation.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": last_block.id,
                            "is_error": True,
                            "content": str(ve),
                        }
                    ],
                }
            )

    raise last_validation_error  # type: ignore[misc]


def call_llm_messages(
    system: list["anthropic.types.TextBlockParam"],
    messages: list["anthropic.types.MessageParam"],
    model: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    max_retries: int = 3,
    on_token_usage: Optional[UsageCallback] = None,
    api_key: Optional[str] = None,
    verbose: bool = False,
) -> str:
    """Call Claude with pre-built system/messages and return plain text.

    Retries on transient API errors with exponential backoff.

    Args:
        system: Pre-built system content blocks (with cache_control as needed).
        messages: Pre-built message list.
        model: Claude model identifier.
        max_tokens: Maximum tokens in response.
        temperature: Sampling temperature.
        max_retries: Retries for transient API errors.
        on_token_usage: Normalized per-response usage callback (LLMUsageEvent).
        api_key: Anthropic API key (falls back to env var).
        verbose: Print verbose output.

    Returns:
        Text content from the first content block.

    Raises:
        RuntimeError: If API key or client cannot be initialized.
        anthropic.AuthenticationError, anthropic.PermissionDeniedError: Fatal, raised immediately.
        Exception: Last transient error if all retries exhausted.
    """
    model = model or default_anthropic_model()
    if _get_backend() == LLMBackend.MOCK:
        from certora_autosetup.utils.llm_mock import generate_mock_text
        return generate_mock_text()

    if is_openai_compatible_backend():
        client = _pick_openai_compatible_client(async_client=False)
        openai_messages = _anthropic_messages_to_openai(system, messages)
        return _local_call_text(
            client, _pick_openai_compatible_model(), openai_messages, max_tokens, temperature,
            max_retries, verbose, on_usage=on_token_usage,
        )

    client = _init_client(verbose, api_key, raise_on_failure=True)

    response = _retry_transient(
        lambda: client.messages.create(  # type: ignore[arg-type]
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages,
        ),
        max_retries,
        verbose,
    )
    _emit_anthropic_usage(response, on_token_usage)

    content_block = response.content[0]
    if hasattr(content_block, "text"):
        return content_block.text.strip()  # type: ignore[union-attr]
    return str(content_block)


def test_llm_connection(api_key: Optional[str] = None, verbose: bool = True) -> bool:
    """
    Test if LLM connection is working.

    Args:
        api_key: API key (if not provided, uses ANTHROPIC_API_KEY env var)
        verbose: Whether to print output

    Returns:
        True if connection works, False otherwise
    """
    response = call_llm(
        "Reply with just 'OK' if you receive this message.",
        max_tokens=10,
        log_to_file=False,
        api_key=api_key,
        verbose=verbose,
    )

    if response and "OK" in response:
        if verbose:
            print("✅ LLM connection test successful")
        return True
    else:
        if verbose:
            print("❌ LLM connection test failed")
        return False


def main():
    """Command-line interface for testing LLM calls."""
    import argparse

    parser = argparse.ArgumentParser(description="Test LLM API calls")
    parser.add_argument("prompt", nargs="?", help="Prompt to send to LLM")
    parser.add_argument("--model", default=default_anthropic_model(), help="Model to use")
    parser.add_argument("--max-tokens", type=int, default=1000, help="Maximum tokens in response")
    parser.add_argument("--temperature", type=float, default=0.0, help="Temperature for response generation")
    parser.add_argument("--test", action="store_true", help="Test LLM connection")
    parser.add_argument("--no-log", action="store_true", help="Don't log to file")
    parser.add_argument("--log-path", help="Path to log file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.test:
        success = test_llm_connection(verbose=args.verbose)
        sys.exit(0 if success else 1)

    if not args.prompt:
        parser.print_help()
        sys.exit(1)

    response = call_llm(
        prompt=args.prompt,
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        log_to_file=not args.no_log,
        log_path=args.log_path,
        verbose=args.verbose,
    )

    if response:
        print(response)
    else:
        if not args.verbose:
            print("Error: LLM call failed. Use -v for details.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
