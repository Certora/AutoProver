"""Inline documents for Claude on Bedrock and the byte budgets around them.

Offline: requests go to an ``httpx.MockTransport``. See composer/llm/bedrock_inline.py."""

import base64
import copy
import json
import logging
from typing import Any

import anthropic
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from composer.llm import bedrock_chat
from composer.input.files import InMemoryBytesFile
from composer.llm.anthropic import (
    AnthropicModelProvider, BedrockFileUploader, BedrockRenderer,
)
from composer.llm.bedrock import (
    BACKEND_ENV_VAR, DEFAULT_MAX_DOCUMENT_BYTES, DEFAULT_MAX_REQUEST_BYTES,
    MAX_DOCUMENT_BYTES_ENV_VAR, MAX_REQUEST_BYTES_ENV_VAR, max_document_bytes,
    max_request_bytes,
)
from composer.llm.bedrock_chat import ChatAnthropicBedrock
from composer.llm.bedrock_inline import (
    FilesApiReferenceError, base64_size, fit_request_to_budget,
)
from composer.llm.types import CacheLevel
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
        "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_API_KEY",
        MAX_DOCUMENT_BYTES_ENV_VAR, MAX_REQUEST_BYTES_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def _pdf(size: int) -> bytes:
    head = b"%PDF-1.4\n"
    return head + b"x" * (size - len(head))


def _inline(name: str, size: int, *, cache: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "document",
        "source": {
            "type": "base64", "media_type": "application/pdf",
            "data": base64.b64encode(_pdf(size)).decode("ascii"),
        },
        "title": name,
    }
    if cache:
        block["cache_control"] = _LONG_CACHE
    return block


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


# --- configuration -----------------------------------------------------------------


def test_budgets_default(bedrock_env) -> None:
    assert max_document_bytes() == DEFAULT_MAX_DOCUMENT_BYTES
    assert max_request_bytes() == DEFAULT_MAX_REQUEST_BYTES
    # The request budget leaves room under Bedrock's 20 MB request cap.
    assert DEFAULT_MAX_DOCUMENT_BYTES < DEFAULT_MAX_REQUEST_BYTES < 20_000_000


def test_budgets_read_from_env(bedrock_env, monkeypatch) -> None:
    monkeypatch.setenv(MAX_DOCUMENT_BYTES_ENV_VAR, "1000")
    monkeypatch.setenv(MAX_REQUEST_BYTES_ENV_VAR, " 5000 ")
    assert max_document_bytes() == 1000
    assert max_request_bytes() == 5000
    assert BedrockRenderer().max_document_bytes == 1000


@pytest.mark.parametrize("value", ["0", "-5", "lots", "1.5"])
def test_malformed_request_budget_fails_when_the_model_is_built(
    bedrock_env, monkeypatch, value: str
) -> None:
    monkeypatch.setenv(MAX_REQUEST_BYTES_ENV_VAR, value)
    provider = AnthropicModelProvider.create(_MODEL, _Options())
    with pytest.raises(ValueError, match=MAX_REQUEST_BYTES_ENV_VAR):
        provider.builder_for()


def test_malformed_document_budget_fails_when_the_uploader_is_built(
    bedrock_env, monkeypatch
) -> None:
    monkeypatch.setenv(MAX_DOCUMENT_BYTES_ENV_VAR, "0")
    with pytest.raises(ValueError, match=MAX_DOCUMENT_BYTES_ENV_VAR):
        BedrockFileUploader()


def test_builder_passes_request_budget(bedrock_env, monkeypatch) -> None:
    monkeypatch.setenv(MAX_REQUEST_BYTES_ENV_VAR, "12345")
    llm = AnthropicModelProvider.create(_MODEL, _Options()).builder_for()
    assert isinstance(llm, ChatAnthropicBedrock)
    assert llm.max_request_bytes == 12345


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


def test_document_at_budget_is_inlined_and_one_byte_over_is_omitted(caplog) -> None:
    contents = _pdf(300)  # 400 bytes as base64
    at_budget = BedrockRenderer(max_document_bytes=400)
    assert at_budget.inline_file_block(
        "spec.pdf", contents, "application/pdf"
    )["type"] == "document"

    over = BedrockRenderer(max_document_bytes=399)
    with caplog.at_level(logging.WARNING, logger="composer.llm.bedrock_inline"):
        block = over.inline_file_block(
            "spec.pdf", contents, "application/pdf", cache_level=CacheLevel.LONG
        )
    assert block["type"] == "text"
    assert "'spec.pdf'" in block["text"]
    assert "399-byte per-document budget" in block["text"]
    assert block["cache_control"] == _LONG_CACHE
    assert any("spec.pdf" in r.getMessage() for r in caplog.records)


