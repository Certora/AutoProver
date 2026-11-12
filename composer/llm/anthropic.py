"""Anthropic LLM backend: model-name probing, Files-API uploader, and the
``ModelProvider`` implementation that mints ``ChatAnthropic`` instances."""

from typing import Literal, TypeGuard, Any, TYPE_CHECKING, override, cast
from io import BytesIO
from dataclasses import dataclass, field
import asyncio
import base64
from functools import cache

import anthropic
import httpx

from composer.input.files import (
    UploaderBase, ContentRenderer, FileUploader, FileData, Document, TextDocument,
    InMemoryBytesFile, InMemoryTextFile,
)
from composer.input.types import ModelConfiguration
from composer.llm.provider import (
    ProviderServiceBase, ProviderSpec, compaction_threshold, payload_error_type,
    standard_callbacks
)
from composer.llm.pricing import PriceProvider, price_provider_for
from composer.llm.bedrock import (
    MAX_TOKENS_ENV_VAR, llm_backend, bedrock_region, max_document_bytes, max_output_tokens,
    max_request_bytes, aws_error_type, is_transient_error_type,
)
from composer.llm.bedrock_inline import INLINE_MEDIA_TYPES, base64_size, omitted_document_block
from .types import CacheLevel
from .list_iter import ListIter, NoSuchElementError

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from graphcore.graph import RawMessageType


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
    stream = ListIter(model_name.split("-"))
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

def level_to_ttl(c: CacheLevel) -> str | None:
    match c:
        case CacheLevel.NONE:
            return None
        case CacheLevel.SHORT:
            return "5m"
        case CacheLevel.LONG:
            return "1h"

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

class AnthropicRenderer:
    def text_block(self, text: str, *, cache_level: CacheLevel = CacheLevel.NONE) -> dict:
        to_ret : dict[str, Any] = {"type": "text", "text": text}
        if (ttl := level_to_ttl(cache_level)) is not None:
            to_ret["cache_control"] = {
                "type": "ephemeral",
                "ttl": ttl
            }
        return to_ret

    def file_block(
        self, file_id: str, *, cache_level: CacheLevel = CacheLevel.NONE
    ) -> dict:
        to_ret : dict[str, Any] = {
            "type": "document",
            "source": {
                "type": "file",
                "file_id": file_id,
            },
        }
        if (ttl := level_to_ttl(cache_level)) is not None:
            to_ret["cache_control"] = {
                "type": "ephemeral",
                "ttl": ttl
            }
        return to_ret

    def inline_file_block(
        self, basename: str, contents: bytes, mime: str,
        *, cache_level: CacheLevel = CacheLevel.NONE
    ) -> dict:
        # ``title`` names the document to the model, and lets a request that has to drop
        # the block for size say which document it dropped.
        to_ret: dict[str, Any] = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.b64encode(contents).decode("ascii"),
            },
            "title": basename,
        }
        if (ttl := level_to_ttl(cache_level)) is not None:
            to_ret["cache_control"] = {
                "type": "ephemeral",
                "ttl": ttl
            }
        return to_ret


@dataclass
class BedrockRenderer(AnthropicRenderer):
    """Content blocks for Claude on Bedrock: binary documents are inlined, within the
    per-document byte budget. A document that cannot be inlined, being over budget or of a
    type a ``document`` block does not accept, becomes a text block that names it."""

    max_document_bytes: int = field(default_factory=max_document_bytes)

    @override
    def file_block(
        self, file_id: str, *, cache_level: CacheLevel = CacheLevel.NONE
    ) -> dict:
        raise NotImplementedError(
            "Bedrock has no Files API; binary content is inlined by BedrockFileUploader "
            "as an InMemoryBytesFile."
        )

    @override
    def inline_file_block(
        self, basename: str, contents: bytes, mime: str,
        *, cache_level: CacheLevel = CacheLevel.NONE
    ) -> dict:
        cache_control = (
            {"type": "ephemeral", "ttl": ttl}
            if (ttl := level_to_ttl(cache_level)) is not None else None
        )
        if mime not in INLINE_MEDIA_TYPES:
            return omitted_document_block(
                basename,
                f"Claude on Bedrock accepts inline documents of type "
                f"{', '.join(sorted(INLINE_MEDIA_TYPES))} only, and this one is {mime}",
                cache_control=cache_control,
            )
        if (size := base64_size(len(contents))) > self.max_document_bytes:
            return omitted_document_block(
                basename,
                f"it is {size} bytes as base64, over the {self.max_document_bytes}-byte "
                "per-document budget for Claude on Bedrock",
                cache_control=cache_control,
            )
        return super().inline_file_block(basename, contents, mime, cache_level=cache_level)

@cache
def _get_service():
    return AnthropicService()

