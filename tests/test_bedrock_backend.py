"""The Bedrock backend for Claude, selected by ``AUTOPROVER_LLM_BACKEND``.

Everything runs offline: requests go to an ``httpx.MockTransport`` and are signed with fake
AWS credentials. See composer/llm/bedrock.py and composer/llm/bedrock_chat.py."""

import json
from dataclasses import dataclass
from typing import Any, Callable

import anthropic
import httpx
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage

from graphcore.utils import get_normalized_token_usage

from composer.input.files import InMemoryBytesFile, InMemoryTextFile
from composer.llm import bedrock_chat, openrouter
from composer.llm.anthropic import (
    AnthropicFileUploader, AnthropicModelProvider, AnthropicService, BedrockFileUploader,
    uploader_for_backend,
)
from composer.llm.bedrock import (
    BACKEND_ENV_VAR, is_bedrock_model_id, llm_backend, to_bedrock_model_id,
)
from composer.llm.bedrock_chat import ChatAnthropicBedrock
from composer.llm.registry import get_provider_for


_REGION = "us-west-2"
_MODEL = "claude-opus-5-5"
_FILES_BETA = "files-api-2025-04-14"


@dataclass(frozen=True)
class _Options:
    thinking_tokens: int | None = 16_384
    tokens: int = 128_000
    memory_tool: bool = False
    interleaved_thinking: bool = False