def test_unsupported_binary_type_is_omitted_not_sent() -> None:
    block = BedrockRenderer(max_document_bytes=10_000).inline_file_block(
        "blob.bin", b"\0\1\2", "application/octet-stream"
    )
    assert block["type"] == "text"
    assert "'blob.bin'" in block["text"]
    assert "application/octet-stream" in block["text"]
    assert "cache_control" not in block


@pytest.mark.asyncio
async def test_oversized_document_keeps_its_bytes(bedrock_env, monkeypatch, tmp_path) -> None:
    """The budget applies when the document is rendered; the document itself, which digests
    and audit records read, keeps the real bytes."""
    monkeypatch.setenv(MAX_DOCUMENT_BYTES_ENV_VAR, "10")
    contents = _pdf(100)
    (tmp_path / "big.pdf").write_bytes(contents)
    doc = await BedrockFileUploader().get_document(tmp_path / "big.pdf")
    assert doc is not None
    assert doc.bytes_contents == contents
    assert doc.to_dict()["type"] == "text"


# --- request budget ----------------------------------------------------------------


def _payload(*system_docs: dict[str, Any], tool_docs: tuple[dict[str, Any], ...] = ()) -> dict:
    return {
        "model": "anthropic.claude-opus-5-5",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "You are a verifier."}, *system_docs],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "start"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read_document", "input": {}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "t1", "is_error": False,
                "content": [{"type": "text", "text": "Contents:"}, *tool_docs],
            }]},
        ],
    }


def test_payload_within_budget_is_returned_unchanged() -> None:
    payload = _payload(_inline("a.pdf", 300), tool_docs=(_inline("b.pdf", 300),))
    assert fit_request_to_budget(payload, _size(payload)) is payload


def test_payload_one_byte_over_drops_the_latest_document(caplog) -> None:
    first = _inline("a.pdf", 300, cache=True)
    last = _inline("b.pdf", 300)
    payload = _payload(first, tool_docs=(last,))
    before = copy.deepcopy(payload)

    with caplog.at_level(logging.WARNING, logger="composer.llm.bedrock_inline"):
        fitted = fit_request_to_budget(payload, _size(payload) - 1)

    assert payload == before, "the input payload must not be mutated"
    assert fitted["system"][1] == first
    tool_result = fitted["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "t1"
    placeholder = tool_result["content"][1]
    assert placeholder["type"] == "text"
    assert "'b.pdf'" in placeholder["text"]
    assert "request budget" in placeholder["text"]
    assert _size(fitted) <= _size(payload) - 1
    assert sum("b.pdf" in r.getMessage() for r in caplog.records) == 1


def test_drops_documents_latest_first_until_it_fits() -> None:
    a, b, c = _inline("a.pdf", 3000, cache=True), _inline("b.pdf", 3000), _inline("c.pdf", 3000)
    payload = _payload(a, b, tool_docs=(c,))
    # Room for exactly one document.
    without_two = _size(payload) - 2 * len(json.dumps(b, separators=(",", ":"))) + 1000
    fitted = fit_request_to_budget(payload, without_two)
    assert fitted["system"][1] == a
    assert fitted["system"][2]["type"] == "text" and "'b.pdf'" in fitted["system"][2]["text"]
    assert fitted["messages"][2]["content"][0]["content"][1]["type"] == "text"
    assert _size(fitted) <= without_two


def test_placeholder_keeps_the_cache_breakpoint() -> None:
    payload = _payload(_inline("a.pdf", 3000, cache=True))
    fitted = fit_request_to_budget(payload, 500)
    assert fitted["system"][1]["type"] == "text"
    assert fitted["system"][1]["cache_control"] == _LONG_CACHE


def test_over_budget_without_documents_is_sent_as_is() -> None:
    payload = _payload()
    assert fit_request_to_budget(payload, 10) is payload


def test_everything_dropped_when_text_alone_is_over_budget() -> None:
    payload = _payload(_inline("a.pdf", 300), tool_docs=(_inline("b.pdf", 300),))
    fitted = fit_request_to_budget(payload, 10)
    assert fitted["system"][1]["type"] == "text"
    assert fitted["messages"][2]["content"][0]["content"][1]["type"] == "text"


def _omission_logs(caplog, name: str) -> list[int]:
    """The levels of the records that report leaving ``name`` out."""
    return [r.levelno for r in caplog.records if f"{name!r} out of" in r.getMessage()]


def test_omitted_document_is_warned_about_once_per_reported_set(caplog) -> None:
    payload = _payload(_inline("a.pdf", 3000), tool_docs=(_inline("b.pdf", 3000),))
    reported: set = set()
    with caplog.at_level(logging.DEBUG, logger="composer.llm.bedrock_inline"):
        for _ in range(3):
            # A fresh copy each time, as each request rebuilds its payload.
            fit_request_to_budget(copy.deepcopy(payload), 500, reported)
    assert _omission_logs(caplog, "a.pdf") == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    assert _omission_logs(caplog, "b.pdf") == [logging.WARNING, logging.DEBUG, logging.DEBUG]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="composer.llm.bedrock_inline"):
        # A new set, as a new chat model has, warns again.
        fit_request_to_budget(payload, 500, set())
        # Without a set, every omission is a warning.
        fit_request_to_budget(payload, 500)
    assert _omission_logs(caplog, "a.pdf") == [logging.WARNING, logging.WARNING]


