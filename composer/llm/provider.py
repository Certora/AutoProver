"""LLM-provider type surface (leaf module).

Pure types shared by the per-provider implementations and the registry:
``ProviderKind``, ``CacheLevel``, the ``ModelProvider`` Protocol each backend
implements, and the small token-stream helper the model-name parsers use.

Kept dependency-free — no runtime import of the concrete providers, the
registry, or ``composer.input.files`` — so both ``composer.input.files`` and
the per-provider modules can import it without an import cycle.
"""

from typing import Protocol, TYPE_CHECKING, Callable
from dataclasses import dataclass
from functools import cached_property
import enum
from composer.input.files import FileUploader
from composer.input.types import ModelConfiguration
from abc import ABC

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from graphcore.tools.memory import AsyncPostgresBackend
    from langchain_core.tools import BaseTool

class ProviderService(Protocol):
    def select_memory_tool(
        self, backend: "AsyncPostgresBackend"
    ) -> "BaseTool":
        ...

    def uploader(self) -> FileUploader:
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

class CacheLevel(enum.StrEnum):
    NONE = "none"
    SHORT = "short"
    LONG = "long"


class ModelProvider(Protocol):
    """A provider-specific LLM backend, bound to one model.

    Holds the per-run model options and the probed model features; ``builder_for``
    mints a chat model with the cache/thinking choice deferred to the call site."""

    @property
    def provider(self) -> ProviderService: ...

    def builder_for(
        self, *, cache_level: CacheLevel | None = None, disable_thinking: bool = False
    ) -> "BaseChatModel": ...

@dataclass(frozen=True)
class ProviderSpec:
    """One row of the provider registry: a name predicate, the provider kind it
    maps to, and the factory that builds the provider's ``ModelProvider``."""
    matches: Callable[[str], bool]
    build: Callable[[str, ModelConfiguration], ModelProvider]
