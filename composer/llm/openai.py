"""OpenAI LLM backend: model-name probing, Files-API uploader, and the
``ModelProvider`` implementation that mints ``ChatOpenAI`` instances.

OpenAI names split on "-" far less cleanly than Claude's. Two families:
  * "gpt"  — gpt-3.5-turbo, gpt-4, gpt-4o, gpt-4.1-mini, gpt-4.5, gpt-5-nano
  * "o"    — the o-series reasoning models, where the family letter and the
             version are glued into the first token: o1, o3-mini, o4-mini
Quirks handled below: the "o" omni suffix on a gpt version ("4o"), dotted minor
versions ("4.1", "3.5"), and the glued o-series head. Trailing snapshot/date/
variant tokens (-2024-08-06, -turbo, -preview, -32k) carry no reasoning-relevant
signal and are ignored.
"""

from typing import Literal, TypeGuard, Any, TYPE_CHECKING
import io
from dataclasses import dataclass, field
import asyncio

import openai

from composer.input.files import _UploaderBase
from composer.input.types import ModelConfiguration
from composer.llm.provider import (
    ProviderKind, CacheLevel, _ListIter, NoSuchElementError,
)

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


# --- model probing ---------------------------------------------------------

type OpenAIFamily = Literal["gpt", "o"]
type OpenAITier = Literal["standard", "mini", "nano", "pro"]

@dataclass
class OpenAIModelFeatures:
    # Reasoning model: emits reasoning tokens and takes ``reasoning_effort``
    # rather than ``temperature``/``top_p``. True for the whole o-series and for
    # gpt-5 and later; gpt-3.5/4/4o/4.1/4.5 are plain chat models.
    reasoning: bool
    version_tuple: tuple[int, int]
    family: OpenAIFamily
    tier: OpenAITier

_valid_tiers: set[OpenAITier] = {"standard", "mini", "nano", "pro"}

def _validate_tier(s: str) -> TypeGuard[OpenAITier]:
    return s in _valid_tiers

# gpt reasoning starts at gpt-5; gpt-4.5 (< 5.0) stays a plain chat model.
_reasoning_pivot_version = (5, 0)

def _is_o_series(head: str) -> bool:
    """o-series reasoning models glue the family letter to the version (``o1``,
    ``o3``, ``o4-mini``). Match ``o`` + digits rather than enumerating versions."""
    return len(head) >= 2 and head[0] == "o" and head[1:].isdigit()

def _parse_gpt_version(token: str) -> tuple[int, int]:
    # gpt version token: "4", "4o", "4.1", "3.5", "5". A trailing "o" (the omni
    # modality marker on gpt-4o) is not a version digit — strip it.
    if token.endswith("o") and token[:-1].replace(".", "").isdigit():
        token = token[:-1]
    if "." in token:
        major_s, minor_s = token.split(".", 1)
        return (int(major_s), int(minor_s))
    return (int(token), 0)

def _model_parser(model_name: str) -> OpenAIModelFeatures:
    stream = _ListIter(model_name.split("-"))
    parsing: Literal["family", "version", "tier"] = "family"
    try:
        head = stream.next()
        if head == "gpt":
            family: OpenAIFamily = "gpt"
            parsing = "version"
            version_tuple = _parse_gpt_version(stream.next())
        elif _is_o_series(head):
            family = "o"
            version_tuple = (int(head[1:]), 0)
        else:
            raise ValueError(f"Unrecognized model provider/family: {head}")

        parsing = "tier"
        tier: OpenAITier = "standard"
        if stream.has_next():
            candidate = stream.peek()
            if _validate_tier(candidate):
                stream.next()
                tier = candidate
            # else: a snapshot/date/variant token (turbo, preview, 2024-…),
            # not a size tier — leave it unconsumed and default to standard.

        reasoning = family == "o" or (
            family == "gpt" and version_tuple >= _reasoning_pivot_version
        )
        return OpenAIModelFeatures(
            reasoning=reasoning,
            version_tuple=version_tuple,
            family=family,
            tier=tier,
        )
    except NoSuchElementError as exc:
        raise ValueError(f"Error parsing {parsing} from model identifier {model_name}; ran out of tokens") from exc
    except ValueError as exc:
        if parsing != "version":
            raise exc
        raise ValueError(f"Error parsing version from model identifier {model_name}; ill-formed version number") from exc


def matches(model: str) -> bool:
    head = model.split("-", 1)[0]
    return head in ("gpt", "chatgpt") or _is_o_series(head)


def _reasoning_effort(thinking_tokens: int) -> Literal["low", "medium", "high"]:
    """Map a thinking-token budget onto OpenAI's three-step effort knob."""
    if thinking_tokens <= 2048:
        return "low"
    if thinking_tokens <= 8192:
        return "medium"
    return "high"


# --- Files API uploader ----------------------------------------------------

@dataclass
class OpenAIFileUploader(_UploaderBase):
    """``FileUploader`` impl backed by OpenAI's Files API
    (``purpose="user_data"``)."""

    client: openai.AsyncOpenAI
    uploaded: dict[str, str] | None = None
    _seed_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    provider: ProviderKind = "openai"

    async def _ensure_seeded(self) -> dict[str, str]:
        """Seed the dedup cache from the account's existing user-data uploads on
        first use, then return it. Guarded so concurrent first uploads list once."""
        async with self._seed_lock:
            if self.uploaded is None:
                seeded: dict[str, str] = {}
                async for f in self.client.files.list(purpose="user_data"):
                    seeded[f.filename] = f.id
                self.uploaded = seeded
            return self.uploaded

    async def _upload_bytes(
        self, crc_basename: str, file_data: bytes, mime: str
    ) -> str:
        uploaded = await self._ensure_seeded()
        if crc_basename in uploaded:
            return uploaded[crc_basename]
        uploaded_file = await self.client.files.create(
            file=(crc_basename, io.BytesIO(file_data), mime),
            purpose="user_data",
        )
        uploaded[crc_basename] = uploaded_file.id
        return uploaded_file.id

    @staticmethod
    def lazy() -> "OpenAIFileUploader":
        """A lazily-seeding uploader — no account file-list until first upload."""
        return OpenAIFileUploader(client=openai.AsyncOpenAI())


# --- ModelProvider ---------------------------------------------------------

@dataclass
class OpenAIModelProvider:
    """``ModelProvider`` for OpenAI. Probes ``model_name`` once at construction;
    ``builder_for`` forwards ``reasoning_effort`` only on reasoning-capable
    models. ``cache_level`` is ignored — OpenAI has no explicit cache knob."""

    model_name: str
    options: ModelConfiguration
    features: OpenAIModelFeatures
    provider: ProviderKind = "openai"

    @staticmethod
    def create(model_name: str, options: ModelConfiguration) -> "OpenAIModelProvider":
        return OpenAIModelProvider(model_name, options, _model_parser(model_name))

    def builder_for(
        self, *, cache_level: CacheLevel | None = None, disable_thinking: bool = False
    ) -> "BaseChatModel":
        from langchain_openai import ChatOpenAI

        opts = self.options
        kwargs: dict[str, Any] = {
            "use_responses_api": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        
        if opts.thinking_tokens is not None and not disable_thinking and self.features.reasoning:
            kwargs["reasoning"] = {
                "effort": _reasoning_effort(opts.thinking_tokens),
                "summary": "auto"
            }
            
        return ChatOpenAI(
            model=self.model_name,
            max_completion_tokens=opts.tokens,
            temperature=1,
            timeout=None,
            max_retries=2,
            **kwargs,
        )