def test_documents_with_the_same_title_are_reported_separately(caplog) -> None:
    reported: set = set()
    with caplog.at_level(logging.DEBUG, logger="composer.llm.bedrock_inline"):
        fit_request_to_budget(_payload(_inline("a.pdf", 3000)), 500, reported)
        fit_request_to_budget(_payload(_inline("a.pdf", 4000)), 500, reported)
    assert _omission_logs(caplog, "a.pdf") == [logging.WARNING, logging.WARNING]


def test_same_block_object_twice_is_counted_per_position() -> None:
    """The ordinal, not object identity, picks what to drop: a block object that the
    conversation holds twice is dropped only where the budget needs it."""
    shared = _inline("a.pdf", 3000)
    payload = _payload(shared, tool_docs=(shared,))
    fitted = fit_request_to_budget(payload, _size(payload) - 1)
    assert fitted["system"][1] is shared
    assert fitted["messages"][2]["content"][0]["content"][1]["type"] == "text"


# --- Files API references from checkpoints -----------------------------------------


_FILE_BLOCK = {"type": "document", "source": {"type": "file", "file_id": "file_011abc"}}


@pytest.mark.parametrize("where", ["system", "message", "tool_result"])
def test_files_api_reference_fails_with_a_clear_error(where: str) -> None:
    payload = _payload()
    match where:
        case "system":
            payload["system"].append(_FILE_BLOCK)
        case "message":
            payload["messages"][0]["content"].append(_FILE_BLOCK)
        case "tool_result":
            payload["messages"][2]["content"][0]["content"].append(_FILE_BLOCK)
    with pytest.raises(FilesApiReferenceError, match=rf"file_011abc.*{BACKEND_ENV_VAR}=anthropic"):
        fit_request_to_budget(payload, 10**9)


# --- end to end against a mocked Mantle endpoint -----------------------------------


def _sse_body() -> bytes:
    events = [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "anthropic." + _MODEL,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"input_tokens": 11, "output_tokens": 2}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _mock_mantle(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    real = bedrock_chat._AsyncMantleClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse_body()
        )

    def factory(**kwargs: Any) -> anthropic.AsyncAnthropicBedrockMantle:
        return real(**{
            **kwargs, "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        })

    monkeypatch.setattr(bedrock_chat, "_AsyncMantleClient", factory)
    return requests


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
    requests = _mock_mantle(monkeypatch)
    (tmp_path / "design.pdf").write_bytes(_pdf(200))
    (tmp_path / "spec.pdf").write_bytes(_pdf(300))
    uploader = BedrockFileUploader()
    design = await uploader.get_document(tmp_path / "design.pdf")
    assert design is not None
    tool_msg = await _read_document_message(tmp_path, "spec.pdf")

    llm = ChatAnthropicBedrock(aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024,
                               streaming=True)
    await llm.ainvoke(_conversation(design.to_dict(CacheLevel.LONG), tool_msg))

    body = json.loads(requests[0].content)
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


@pytest.mark.asyncio
async def test_request_over_budget_reaches_bedrock_with_document_omitted(
    bedrock_env, monkeypatch, tmp_path
) -> None:
    requests = _mock_mantle(monkeypatch)
    (tmp_path / "design.pdf").write_bytes(_pdf(2000))
    (tmp_path / "spec.pdf").write_bytes(_pdf(30_000))
    design = await BedrockFileUploader().get_document(tmp_path / "design.pdf")
    assert design is not None
    tool_msg = await _read_document_message(tmp_path, "spec.pdf")

    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024,
        max_request_bytes=20_000, streaming=True,
    )
    await llm.ainvoke(_conversation(design.to_dict(CacheLevel.LONG), tool_msg))

    sent = requests[0].content
    assert len(sent) <= 20_000
    body = json.loads(sent)
    assert body["system"][1]["type"] == "document"
    (tool_result,) = body["messages"][-1]["content"]
    assert tool_result["content"][-1]["type"] == "text"
    assert "'spec.pdf'" in tool_result["content"][-1]["text"]


@pytest.mark.asyncio
async def test_chat_model_warns_once_about_a_document_every_request_omits(
    bedrock_env, monkeypatch, tmp_path, caplog
) -> None:
    _mock_mantle(monkeypatch)
    (tmp_path / "spec.pdf").write_bytes(_pdf(30_000))
    tool_msg = await _read_document_message(tmp_path, "spec.pdf")
    system = {"type": "text", "text": "You are a verifier."}

    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024,
        max_request_bytes=20_000, streaming=True,
    )
    with caplog.at_level(logging.DEBUG, logger="composer.llm.bedrock_inline"):
        for _ in range(3):
            await llm.ainvoke(_conversation(system, tool_msg))
    assert _omission_logs(caplog, "spec.pdf") == [
        logging.WARNING, logging.DEBUG, logging.DEBUG,
    ]