@dataclass
class AnthropicFileUploader(UploaderBase):
    """``FileUploader`` impl backed by Anthropic's beta Files API."""

    client: anthropic.AsyncAnthropic
    uploaded: dict[str, str] | None = None
    _seed_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    renderer: ContentRenderer = field(default_factory=AnthropicRenderer)

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
        """A lazily-seeding uploader — no account file-list until first upload. The Files
        API exists only on the Anthropic API, so this refuses to build a first-party
        client when the Bedrock backend is selected; use :func:`uploader_for_backend`."""
        if llm_backend() != "anthropic":
            raise ValueError(
                "The Anthropic Files API uploader is only available with the anthropic "
                "LLM backend"
            )
        return AnthropicFileUploader(client=anthropic.AsyncAnthropic())


@dataclass
class BedrockFileUploader(UploaderBase):
    """``FileUploader`` for Claude on Bedrock, which has no Files API. Text that would be
    uploaded stays in the prompt instead, and binary documents become
    :class:`InMemoryBytesFile`, sent as base64 in every request that carries them. The
    document keeps its bytes whatever its size; :class:`BedrockRenderer` decides, when it is
    rendered, whether they fit the budget."""

    renderer: ContentRenderer = field(default_factory=BedrockRenderer)

    @override
    async def _upload_bytes(
        self, crc_basename: str, file_data: bytes, mime: str
    ) -> str:
        raise NotImplementedError("Bedrock has no Files API.")

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
        # There is no upload to make it smaller than the prompt, so the very-large-text
        # case this exists for collapses into the ordinary one.
        return InMemoryTextFile(
            basename=data.basename,
            string_contents=data.raw_data.decode("utf-8"),
            renderer=self.renderer,
        )


def uploader_for_backend() -> FileUploader:
    """The Claude uploader for the selected LLM backend. Never builds a first-party
    Anthropic client when the Bedrock backend is selected."""
    match llm_backend():
        case "anthropic":
            return AnthropicFileUploader.lazy()
        case "bedrock":
            return BedrockFileUploader()


# --- ModelProvider ---------------------------------------------------------


INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"

RETRYABLE_ERROR_TYPES = frozenset({
    "overloaded_error",  # 529
    "api_error",         # 500
    "rate_limit_error",  # 429
    "timeout_error",     # 408
})
"""The ``error.type`` values of the statuses ``should_retry`` accepts, for payloads whose
status code cannot speak for them."""


class AnthropicService(ProviderServiceBase):
    def __init__(self):
        from graphcore.tools.memory import anthropic_async_memory_tool
        super().__init__(
            anthropic_async_memory_tool,
            uploader_for_backend
        )

    @override
    def cache_marker(self, payload: "RawMessageType", cache_level: CacheLevel) -> "RawMessageType":
        if (ttl := level_to_ttl(cache_level)) is None:
            return super().cache_marker(payload, cache_level)
        to_ret = payload
        if not isinstance(to_ret, dict):
            to_ret = cast(dict, {"type": "text", "text": to_ret})
        else:
            to_ret = to_ret.copy()
        to_ret["cache_control"] = {
            "type": "ephemeral",
            "ttl": ttl
        }
        return to_ret

    @override
    def should_retry(self, exc: Exception) -> bool:
        """Mirrors the SDK's own ``_should_retry`` status roster (408/409/429
        and every 5xx, which covers 529 overloaded) plus connection-level
        failures (``APITimeoutError`` subclasses ``APIConnectionError``), a
        transport failure mid-stream, and an error the server reports inside a
        stream. 400-class request errors are deterministic — an over-long prompt
        fails identically on every attempt — and are deliberately excluded.

        On Bedrock the same statuses carry the same meaning (429 throttling and model
        not ready, 500, 503, 408 model timeout; 400 validation, 403 access denied and
        404 unknown model are deterministic). Bedrock's exception names for transient
        errors count as well (``composer.llm.bedrock.TRANSIENT_ERROR_TYPES``, read from the
        payload or, failing that, the ``x-amzn-errortype`` header), and so
        does any ``RetryableError``, which is how a passing failure to fetch the AWS
        credentials that sign a request reaches here once the SDK's retries are spent."""
        if isinstance(exc, (anthropic.APIConnectionError, anthropic.RetryableError)):
            return True
        # The SDK does not wrap streamed-body failures as APIConnectionError: a dropped stream
        # raises httpx.RemoteProtocolError, a stalled one httpx.ReadTimeout. Both are transient.
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
                return True
            # An error the server reports part-way through a stream rides the already-open
            # 200 response, and the SDK builds the exception from that response — so the
            # status code carries none of the meaning and the class stays the base
            # APIStatusError. The transient/deterministic split lives in the payload.
            # AWS front ends may name the exception in a response header, either instead of
            # an ``error.type`` in the body or next to a generic one (``permission_error``
            # for an expired token), so a transient name in either place counts.
            error_types = (payload_error_type(exc.body), aws_error_type(exc.response.headers))
            return any(
                t in RETRYABLE_ERROR_TYPES or is_transient_error_type(t) for t in error_types
            )
        return False

