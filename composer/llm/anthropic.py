"""Anthropic LLM backend: model-name probing, Files-API uploader, and the
``ModelProvider`` implementation that mints ``ChatAnthropic`` instances."""

from typing import Literal, TypeGuard, Any, TYPE_CHECKING
from io import BytesIO
from dataclasses import dataclass, field
import asyncio

import anthropic

from composer.input.files import _UploaderBase
from composer.input.types import ModelConfiguration
from composer.llm.provider import (
    ProviderKind, CacheLevel, _ListIter, NoSuchElementError, compaction_threshold,
)

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


# --- model probing ---------------------------------------------------------

type ClaudeModelNames = Literal["opus", "sonnet", "haiku", "fable"]

@dataclass
class ModelFeatures:
    interleaved_thinking: bool
    adaptive_thinking: bool
    # Whether summarized thinking has to be asked for. From 4.7 on, a response still carries
    # thinking blocks but their text is empty unless the request opts in; up to 4.6 the summary
    # came back by default.
    thinking_summary_opt_in: bool
    version_tuple: tuple[int, int]
    name: ClaudeModelNames

_valid_names: set[ClaudeModelNames] = {"opus", "sonnet", "haiku", "fable"}

def _validate_model(s: str) -> TypeGuard[ClaudeModelNames]:
    return s in _valid_names

# Interleaved thinking on <= 4.5; adaptive thinking on newer models.
_interleaved_pivot_version = (4, 5)

# From 4.7 on, a response still carries thinking blocks but their
# text is empty unless the request opts in; up to 4.6 the summary
# came back by default.
# Last version to return a thinking summary without being asked.
_default_thinking_summary_version = (4, 6)

def _model_parser(model_name: str) -> ModelFeatures:
    stream = _ListIter(model_name.split("-"))
    parsing: Literal["claude", "model", "version"] = "claude"
    try:
        claude = stream.next()
        if claude != "claude":
            raise ValueError(f"Unrecognized model provider: {claude}")
        parsing = "model"
        model = stream.next()
        if _validate_model(model):
            model_class = model
        else:
            raise ValueError(f"Unrecognized model name: {model}")
        parsing = "version"

        major_version = int(stream.next())
        minor_version = int(stream.next()) if stream.has_next() else 0

        version_tuple = (major_version, minor_version)
        interleaved_flag = version_tuple <= _interleaved_pivot_version
        return ModelFeatures(
            interleaved_thinking=interleaved_flag,
            adaptive_thinking=not interleaved_flag,
            thinking_summary_opt_in=version_tuple > _default_thinking_summary_version,
            name=model_class,
            version_tuple=version_tuple,
        )
    except NoSuchElementError as exc:
        raise ValueError(f"Error parsing {parsing} from model identifier {model_name}; ran out of tokens") from exc
    except ValueError as exc:
        if parsing != "version":
            raise exc
        raise ValueError(f"Error parsing version from model identifier {model_name}; ill-formed version number") from exc


def matches(model: str) -> bool:
    return model.split("-", 1)[0] == "claude"


_long_context_window = 1_000_000
_base_context_window = 200_000


def _long_context_pivot(family: ClaudeModelNames) -> tuple[int, int] | None:
    """The first version of `family` to ship the 1M-token window, or None for a family that has
    none. A version comparison rather than a list of model identifiers, so a new release of a
    family that already crossed over needs no edit here; a `match` so that a family added to
    `ClaudeModelNames` fails the type check here rather than at runtime."""
    match family:
        case "opus" | "sonnet":
            return (4, 6)
        case "fable":
            return (5, 0)
        case "haiku":
            # No long-context haiku has shipped; understating one only costs earlier compaction.
            return None


def _context_window(features: ModelFeatures) -> int:
    pivot = _long_context_pivot(features.name)
    if pivot is not None and features.version_tuple >= pivot:
        return _long_context_window
    return _base_context_window


# --- Files API uploader ----------------------------------------------------

@dataclass
class AnthropicFileUploader(_UploaderBase):
    """``FileUploader`` impl backed by Anthropic's beta Files API."""

    client: anthropic.AsyncAnthropic
    uploaded: dict[str, str] | None = None
    _seed_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    provider: ProviderKind = "anthropic"

    async def _ensure_seeded(self) -> dict[str, str]:
        """Seed the dedup cache from the account's existing Files-API uploads on
        first use, then return it. Guarded so concurrent first uploads list once."""
        async with self._seed_lock:
            if self.uploaded is None:
                seeded: dict[str, str] = {}
                async for f in await self.client.beta.files.list():
                    seeded[f.filename] = f.id
                self.uploaded = seeded
            return self.uploaded

    async def _upload_bytes(
        self, crc_basename: str, file_data: bytes, mime: str
    ) -> str:
        uploaded = await self._ensure_seeded()
        if (res := uploaded.get(crc_basename)):
            return res
        uploaded_file = await self.client.beta.files.upload(
            file=(crc_basename, BytesIO(file_data), mime)
        )
        uploaded[crc_basename] = uploaded_file.id
        return uploaded_file.id

    @staticmethod
    def lazy() -> "AnthropicFileUploader":
        """A lazily-seeding uploader — no account file-list until first upload."""
        return AnthropicFileUploader(client=anthropic.AsyncAnthropic())


# --- ModelProvider ---------------------------------------------------------

@dataclass
class AnthropicModelProvider:
    """``ModelProvider`` for Anthropic. Probes ``model_name`` once at
    construction; ``builder_for`` reads the resulting features to pick the
    thinking mode and interleaved-thinking beta."""

    model_name: str
    options: ModelConfiguration
    features: ModelFeatures
    provider: ProviderKind = "anthropic"

    @staticmethod
    def create(model_name: str, options: ModelConfiguration) -> "AnthropicModelProvider":
        return AnthropicModelProvider(model_name, options, _model_parser(model_name))

    @property
    def max_prompt_tokens(self) -> int:
        return compaction_threshold(_context_window(self.features))

    def builder_for(
        self, *, cache_level: CacheLevel | None = None, disable_thinking: bool = False
    ) -> "BaseChatModel":
        from langchain_anthropic import ChatAnthropic
        from composer.diagnostics.usage_callback import UsageCallback

        opts = self.options
        thinking: dict[str, Any] | None
        if opts.thinking_tokens is None or disable_thinking:
            thinking = None
        elif self.features.adaptive_thinking:
            thinking = {"type": "adaptive"}
            if self.features.thinking_summary_opt_in:
                # Ask for the summary the model would otherwise leave empty. It costs nothing
                # (thinking is billed the same either way), we want it to help with debugging.
                thinking["display"] = "summarized"
        else:
            thinking = {"type": "enabled", "budget_tokens": opts.thinking_tokens}

        betas = ["files-api-2025-04-14"]
        if self.features.interleaved_thinking and opts.interleaved_thinking:
            betas.append("interleaved-thinking-2025-05-14")
        if opts.memory_tool:
            betas.append("context-management-2025-06-27")

        match cache_level:
            case CacheLevel.SHORT:
                ttl = "5m"
            case CacheLevel.LONG:
                ttl = "1h"
            case None | CacheLevel.NONE:
                ttl = None
        model_kwargs = (
            {"cache_control": {"type": "ephemeral", "ttl": ttl}} if ttl else {}
        )

        return ChatAnthropic(
            model_name=self.model_name,
            max_tokens_to_sample=opts.tokens,
            timeout=None,
            max_retries=8,
            stop=None,
            betas=betas,
            thinking=thinking,
            model_kwargs=model_kwargs,
            callbacks=[UsageCallback()],
        )
