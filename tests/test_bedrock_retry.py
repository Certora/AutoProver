"""Claude on Bedrock: the ``max_tokens`` cap, and which failures are retried.

Bedrock admits a request against the token quota by its input plus its ``max_tokens``, so
Bedrock requests carry a capped ``max_tokens``; the Anthropic API path keeps the configured
value. Transient Bedrock failures (throttling, model not ready, 5xx, a model timeout, a
transient error inside a stream, a passing failure to fetch AWS credentials) are retried, by
the SDK and then by ``AnthropicService.should_retry``; deterministic ones (validation, access
denied, unknown model, missing credentials) are not.

Everything runs offline against an ``httpx.MockTransport``. See composer/llm/bedrock.py,
composer/llm/bedrock_chat.py and composer/llm/anthropic.py."""

import json
from typing import Any

import anthropic
import botocore.exceptions
import httpx
import pytest
from langchain_anthropic import ChatAnthropic

from anthropic.lib.bedrock import _mantle
from composer.llm.anthropic import AnthropicModelProvider, AnthropicService
from composer.llm.bedrock import (
    BACKEND_ENV_VAR, DEFAULT_MAX_TOKENS, MAX_TOKENS_ENV_VAR, BedrockCredentialRefreshError,
    BedrockCredentialsError,
)
from composer.llm.bedrock_chat import ChatAnthropicBedrock

from tests.test_bedrock_backend import (
    _MODEL, _REGION, _Options, _build, _mock_mantle, _sse, _stream_body, bedrock_env,
)

__all__ = ["bedrock_env"]  # the fixture, imported for the tests below

_MAX_RETRIES = 2


def _svc() -> AnthropicService:
    return AnthropicService()


@pytest.fixture
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the SDK's retry loop, without its backoff delays."""
    monkeypatch.setattr(
        anthropic.AsyncAnthropicBedrockMantle, "_calculate_retry_timeout",
        lambda self, *args, **kwargs: 0.0,
    )


# --- max_tokens -----------------------------------------------------------------------


def test_bedrock_caps_max_tokens_by_default(bedrock_env) -> None:
    llm = _build()
    assert isinstance(llm, ChatAnthropicBedrock)
    assert llm.max_tokens == DEFAULT_MAX_TOKENS
    assert llm._get_request_payload("hi")["max_tokens"] == DEFAULT_MAX_TOKENS


def test_bedrock_max_tokens_cap_from_env(bedrock_env, monkeypatch) -> None:
    monkeypatch.setenv(MAX_TOKENS_ENV_VAR, "20000")
    assert _build().max_tokens == 20_000


def test_bedrock_keeps_a_smaller_configured_max_tokens(bedrock_env) -> None:
    assert _build(tokens=8_000).max_tokens == 8_000


@pytest.mark.parametrize("value", ["0", "-5", "lots"])
def test_bedrock_max_tokens_cap_must_be_positive(bedrock_env, monkeypatch, value: str) -> None:
    monkeypatch.setenv(MAX_TOKENS_ENV_VAR, value)
    with pytest.raises(ValueError, match=f"{MAX_TOKENS_ENV_VAR}=.*positive number of tokens"):
        _build()


def test_anthropic_path_keeps_configured_max_tokens(monkeypatch) -> None:
    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    monkeypatch.setenv(MAX_TOKENS_ENV_VAR, "1000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    llm = _build()
    assert type(llm) is ChatAnthropic
    assert llm.max_tokens == 128_000


def _build_budgeted(tokens: int = 128_000, interleaved_thinking: bool = False) -> Any:
    # A model before 4.6, which thinks within a fixed budget.
    return AnthropicModelProvider.create(
        "claude-sonnet-4-5",
        _Options(thinking_tokens=16_384, tokens=tokens, interleaved_thinking=interleaved_thinking),
    ).builder_for()


def test_bedrock_thinking_budget_stays_below_the_cap(bedrock_env) -> None:
    payload = _build_budgeted()._get_request_payload("hi")
    assert payload["max_tokens"] == DEFAULT_MAX_TOKENS
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16_384}


def test_bedrock_thinking_budget_over_the_cap_fails_when_built(bedrock_env, monkeypatch) -> None:
    monkeypatch.setenv(MAX_TOKENS_ENV_VAR, "16000")
    with pytest.raises(ValueError, match=f"thinking budget of 16384.*{MAX_TOKENS_ENV_VAR}"):
        _build_budgeted()


