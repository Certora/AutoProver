"""Inline documents for Claude on Bedrock: how they are rendered, the errors for one that
cannot be sent, and how they reach InvokeModel.

Offline: requests go to an ``httpx.MockTransport``."""

import base64
import json
from typing import Any

import httpx
import pytest
from langchain_aws import ChatAnthropicBedrock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from composer.input.files import InMemoryBytesFile
from composer.llm.anthropic import (
    AnthropicRenderer, BedrockFileUploader, BedrockRenderer,
)
from composer.llm.bedrock import (
    BACKEND_ENV_VAR, MAX_DOCUMENT_BYTES, BedrockDocumentError, base64_size,
)
from composer.llm.types import CacheLevel
from tests.test_bedrock_backend import _EVENT_STREAM, _mock_bedrock, _stream_body
from composer.spec.source.design_doc_finder import read_document_tool

_REGION = "us-west-2"
_MODEL = "claude-opus-5-5"
_LONG_CACHE = {"type": "ephemeral", "ttl": "1h"}


class _Options:
    thinking_tokens: int | None = None
    tokens: int = 1024
    memory_tool: bool = False
    interleaved_thinking: bool = False


@pytest.fixture
def bedrock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BACKEND_ENV_VAR, "bedrock")
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    for var in (
        "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_API_KEY", "AUTOPROVER_BEDROCK_INFERENCE_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)


def _pdf(size: int) -> bytes:
    head = b"%PDF-1.4\n"
    return head + b"x" * (size - len(head))


# --- configuration -----------------------------------------------------------------


def test_document_limit_leaves_room_under_the_request_cap(bedrock_env) -> None:
    # Bedrock rejects a request over 20 MB, and every request of a conversation carries
    # the document.
    assert MAX_DOCUMENT_BYTES < 20_000_000
    assert BedrockRenderer().max_document_bytes == MAX_DOCUMENT_BYTES


def test_anthropic_renderer_does_not_inline() -> None:
    """On the Anthropic API binary documents go through the Files API, so only the Bedrock
    renderer inlines them, with its checks."""
    with pytest.raises(NotImplementedError):
        AnthropicRenderer().inline_file_block("a.pdf", _pdf(100), "application/pdf")


# --- rendering ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_renders_as_inline_document_with_cache_control(bedrock_env, tmp_path) -> None:
    contents = _pdf(100)
    (tmp_path / "design.pdf").write_bytes(contents)
    doc = await BedrockFileUploader().get_document(tmp_path / "design.pdf")
    assert isinstance(doc, InMemoryBytesFile)
    assert doc.to_dict(CacheLevel.LONG) == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(contents).decode("ascii"),
        },
        "title": "design.pdf",
        "cache_control": _LONG_CACHE,
    }
    assert "cache_control" not in doc.to_dict()


@pytest.mark.parametrize("raw_size", [1, 2, 3, 4, 299, 300, 301])
def test_base64_size_matches_encoding(raw_size: int) -> None:
    assert base64_size(raw_size) == len(base64.b64encode(b"\0" * raw_size))


def test_document_at_the_limit_is_inlined_and_one_byte_over_raises() -> None:
    contents = _pdf(300)  # 400 bytes as base64
    at_limit = BedrockRenderer(max_document_bytes=400)
    assert at_limit.inline_file_block(
        "spec.pdf", contents, "application/pdf"
    )["type"] == "document"

    over = BedrockRenderer(max_document_bytes=399)
    with pytest.raises(BedrockDocumentError, match=r"'spec\.pdf'.*399-byte limit"):
        over.inline_file_block("spec.pdf", contents, "application/pdf")


def test_unsupported_binary_type_raises() -> None:
    renderer = BedrockRenderer(max_document_bytes=10_000)
    with pytest.raises(BedrockDocumentError, match=r"'blob\.bin'.*application/octet-stream"):
        renderer.inline_file_block("blob.bin", b"\0\1\2", "application/octet-stream")


@pytest.mark.asyncio
async def test_oversized_document_keeps_its_bytes_and_raises_when_rendered(
    bedrock_env, tmp_path
) -> None:
    """The limit applies when the document is rendered; the document itself, which digests
    and audit records read, keeps the real bytes."""
    contents = _pdf(100)
    (tmp_path / "big.pdf").write_bytes(contents)
    uploader = BedrockFileUploader(renderer=BedrockRenderer(max_document_bytes=10))
    doc = await uploader.get_document(tmp_path / "big.pdf")
    assert doc is not None
    assert doc.bytes_contents == contents
    with pytest.raises(BedrockDocumentError):
        doc.to_dict()


# --- end to end against a mocked InvokeModel endpoint --------------------------------


async def _read_document_message(tmp_path, name: str) -> ToolMessage:
    """The ToolMessage the design-doc finder's ``read_document`` tool produces for ``name``
    under the Bedrock uploader."""
    tool = read_document_tool(BedrockFileUploader(), str(tmp_path))
    msg = await tool.ainvoke(
        {"name": "read_document", "args": {"path": name}, "id": "t1", "type": "tool_call"}
    )
    assert isinstance(msg, ToolMessage)
    return msg


def _conversation(system_doc: dict[str, Any], tool_msg: ToolMessage) -> list[Any]:
    return [
        SystemMessage(content=[{"type": "text", "text": "You are a verifier."}, system_doc]),
        HumanMessage(content="Find the design document."),
        AIMessage(content="", tool_calls=[
            {"name": "read_document", "args": {"path": "spec.pdf"}, "id": "t1",
             "type": "tool_call"},
        ]),
        tool_msg,
    ]


@pytest.mark.asyncio
async def test_documents_reach_bedrock_inline_including_inside_tool_results(
    bedrock_env, monkeypatch, tmp_path
) -> None:
    rec = _mock_bedrock(monkeypatch, lambda _: httpx.Response(
        200, headers=_EVENT_STREAM, content=_stream_body(_MODEL),
    ))
    (tmp_path / "design.pdf").write_bytes(_pdf(200))
    (tmp_path / "spec.pdf").write_bytes(_pdf(300))
    uploader = BedrockFileUploader()
    design = await uploader.get_document(tmp_path / "design.pdf")
    assert design is not None
    tool_msg = await _read_document_message(tmp_path, "spec.pdf")

    llm = ChatAnthropicBedrock(
        region_name=_REGION, model=f"global.anthropic.{_MODEL}",
        max_tokens_to_sample=1024, streaming=True)
    await llm.ainvoke(_conversation(design.to_dict(CacheLevel.LONG), tool_msg))

    body = json.loads(rec.requests[0].content)
    system_doc = body["system"][1]
    assert system_doc["type"] == "document"
    assert system_doc["source"]["type"] == "base64"
    assert system_doc["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(system_doc["source"]["data"]) == _pdf(200)
    assert system_doc["cache_control"] == _LONG_CACHE

    (tool_result,) = body["messages"][-1]["content"]
    assert tool_result["type"] == "tool_result"
    nested = tool_result["content"][-1]
    assert nested["type"] == "document"
    assert nested["source"]["type"] == "base64"
    assert nested["title"] == "spec.pdf"
    assert base64.b64decode(nested["source"]["data"]) == _pdf(300)
