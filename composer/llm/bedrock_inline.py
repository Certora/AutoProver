"""Inline documents for Claude on Bedrock, and the byte budgets that keep requests under
Bedrock's 20 MB cap.

Bedrock has no Files API, so a binary document is a base64 ``document`` block carried in
every request that includes it. Two checks keep a run from failing on size:

- when a document is rendered, one whose base64 body is over the per-document budget
  becomes a text block naming it (:func:`omitted_document_block`);
- when a request is built, :func:`fit_request_to_budget` replaces inline documents with such
  text blocks, latest first, until the serialized request fits the per-request budget.

Both log a warning for each document they leave out, and neither raises for size. A
document that every request of a conversation leaves out is warned about once per
``reported`` set the caller keeps (one per chat model), and logged at DEBUG after that.

Kept free of langchain imports, like :mod:`composer.llm.bedrock`.
"""

import json
import logging
from typing import Any, Callable, TypeGuard

from composer.llm.bedrock import BACKEND_ENV_VAR

logger = logging.getLogger(__name__)

INLINE_MEDIA_TYPES: frozenset[str] = frozenset({"application/pdf"})
"""The media types a base64 ``document`` block accepts."""


def base64_size(raw_size: int) -> int:
    """The length of the base64 encoding of ``raw_size`` bytes, padding included."""
    return 4 * ((raw_size + 2) // 3)


def omitted_document_block(
    name: str, reason: str, *, cache_control: Any = None, log_level: int = logging.WARNING
) -> dict[str, Any]:
    """A text block that stands in for a document left out of the request, telling the model
    which document is missing and why, logged at ``log_level``. ``cache_control`` is carried
    over so the request keeps the cache breakpoint the document held."""
    logger.log(log_level, "Leaving document %r out of a Bedrock request: %s", name, reason)
    block: dict[str, Any] = {
        "type": "text",
        "text": (
            f"[Document {name!r} was omitted: {reason}. Its contents are not available in "
            "this conversation, and reading it again will not attach it.]"
        ),
    }
    if cache_control is not None:
        block["cache_control"] = cache_control
    return block


class FilesApiReferenceError(ValueError):
    """A request for Bedrock refers to a document uploaded to the Anthropic Files API.

    Such blocks come from conversation checkpoints written under the anthropic backend. They
    cannot be rewritten into inline blocks: the checkpoint holds only the file id, and the
    Files API does not return the bytes of a file the client uploaded. So the request fails
    before it is sent."""


def _serialized_size(value: Any) -> int:
    # The compact form the HTTP client sends; ``default=str`` only keeps the estimate
    # total for values the SDK would serialize itself.
    return len(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8"))


def serialized_request_size(payload: dict[str, Any]) -> int:
    """The estimated size in bytes of ``payload`` as the SDK sends it."""
    return _serialized_size(payload)


def _is_block(value: object) -> TypeGuard[dict[str, Any]]:
    """Whether ``value`` is a content block: a JSON object, whose keys are strings."""
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _map_content_blocks(
    payload: dict[str, Any], fn: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """A copy of ``payload`` with ``fn`` applied to every content block of the system prompt
    and of each message, in request order, descending into ``tool_result`` content. Only the
    dicts and lists along the way are copied; ``payload`` itself is left unchanged."""

    def map_list(blocks: list[object]) -> list[object]:
        out: list[object] = []
        for block in blocks:
            if not _is_block(block):
                out.append(block)
                continue
            content = block.get("content")
            if block.get("type") == "tool_result" and _is_list(content):
                out.append({**block, "content": map_list(content)})
            else:
                out.append(fn(block))
        return out

    result = dict(payload)
    if isinstance(system := payload.get("system"), list):
        result["system"] = map_list(system)
    if isinstance(messages := payload.get("messages"), list):
        result["messages"] = [
            {**m, "content": map_list(m["content"])}
            if isinstance(m, dict) and isinstance(m.get("content"), list) else m
            for m in messages
        ]
    return result


def _is_inline_document(block: dict[str, Any]) -> bool:
    source = block.get("source")
    return (
        block.get("type") == "document"
        and isinstance(source, dict) and source.get("type") == "base64"
    )


def _check_no_file_reference(block: dict[str, Any]) -> None:
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "file":
        raise FilesApiReferenceError(
            f"This conversation contains a {block.get('type', 'content')} block that refers to "
            f"Anthropic Files API upload {source.get('file_id')!r}. It was most likely "
            f"checkpointed by a run with {BACKEND_ENV_VAR}=anthropic, and Bedrock cannot "
            f"read Files API uploads. Resume this thread with {BACKEND_ENV_VAR}=anthropic, "
            f"or start a new thread with {BACKEND_ENV_VAR}=bedrock."
        )


type DocumentKey = tuple[str, int, int]
"""Identifies an inline document across the requests of a conversation: its title, and the
length and hash of its base64 body."""


def _document_key(block: dict[str, Any]) -> DocumentKey:
    data = block["source"].get("data")
    body = data if isinstance(data, str) else ""
    return (str(block.get("title") or ""), len(body), hash(body))


def fit_request_to_budget(
    payload: dict[str, Any], max_bytes: int, reported: set[DocumentKey] | None = None
) -> dict[str, Any]:
    """``payload`` with inline documents replaced by text blocks, latest first, until its
    serialized size is at most ``max_bytes``. Returns ``payload`` itself when it already
    fits or carries no inline document; never mutates it.

    Each document left out is logged as a warning. With ``reported``, only a document not yet
    in that set is, and it is added; one already in it is logged at DEBUG. Every request of a
    conversation carries its whole history, so without this a document left out once is
    warned about on every later request.

    Leaving out the latest documents first keeps the earliest ones, which hold the system
    and design documents the whole conversation builds on and sit in the cached prefix.
    If the request is still over budget with every document left out, it is sent that way:
    only documents can be dropped.

    Raises :class:`FilesApiReferenceError` when the payload refers to a Files API upload."""
    documents: list[dict[str, Any]] = []

    def collect(block: dict[str, Any]) -> dict[str, Any]:
        _check_no_file_reference(block)
        if _is_inline_document(block):
            documents.append(block)
        return block

    _map_content_blocks(payload, collect)
    if not documents:
        return payload

    total = _serialized_size(payload)
    if total <= max_bytes:
        return payload

    replacements: dict[int, dict[str, Any]] = {}
    for ordinal in reversed(range(len(documents))):
        if total <= max_bytes:
            break
        document = documents[ordinal]
        log_level = logging.WARNING
        if reported is not None:
            key = _document_key(document)
            if key in reported:
                log_level = logging.DEBUG
            else:
                reported.add(key)
        placeholder = omitted_document_block(
            str(document.get("title") or "untitled document"),
            f"the request would exceed the {max_bytes}-byte request budget for Claude on "
            "Bedrock",
            cache_control=document.get("cache_control"),
            log_level=log_level,
        )
        total -= _serialized_size(document) - _serialized_size(placeholder)
        replacements[ordinal] = placeholder

    seen = 0

    def replace(block: dict[str, Any]) -> dict[str, Any]:
        nonlocal seen
        if not _is_inline_document(block):
            return block
        ordinal = seen
        seen += 1
        return replacements.get(ordinal, block)

    return _map_content_blocks(payload, replace)