@dataclass
class AnthropicModelProvider:
    """``ModelProvider`` for Anthropic. Probes ``model_name`` once at
    construction; ``builder_for`` reads the resulting features to pick the
    thinking mode and interleaved-thinking beta."""

    model_name: str
    options: ModelConfiguration
    features: ModelFeatures
    price_provider: PriceProvider

    provider: AnthropicService = field(default_factory=_get_service)


    @staticmethod
    def create(model_name: str, options: ModelConfiguration) -> "AnthropicModelProvider":
        return AnthropicModelProvider(
            model_name,
            options,
            _model_parser(model_name),
            price_provider_for(model_name)
        )

    @property
    def max_prompt_tokens(self) -> int:
        return compaction_threshold(_context_window(self.features))

    def builder_for(
        self, *, cache_level: CacheLevel = CacheLevel.NONE, disable_thinking: bool = False
    ) -> "BaseChatModel":
        from langchain_anthropic import ChatAnthropic

        # Read first so a bad AUTOPROVER_LLM_BACKEND (or, for Bedrock, a missing region or a
        # malformed request budget) fails here, when the model is built, rather than on its
        # first request.
        backend = llm_backend()
        bedrock_settings = (
            (bedrock_region(), max_request_bytes(), max_output_tokens())
            if backend == "bedrock" else None
        )

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

        betas: list[str] = []
        if backend == "anthropic":
            # Documents uploaded through the Files API are referenced by id, which needs
            # this beta. Bedrock has no Files API, so its requests never carry such a block.
            betas.append("files-api-2025-04-14")
        if self.features.interleaved_thinking and opts.interleaved_thinking:
            betas.append(INTERLEAVED_THINKING_BETA)
        if opts.memory_tool:
            betas.append("context-management-2025-06-27")

        ttl = level_to_ttl(cache_level)
        model_kwargs = (
            {"cache_control": {"type": "ephemeral", "ttl": ttl}} if ttl is not None else {}
        )

        callbacks = standard_callbacks(
            self.price_provider, long_cache=cache_level == CacheLevel.LONG
        )
        if bedrock_settings is not None:
            from composer.llm.bedrock_chat import ChatAnthropicBedrock
            aws_region, request_budget, token_cap = bedrock_settings
            # Bedrock counts max_tokens against the token quota when it admits a request, so
            # it is capped here; see composer.llm.bedrock.DEFAULT_MAX_TOKENS.
            max_tokens = min(opts.tokens, token_cap)
            # With interleaved thinking the budget covers every thinking block of a turn and
            # may exceed max_tokens; without it, one thinking block must fit below max_tokens.
            budget = (thinking or {}).get("budget_tokens")
            interleaved = INTERLEAVED_THINKING_BETA in betas
            if budget is not None and not interleaved and budget >= max_tokens:
                raise ValueError(
                    f"The thinking budget of {budget} tokens must be below max_tokens, which "
                    f"on Bedrock is {max_tokens}, the smaller of --tokens ({opts.tokens}) and "
                    f"{MAX_TOKENS_ENV_VAR} ({token_cap}); lower --thinking-tokens or raise "
                    "the other two"
                )
            return ChatAnthropicBedrock(
                aws_region=aws_region,
                max_request_bytes=request_budget,
                model_name=self.model_name,
                max_tokens_to_sample=max_tokens,
                # The same transport settings as the Anthropic API path below; see there.
                timeout=300.0,
                streaming=True,
                max_retries=8,
                stop=None,
                # None rather than an empty list: with no betas the request goes to the
                # plain messages endpoint instead of the beta one.
                betas=betas or None,
                thinking=thinking,
                model_kwargs=model_kwargs,
                callbacks=callbacks,
            )

        return ChatAnthropic(
            model_name=self.model_name,
            max_tokens_to_sample=opts.tokens,
            # An explicit None DISABLES the SDK's timeouts (None != not-given), so a
            # socket that dies silently mid-stream hangs the session forever. A float
            # is a per-phase httpx timeout — for a streamed response, the max silence
            # between chunks, not a cap on the whole turn.
            timeout=300.0,
            # Stream every request: a long authoring turn (Opus + thinking on a large
            # prompt) can exceed the SDK's 600s non-streaming ceiling, and a silent
            # 10-minute wait is long enough for NAT/idle killers to drop the socket
            # (surfaces as APIConnectionError mid-run). Streaming keeps bytes flowing.
            streaming=True,
            max_retries=8,
            stop=None,
            betas=betas,
            thinking=thinking,
            model_kwargs=model_kwargs,
            callbacks=callbacks,
        )

ANTHROPIC_SPEC = ProviderSpec(
    matches=matches,
    build=AnthropicModelProvider.create
)