def test_bedrock_interleaved_thinking_budget_may_exceed_the_cap(bedrock_env, monkeypatch) -> None:
    # With interleaved thinking the budget spans a whole turn, so it may exceed max_tokens.
    monkeypatch.setenv(MAX_TOKENS_ENV_VAR, "16000")
    llm = _build_budgeted(interleaved_thinking=True)
    payload = llm._get_request_payload("hi")
    assert payload["max_tokens"] == 16_000
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16_384}
    assert "interleaved-thinking-2025-05-14" in llm.betas


# --- HTTP errors ------------------------------------------------------------------------


def _error(status: int, error_type: str, message: str = "...") -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": message}}
    )


async def _invoke_failing(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> tuple[Exception, int]:
    """Send one request to a Mantle endpoint that always answers ``response``; return the
    exception the chat model raised and how many attempts the SDK made."""
    rec = _mock_mantle(monkeypatch, lambda _: response)
    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024,
        max_retries=_MAX_RETRIES, streaming=True,
    )
    with pytest.raises(Exception) as info:
        await llm.ainvoke("hi")
    return info.value, len(rec.requests)


# Bedrock's own error names alongside the Anthropic ones, as Mantle may report either.
@pytest.mark.parametrize("status, error_type", [
    (429, "ThrottlingException"),
    (429, "ModelNotReadyException"),
    (429, "rate_limit_error"),
    (500, "InternalServerException"),
    (503, "ServiceUnavailableException"),
    (408, "ModelTimeoutException"),
    (529, "overloaded_error"),
])
@pytest.mark.asyncio
async def test_transient_bedrock_status_is_retried(
    bedrock_env, monkeypatch, no_retry_sleep, status: int, error_type: str
) -> None:
    exc, attempts = await _invoke_failing(monkeypatch, _error(status, error_type))
    assert isinstance(exc, anthropic.APIStatusError)
    assert exc.status_code == status
    assert attempts == _MAX_RETRIES + 1
    assert _svc().should_retry(exc) is True


@pytest.mark.parametrize("status, error_type, cls", [
    (400, "ValidationException", anthropic.BadRequestError),
    (400, "invalid_request_error", anthropic.BadRequestError),
    (403, "AccessDeniedException", anthropic.PermissionDeniedError),
    (404, "ResourceNotFoundException", anthropic.NotFoundError),
    (424, "ModelErrorException", anthropic.APIStatusError),
])
@pytest.mark.asyncio
async def test_deterministic_bedrock_status_is_not_retried(
    bedrock_env, monkeypatch, no_retry_sleep, status: int, error_type: str, cls: type
) -> None:
    message = f"{error_type} detail"
    exc, attempts = await _invoke_failing(monkeypatch, _error(status, error_type, message))
    assert type(exc) is cls
    assert attempts == 1
    assert _svc().should_retry(exc) is False
    # Bedrock's own explanation reaches the caller.
    assert message in str(exc)


@pytest.mark.asyncio
async def test_expired_token_is_retried_by_the_run(bedrock_env, monkeypatch, no_retry_sleep) -> None:
    # A 403 the SDK does not retry, but a later request, signed with refreshed
    # credentials, does not repeat.
    exc, attempts = await _invoke_failing(monkeypatch, _error(403, "ExpiredTokenException"))
    assert isinstance(exc, anthropic.PermissionDeniedError)
    assert attempts == 1
    assert _svc().should_retry(exc) is True


# --- errors inside a stream -----------------------------------------------------------


def _stream_failing_with(error_type: str) -> httpx.Response:
    body = _sse([
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "anthropic.claude-opus-5-5", "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 11, "output_tokens": 1},
        }},
        {"type": "error", "error": {"type": error_type, "message": "..."}},
    ])
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.mark.parametrize("error_type, transient", [
    ("ThrottlingException", True),
    ("ModelStreamErrorException", True),
    ("InternalServerException", True),
    ("overloaded_error", True),
    ("ValidationException", False),
    ("invalid_request_error", False),
])
@pytest.mark.asyncio
async def test_error_inside_a_stream(
    bedrock_env, monkeypatch, error_type: str, transient: bool
) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: _stream_failing_with(error_type))
    with pytest.raises(anthropic.APIStatusError) as info:
        await _build().ainvoke("hi")
    assert info.value.status_code == 200
    assert len(rec.requests) == 1
    assert _svc().should_retry(info.value) is transient