@pytest.fixture
def bedrock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedrock selected, a region set, fake static AWS credentials (so SigV4 signing runs
    without touching a real account), and no Anthropic API key anywhere."""
    monkeypatch.setenv(BACKEND_ENV_VAR, "bedrock")
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    for var in (
        "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _build(**options: Any) -> Any:
    return AnthropicModelProvider.create(_MODEL, _Options(**options)).builder_for()


# --- backend selection --------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "anthropic", " Anthropic "])
def test_default_backend_is_anthropic(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(BACKEND_ENV_VAR, value)
    assert llm_backend() == "anthropic"


def test_invalid_backend_fails_when_the_model_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BACKEND_ENV_VAR, "vertex")
    provider = AnthropicModelProvider.create(_MODEL, _Options())
    with pytest.raises(ValueError, match=f"{BACKEND_ENV_VAR}='vertex'.*anthropic, bedrock"):
        provider.builder_for()


def test_bedrock_without_region_fails_when_the_model_is_built(bedrock_env, monkeypatch) -> None:
    monkeypatch.delenv("AWS_REGION")
    with pytest.raises(ValueError, match="AWS_REGION or AWS_DEFAULT_REGION"):
        _build()


def test_bedrock_region_falls_back_to_default_region(bedrock_env, monkeypatch) -> None:
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    llm = _build()
    assert isinstance(llm, ChatAnthropicBedrock)
    assert llm.aws_region == "eu-central-1"
    assert str(llm._async_client.base_url) == (
        "https://bedrock-mantle.eu-central-1.api.aws/anthropic/"
    )


# --- the Anthropic path is unchanged -------------------------------------------------


@pytest.mark.parametrize("value", [None, "anthropic"])
def test_anthropic_path_builds_plain_chat_anthropic(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(BACKEND_ENV_VAR, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    llm = _build(memory_tool=True)
    assert type(llm) is ChatAnthropic
    assert llm.model == _MODEL
    assert llm.betas == [_FILES_BETA, "context-management-2025-06-27"]
    assert llm.default_request_timeout == 300.0
    assert llm.max_retries == 8
    assert llm.streaming is True
    client = llm._async_client
    assert type(client) is anthropic.AsyncAnthropic
    assert str(client.base_url) == "https://api.anthropic.com"
    assert client.timeout == 300.0
    assert client.max_retries == 8


def test_anthropic_path_keeps_files_beta_without_other_betas(monkeypatch) -> None:
    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    assert _build().betas == [_FILES_BETA]


# --- the Bedrock chat model ----------------------------------------------------------


def test_bedrock_builder_wires_mantle_clients(bedrock_env) -> None:
    llm = _build()
    assert isinstance(llm, ChatAnthropicBedrock)
    # Logical outside the client.
    assert llm.model == _MODEL
    # No Files API on Bedrock, and no other beta asked for: the plain messages endpoint.
    assert llm.betas is None
    for client, cls in (
        (llm._client, anthropic.AnthropicBedrockMantle),
        (llm._async_client, anthropic.AsyncAnthropicBedrockMantle),
    ):
        assert isinstance(client, cls)
        assert client.timeout == 300.0
        assert client.max_retries == 8
        assert str(client.base_url) == f"https://bedrock-mantle.{_REGION}.api.aws/anthropic/"
        assert client.aws_region == _REGION
        assert client.api_key is None
        assert client._custom_headers["User-Agent"].startswith("langchain-anthropic")


def test_bedrock_models_share_one_connection_pool(bedrock_env) -> None:
    first, second = _build(), _build()
    assert first._async_client._client is second._async_client._client
    assert first._client._client is second._client._client


def test_bedrock_client_honours_anthropic_proxy(bedrock_env, monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    real = bedrock_chat._get_default_async_httpx_client

    def recording(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(bedrock_chat, "_get_default_async_httpx_client", recording)
    monkeypatch.setenv("ANTHROPIC_PROXY", "http://proxy.invalid:3128")
    _build()._async_client
    assert seen == [{
        "base_url": f"https://bedrock-mantle.{_REGION}.api.aws",
        "timeout": 300.0,
        "anthropic_proxy": "http://proxy.invalid:3128",
    }]


def test_bedrock_builder_never_sends_files_beta(bedrock_env) -> None:
    llm = _build(memory_tool=True)
    assert llm.betas == ["context-management-2025-06-27"]


def test_bedrock_payload_carries_bedrock_id(bedrock_env) -> None:
    llm = _build()
    payload = llm._get_request_payload("hi")
    assert payload["model"] == "anthropic.claude-opus-5-5"
    assert "betas" not in payload
    assert llm.model == _MODEL


# --- end to end against a mocked Mantle endpoint -----------------------------------


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()


def _stream_body(model: str) -> bytes:
    return _sse([
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"input_tokens": 11, "output_tokens": 7}},
        {"type": "message_stop"},
    ])


def _json_body(model: str) -> bytes:
    return json.dumps({
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }).encode()


@dataclass
class _Recorder:
    requests: list[httpx.Request]
    client_kwargs: list[dict[str, Any]]


def _mock_mantle(
    monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]
) -> _Recorder:
    """Route the async Mantle client through a MockTransport, recording each request and
    the constructor arguments the chat model passed."""
    rec = _Recorder([], [])
    real = bedrock_chat._AsyncMantleClient

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return respond(request)

    def factory(**kwargs: Any) -> anthropic.AsyncAnthropicBedrockMantle:
        rec.client_kwargs.append(kwargs)
        mocked = {**kwargs, "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler))}
        return real(**mocked)

    monkeypatch.setattr(bedrock_chat, "_AsyncMantleClient", factory)
    return rec


def _check_signed_bedrock_request(request: httpx.Request) -> dict[str, Any]:
    assert request.url.host == f"bedrock-mantle.{_REGION}.api.aws"
    assert request.url.path == "/anthropic/v1/messages"
    auth = request.headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert f"/{_REGION}/bedrock-mantle/aws4_request" in auth
    assert "x-api-key" not in request.headers
    assert "anthropic-beta" not in request.headers
    return json.loads(request.content)


@pytest.mark.asyncio
async def test_bedrock_streaming_round_trip(bedrock_env, monkeypatch) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_stream_body("anthropic.claude-opus-5-5"),
    ))
    llm = _build()
    msg = await llm.ainvoke("hi")

    assert len(rec.requests) == 1
    body = _check_signed_bedrock_request(rec.requests[0])
    assert body["model"] == "anthropic.claude-opus-5-5"
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}

    (kwargs,) = rec.client_kwargs
    assert kwargs["timeout"] == 300.0
    assert kwargs["max_retries"] == 8
    assert kwargs["aws_region"] == _REGION
    assert "api_key" not in kwargs

    assert isinstance(msg, AIMessage)
    assert msg.response_metadata["model_name"] == _MODEL
    usage = get_normalized_token_usage(msg)
    assert usage["model_name"] == _MODEL
    assert usage["total_input_tokens"] == 11
    assert usage["total_output_tokens"] == 7


@pytest.mark.asyncio
async def test_bedrock_non_streaming_round_trip(bedrock_env, monkeypatch) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(
        200, json=json.loads(_json_body("us.anthropic.claude-sonnet-5-5")),
    ))
    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name="claude-sonnet-5-5", max_tokens_to_sample=1024,
        streaming=False,
    )
    msg = await llm.ainvoke("hi")
    body = _check_signed_bedrock_request(rec.requests[0])
    assert body["model"] == "anthropic.claude-sonnet-5-5"
    assert isinstance(msg, AIMessage)
    assert msg.content == "hello"
    assert msg.response_metadata["model_name"] == "claude-sonnet-5-5"
    assert msg.response_metadata["model"] == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_bedrock_beta_request_goes_to_beta_endpoint(bedrock_env, monkeypatch) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_stream_body("anthropic.claude-opus-5-5"),
    ))
    llm = _build(memory_tool=True)
    await llm.ainvoke("hi")
    request = rec.requests[0]
    assert request.url.params.get("beta") == "true"
    assert request.headers["anthropic-beta"] == "context-management-2025-06-27"
    assert json.loads(request.content)["model"] == "anthropic.claude-opus-5-5"


# --- uploader --------------------------------------------------------------------


def _forbid_first_party_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a first-party Anthropic client was built in bedrock mode")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", refuse)


def test_bedrock_uploader_builds_no_first_party_client(bedrock_env, monkeypatch) -> None:
    _forbid_first_party_client(monkeypatch)
    assert isinstance(uploader_for_backend(), BedrockFileUploader)
    assert isinstance(AnthropicService().uploader(), BedrockFileUploader)
    with pytest.raises(ValueError, match="anthropic LLM backend"):
        AnthropicFileUploader.lazy()


def test_anthropic_uploader_is_files_api(monkeypatch) -> None:
    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    uploader = AnthropicService().uploader()
    assert isinstance(uploader, AnthropicFileUploader)
    assert isinstance(uploader.client, anthropic.AsyncAnthropic)


@pytest.mark.asyncio
async def test_bedrock_uploader_keeps_text_inline(bedrock_env, tmp_path) -> None:
    doc_path = tmp_path / "design.md"
    doc_path.write_text("# Design\n")
    uploader = BedrockFileUploader()
    doc = await uploader.get_document(doc_path)
    assert isinstance(doc, InMemoryTextFile)
    assert doc.to_dict() == {"type": "text", "text": "# Design\n"}
    uploaded = await uploader.upload_text_file_if_needed(doc_path)
    assert isinstance(uploaded, InMemoryTextFile)


@pytest.mark.asyncio
async def test_bedrock_uploader_inlines_binary(bedrock_env, tmp_path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%\x00\x01")
    doc = await BedrockFileUploader().get_document(pdf)
    assert isinstance(doc, InMemoryBytesFile)
    assert doc.to_dict()["source"]["type"] == "base64"


# --- model ids -------------------------------------------------------------------


@pytest.mark.parametrize("logical", [
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1",
])
def test_model_id_mapping(logical: str) -> None:
    bedrock_id = to_bedrock_model_id(logical)
    assert bedrock_id == f"anthropic.{logical}"
    assert is_bedrock_model_id(bedrock_id)
    assert not is_bedrock_model_id(logical)


# Whatever name Bedrock reports, the response carries the logical id the request was built
# from: the client knows which model it asked for.
_REPORTED_NAMES = [
    "anthropic.claude-opus-5-5",
    "anthropic.claude-opus-5-5-v1:0",
    "us.anthropic.claude-opus-5-5",
    "us.anthropic.claude-opus-5-5-v1:0",
    "global.anthropic.claude-opus-5-5",
    "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.anthropic.claude-opus-5-5-v1:0",
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-5-5-v1:0",
    "claude-opus-5-5",
]


@pytest.mark.parametrize("reported", _REPORTED_NAMES)
@pytest.mark.asyncio
async def test_reported_model_name_is_the_logical_id_when_streaming(
    bedrock_env, monkeypatch, reported: str
) -> None:
    _mock_mantle(monkeypatch, lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_stream_body(reported),
    ))
    msg = await _build().ainvoke("hi")
    assert msg.response_metadata["model_name"] == _MODEL
    assert get_normalized_token_usage(msg)["model_name"] == _MODEL


@pytest.mark.parametrize("reported", _REPORTED_NAMES)
@pytest.mark.asyncio
async def test_reported_model_name_is_the_logical_id(
    bedrock_env, monkeypatch, reported: str
) -> None:
    _mock_mantle(monkeypatch, lambda _: httpx.Response(200, json=json.loads(_json_body(reported))))
    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024, streaming=False,
    )
    msg = await llm.ainvoke("hi")
    assert msg.response_metadata["model_name"] == _MODEL
    assert msg.response_metadata["model"] == _MODEL
    assert get_normalized_token_usage(msg)["model_name"] == _MODEL


@pytest.mark.parametrize("model", [
    "claude-opus-5-5", "anthropic/claude-sonnet-5", "moonshotai/kimi-k2.5", "gpt-5",
    "notanthropic.claude-x",
])
def test_non_bedrock_ids(model: str) -> None:
    assert not is_bedrock_model_id(model)


def test_to_bedrock_rejects_bedrock_id() -> None:
    with pytest.raises(ValueError, match="already a Bedrock model id"):
        to_bedrock_model_id("anthropic.claude-opus-5-5")


# --- registry --------------------------------------------------------------------

_ARN = "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.anthropic.claude-opus-5-5"


def test_logical_id_in_bedrock_mode_resolves_to_anthropic(bedrock_env) -> None:
    provider = get_provider_for(model_name=_MODEL, options=_Options())
    assert isinstance(provider, AnthropicModelProvider)
    assert isinstance(provider.builder_for(), ChatAnthropicBedrock)


@pytest.mark.parametrize("model", ["anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5", _ARN])
def test_bedrock_ids_are_refused_by_the_registry(bedrock_env, model: str) -> None:
    with pytest.raises(ValueError, match="is a Bedrock model id"):
        get_provider_for(model_name=model, options=_Options())


def test_openrouter_does_not_claim_bedrock_arn() -> None:
    assert not openrouter.matches(_ARN.lower())
    assert openrouter.matches("anthropic/claude-sonnet-5")