@pytest.mark.asyncio
async def test_resumed_checkpoint_with_file_id_fails_before_sending(
    bedrock_env, monkeypatch
) -> None:
    requests = _mock_mantle(monkeypatch)
    llm = ChatAnthropicBedrock(aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024,
                               streaming=True)
    with pytest.raises(FilesApiReferenceError, match="file_011abc"):
        await llm.ainvoke([HumanMessage(content=[
            {"type": "text", "text": "Design document:"}, _FILE_BLOCK,
        ])])
    assert requests == []


def _mock_mantle_with_cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> list[httpx.Request]:
    """Like :func:`_mock_mantle`, but answers 413 to a request whose body exceeds ``cap``."""
    requests: list[httpx.Request] = []
    real = bedrock_chat._AsyncMantleClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(request.content) > cap:
            return httpx.Response(413, json={
                "type": "error",
                "error": {"type": "request_too_large", "message": "Request exceeds the maximum size"},
            })
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse_body()
        )

    def factory(**kwargs: Any) -> anthropic.AsyncAnthropicBedrockMantle:
        return real(**{
            **kwargs, "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        })

    monkeypatch.setattr(bedrock_chat, "_AsyncMantleClient", factory)
    return requests


@pytest.mark.asyncio
async def test_request_rejected_as_too_large_is_sent_again_with_fewer_documents(
    bedrock_env, monkeypatch, tmp_path, caplog
) -> None:
    # The size estimate says the request fits, but Bedrock's real limit is lower.
    requests = _mock_mantle_with_cap(monkeypatch, cap=20_000)
    (tmp_path / "design.pdf").write_bytes(_pdf(2000))
    (tmp_path / "spec.pdf").write_bytes(_pdf(30_000))
    design = await BedrockFileUploader().get_document(tmp_path / "design.pdf")
    assert design is not None
    tool_msg = await _read_document_message(tmp_path, "spec.pdf")
    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024,
        max_request_bytes=1_000_000, streaming=True,
    )
    conversation = _conversation(design.to_dict(CacheLevel.LONG), tool_msg)

    with caplog.at_level(logging.WARNING):
        msg = await llm.ainvoke(conversation)
    assert msg.content == "ok"
    assert [len(r.content) > 20_000 for r in requests] == [True, False]
    body = json.loads(requests[1].content)
    assert body["system"][1]["type"] == "document"
    (tool_result,) = body["messages"][-1]["content"]
    assert "'spec.pdf'" in tool_result["content"][-1]["text"]
    assert any("too large" in r.getMessage() for r in caplog.records)

    # The lowered budget stays, so the next request of the conversation fits at once.
    await llm.ainvoke(conversation)
    assert len(requests) == 3
    assert len(requests[2].content) <= 20_000


@pytest.mark.asyncio
async def test_request_too_large_without_documents_is_raised(bedrock_env, monkeypatch) -> None:
    requests = _mock_mantle_with_cap(monkeypatch, cap=10)
    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024, streaming=True,
    )
    with pytest.raises(anthropic.RequestTooLargeError):
        await llm.ainvoke("hi")
    assert len(requests) == 1
