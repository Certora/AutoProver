"""LLM-provider type surface (leaf module).

Pure types shared by the per-provider implementations and the registry:
``ProviderKind``, ``CacheLevel``, the ``ModelProvider`` Protocol each backend
implements, and the small token-stream helper the model-name parsers use.

Kept dependency-free — no runtime import of the concrete providers, the
registry, or ``composer.input.files`` — so both ``composer.input.files`` and
the per-provider modules can import it without an import cycle.
"""

from typing import Protocol, TYPE_CHECKING, Callable, Literal
from dataclasses import dataclass
from functools import cached_property
from composer.input.files import FileUploader
from composer.input.types import ModelConfiguration
from .pricing import PriceProvider
from .types import CacheLevel
from abc import ABC, abstractmethod


if TYPE_CHECKING:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models.chat_models import BaseChatModel
    from graphcore.tools.memory import AsyncPostgresBackend
    from graphcore.graph import RawMessageType
    from langchain_core.tools import BaseTool

class ProviderService(Protocol):
    def select_memory_tool(
        self, backend: "AsyncPostgresBackend"
    ) -> "BaseTool":
        ...

    def uploader(self) -> FileUploader:
        ...

    def cache_marker(self, payload: "RawMessageType", cache_level: CacheLevel) -> "RawMessageType":
        ...

    def should_retry(self, exc: Exception) -> bool:
        """Whether ``exc`` is a transient provider-side failure worth retrying
        (rate limits, overload, dropped connections) — as opposed to a
        deterministic request error (an over-long prompt 400s identically
        every time). The harness assembles this into the run-wide retry
        policy (``composer.io.context.install_retry_policy``)."""
        ...

class ProviderServiceBase(ABC):
    def __init__(self,
        mem_fact: Callable[["AsyncPostgresBackend"], "BaseTool"],
        uploader_fact: Callable[[], FileUploader]
    ):
        self.mem_fact = mem_fact
        self.uploader_fact = uploader_fact

    @cached_property
    def _uploader_prop(self) -> FileUploader:
        return self.uploader_fact()
    
    def uploader(self) -> FileUploader:
        return self._uploader_prop
    
    def select_memory_tool(
        self, backend: "AsyncPostgresBackend"
    ) -> "BaseTool":
        return self.mem_fact(backend)

    def cache_marker(self, payload: "RawMessageType", cache_level: CacheLevel) -> "RawMessageType":
        return payload

    def should_retry(self, exc: Exception) -> bool:
        # Providers opt in to retryability explicitly; unknown exceptions are
        # never worth an automatic re-run.
        return False


class ModelProvider(Protocol):
    """A provider-specific LLM backend, bound to one model.

    Holds the per-run model options and the probed model features; ``builder_for``
    mints a chat model with the cache/thinking choice deferred to the call site."""

    @property
    def provider(self) -> ProviderService: ...

    @property
    def max_prompt_tokens(self) -> int:
        """Prompt-token threshold at which a workflow should compact its history, derived from this
        model's context window. Handed to graphcore alongside the model itself, since graphcore
        sees only an opaque chat model and cannot work the window out for itself."""
        ...

    def builder_for(
        self, *, cache_level: CacheLevel = CacheLevel.NONE, disable_thinking: bool = False
    ) -> "BaseChatModel": ...

@dataclass(frozen=True)
class ProviderSpec:
    """One row of the provider registry: a name predicate, the provider kind it
    maps to, and the factory that builds the provider's ``ModelProvider``."""
    matches: Callable[[str], bool]
    build: Callable[[str, ModelConfiguration], ModelProvider]


# Compaction has to leave room for everything that rides on top of the prompt just measured: the
# response, the thinking budget, and the next batch of tool results all have to fit the same
# window. Half of it is a working figure, not a derived one.
_PROMPT_TOKEN_SHARE = 0.5


def compaction_threshold(context_window: int) -> int:
    """The prompt-token budget to allow a model with a ``context_window``-token window."""
    return int(context_window * _PROMPT_TOKEN_SHARE)


# Thresholds for the three-step effort knob the OpenAI-shaped providers take in
# place of a thinking-token count. Shared so a retune can't reach one backend and
# miss another — the same budget has to mean the same effort on every route.
_MEDIUM_EFFORT_FROM_TOKENS = 2048
_HIGH_EFFORT_FROM_TOKENS = 8192


def reasoning_effort(thinking_tokens: int) -> Literal["low", "medium", "high"]:
    """Map a thinking-token budget onto the three-step effort knob."""
    if thinking_tokens <= _MEDIUM_EFFORT_FROM_TOKENS:
        return "low"
    if thinking_tokens <= _HIGH_EFFORT_FROM_TOKENS:
        return "medium"
    return "high"


def standard_callbacks(
    price_provider: PriceProvider, *, long_cache: bool = False
) -> list["BaseCallbackHandler"]:
    """The usage + cost instrumentation to attach at model construction.

    Shared because a provider that forgets either one produces an unmetered or
    uncosted run rather than an error. ``long_cache`` selects the 1-hour
    cache-write rate; providers with no cache-TTL knob leave it False. Imported
    lazily to keep this module a dependency-free leaf."""
    from composer.diagnostics.usage_callback import UsageCallback
    from composer.diagnostics.cost_callback import CostAccumulator

    return [UsageCallback(), CostAccumulator(price_provider, long_cache=long_cache)]