@pytest.mark.parametrize("error_type", sorted({
    "ThrottlingException", "ModelNotReadyException", "ServiceUnavailableException",
    "InternalServerException", "ModelTimeoutException", "ModelStreamErrorException",
    "ExpiredTokenException",
}))
def test_bedrock_transient_names_on_any_status(error_type: str) -> None:
    request = httpx.Request("POST", f"https://bedrock-mantle.{_REGION}.api.aws/anthropic/v1/messages")
    exc = anthropic.APIStatusError(
        "Error code: 200",
        response=httpx.Response(200, request=request),
        body={"type": "error", "error": {"type": error_type, "message": "..."}},
    )
    assert _svc().should_retry(exc) is True


# --- AWS credentials --------------------------------------------------------------------


def _signing_fails(monkeypatch: pytest.MonkeyPatch, *errors: Exception) -> list[int]:
    """Make SigV4 signing raise ``errors`` in turn, then sign normally. Returns a list that
    counts the signing attempts."""
    real = _mantle.get_auth_headers
    pending = list(errors)
    calls: list[int] = []

    def signer(**kwargs: Any) -> dict[str, str]:
        calls.append(1)
        if pending:
            raise pending.pop(0)
        return real(**kwargs)

    monkeypatch.setattr(_mantle, "get_auth_headers", signer)
    return calls


def _retrieval_error() -> Exception:
    return botocore.exceptions.CredentialRetrievalError(
        provider="container-role", error_msg="timed out"
    )


@pytest.mark.asyncio
async def test_credential_refresh_blip_is_retried_by_the_sdk(
    bedrock_env, monkeypatch, no_retry_sleep
) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_stream_body("anthropic.claude-opus-5-5"),
    ))
    calls = _signing_fails(
        monkeypatch, _retrieval_error(),
        botocore.exceptions.EndpointConnectionError(endpoint_url="http://169.254.170.2"),
    )
    msg = await _build().ainvoke("hi")
    assert msg.content == "hello"
    assert len(calls) == 3
    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_persistent_credential_refresh_failure_is_retried_by_the_run(
    bedrock_env, monkeypatch, no_retry_sleep
) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(500))
    calls = _signing_fails(monkeypatch, *(_retrieval_error() for _ in range(_MAX_RETRIES + 1)))
    llm = ChatAnthropicBedrock(
        aws_region=_REGION, model_name=_MODEL, max_tokens_to_sample=1024,
        max_retries=_MAX_RETRIES, streaming=True,
    )
    with pytest.raises(BedrockCredentialRefreshError, match="timed out") as info:
        await llm.ainvoke("hi")
    assert isinstance(info.value.__cause__, botocore.exceptions.CredentialRetrievalError)
    assert len(calls) == _MAX_RETRIES + 1
    assert rec.requests == []
    assert _svc().should_retry(info.value) is True


@pytest.mark.parametrize("error", [
    botocore.exceptions.NoCredentialsError(),
    botocore.exceptions.PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY"),
    botocore.exceptions.ProfileNotFound(profile="missing"),
])
@pytest.mark.asyncio
async def test_missing_credentials_are_not_retried(
    bedrock_env, monkeypatch, no_retry_sleep, error: Exception
) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(500))
    calls = _signing_fails(monkeypatch, error)
    with pytest.raises(BedrockCredentialsError, match="default credential chain") as info:
        await _build().ainvoke("hi")
    assert info.value.__cause__ is error
    assert len(calls) == 1
    assert rec.requests == []
    assert _svc().should_retry(info.value) is False


def test_credential_refresh_blip_is_retried_by_the_sync_client(
    bedrock_env, monkeypatch
) -> None:
    monkeypatch.setattr(
        anthropic.AnthropicBedrockMantle, "_calculate_retry_timeout",
        lambda self, *args, **kwargs: 0.0,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_stream_body("anthropic.claude-opus-5-5"),
        )

    real = anthropic.AnthropicBedrockMantle

    def factory(**kwargs: Any) -> anthropic.AnthropicBedrockMantle:
        return real(**{**kwargs, "http_client": httpx.Client(transport=httpx.MockTransport(respond))})

    monkeypatch.setattr(anthropic, "AnthropicBedrockMantle", factory)
    calls = _signing_fails(monkeypatch, _retrieval_error())
    msg = _build().invoke("hi")
    assert msg.content == "hello"
    assert len(calls) == 2
    assert len(requests) == 1
    assert json.loads(requests[0].content)["model"] == "anthropic.claude-opus-5-5"
