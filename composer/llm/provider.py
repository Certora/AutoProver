"""LLM-provider type surface (leaf module).

Pure types shared by the per-provider implementations and the registry:
``ProviderKind``, ``CacheLevel``, the ``ModelProvider`` Protocol each backend
implements, and the small token-stream helper the model-name parsers use.

Kept dependency-free — no runtime import of the concrete providers, the
registry, or ``composer.input.files`` — so both ``composer.input.files`` and
the per-provider modules can import it without an import cycle.
"""

from typing import Literal, Protocol, TYPE_CHECKING
from dataclasses import dataclass, field
import enum

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


type ProviderKind = Literal["anthropic", "openai"]


class CacheLevel(enum.StrEnum):
    NONE = "none"
    SHORT = "short"
    LONG = "long"


class ModelProvider(Protocol):
    """A provider-specific LLM backend, bound to one model.

    Holds the per-run model options and the probed model features; ``builder_for``
    mints a chat model with the cache/thinking choice deferred to the call site."""

    @property
    def provider(self) -> ProviderKind: ...

    def builder_for(
        self, *, cache_level: CacheLevel | None = None, disable_thinking: bool = False
    ) -> "BaseChatModel": ...


class NoSuchElementError(RuntimeError):
    pass


@dataclass
class _ListIter[T]:
    l: list[T]
    ind: int = field(default=0)

    def has_next(self) -> bool:
        return self.ind < len(self.l)

    def peek(self) -> T:
        if not self.has_next():
            raise NoSuchElementError("Invalid state, no more elements")
        return self.l[self.ind]

    def next(self) -> T:
        if not self.has_next():
            raise NoSuchElementError("Invalid state, no more elements")
        to_ret = self.l[self.ind]
        self.ind += 1
        return to_ret
