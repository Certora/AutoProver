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

from anthropic.lib.aws import _auth
from anthropic.lib.bedrock import _mantle
from composer.llm import bedrock_chat
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
        bedrock_chat._AsyncMantleClient, "_calculate_retry_timeout",
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


def _header_error(status: int, header: str, body: Any) -> httpx.Response:
    return httpx.Response(status, headers={"x-amzn-errortype": header}, json=body)


@pytest.mark.parametrize("header", [
    "ExpiredTokenException",
    "ExpiredTokenException:http://example.com/errors/",
])
@pytest.mark.parametrize("body", [
    {"message": "The security token included in the request is expired"},
    {"Message": "The security token included in the request is expired"},
])
@pytest.mark.asyncio
async def test_expired_token_named_in_header_is_retried_by_the_run(
    bedrock_env, monkeypatch, no_retry_sleep, header: str, body: dict[str, str]
) -> None:
    exc, attempts = await _invoke_failing(monkeypatch, _header_error(403, header, body))
    assert isinstance(exc, anthropic.PermissionDeniedError)
    assert attempts == 1
    assert _svc().should_retry(exc) is True


@pytest.mark.parametrize("header, transient", [
    ("ThrottlingException", True),
    ("ThrottlingException:http://example.com/errors/", True),
    ("AccessDeniedException", False),
    ("AccessDeniedException:http://example.com/errors/", False),
    ("", False),
])
def test_header_error_type_on_a_body_without_one(header: str, transient: bool) -> None:
    request = httpx.Request("POST", f"https://bedrock-mantle.{_REGION}.api.aws/anthropic/v1/messages")
    exc = anthropic.APIStatusError(
        "Error code: 400",
        response=httpx.Response(400, request=request, headers={"x-amzn-errortype": header}),
        body={"message": "..."},
    )
    assert _svc().should_retry(exc) is transient


def test_transient_header_counts_next_to_a_payload_error_type() -> None:
    # A front end can pair a generic payload type with the specific exception in the header.
    request = httpx.Request("POST", f"https://bedrock-mantle.{_REGION}.api.aws/anthropic/v1/messages")
    exc = anthropic.APIStatusError(
        "Error code: 403",
        response=httpx.Response(
            403, request=request, headers={"x-amzn-errortype": "ExpiredTokenException"},
        ),
        body={"type": "error", "error": {"type": "permission_error", "message": "..."}},
    )
    assert _svc().should_retry(exc) is True


@pytest.mark.asyncio
async def test_expired_token_in_header_with_anthropic_body_is_retried_by_the_run(
    bedrock_env, monkeypatch, no_retry_sleep
) -> None:
    response = _header_error(
        403, "ExpiredTokenException",
        {"type": "error", "error": {"type": "permission_error", "message": "expired"}},
    )
    exc, attempts = await _invoke_failing(monkeypatch, response)
    assert isinstance(exc, anthropic.PermissionDeniedError)
    assert attempts == 1
    assert _svc().should_retry(exc) is True


def test_deterministic_payload_and_header_are_not_retried() -> None:
    request = httpx.Request("POST", f"https://bedrock-mantle.{_REGION}.api.aws/anthropic/v1/messages")
    exc = anthropic.APIStatusError(
        "Error code: 403",
        response=httpx.Response(
            403, request=request, headers={"x-amzn-errortype": "AccessDeniedException"},
        ),
        body={"type": "error", "error": {"type": "permission_error", "message": "..."}},
    )
    assert _svc().should_retry(exc) is False


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


@pytest.fixture
def empty_credential_chain(bedrock_env, monkeypatch, tmp_path) -> Any:
    """A default AWS credential chain that finds nothing: no credential variables, config
    and credentials files that do not exist, and no container or instance role. The SDK
    caches its boto3 session, which would keep credentials an earlier test resolved, so
    its cache is cleared on the way in and out."""
    for var in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    _auth._get_session.cache_clear()
    yield
    _auth._get_session.cache_clear()


def _count_signing(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the calls to the SDK's real signer."""
    real = _mantle.get_auth_headers
    calls: list[int] = []

    def signer(**kwargs: Any) -> dict[str, str]:
        calls.append(1)
        return real(**kwargs)

    monkeypatch.setattr(_mantle, "get_auth_headers", signer)
    return calls


@pytest.mark.asyncio
async def test_no_credentials_are_not_retried(
    empty_credential_chain, monkeypatch, no_retry_sleep
) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(500))
    calls = _count_signing(monkeypatch)
    with pytest.raises(BedrockCredentialsError, match="has no credentials") as info:
        await _build().ainvoke("hi")
    # The SDK's own failure, kept as the cause.
    assert isinstance(info.value.__cause__, RuntimeError)
    assert len(calls) == 1
    assert rec.requests == []
    assert _svc().should_retry(info.value) is False


def test_no_credentials_are_not_retried_by_the_sync_client(
    empty_credential_chain, monkeypatch
) -> None:
    requests: list[httpx.Request] = []
    real = bedrock_chat._MantleClient

    def factory(**kwargs: Any) -> anthropic.AnthropicBedrockMantle:
        transport = httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(500))
        return real(**{**kwargs, "http_client": httpx.Client(transport=transport)})

    monkeypatch.setattr(bedrock_chat, "_MantleClient", factory)
    calls = _count_signing(monkeypatch)
    with pytest.raises(BedrockCredentialsError, match="has no credentials"):
        _build().invoke("hi")
    assert len(calls) == 1
    assert requests == []


@pytest.mark.parametrize("env, cause", [
    ({"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE"}, botocore.exceptions.PartialCredentialsError),
    ({"AWS_PROFILE": "missing"}, botocore.exceptions.ProfileNotFound),
])
@pytest.mark.asyncio
async def test_unusable_credentials_are_not_retried(
    empty_credential_chain, monkeypatch, no_retry_sleep, env: dict[str, str], cause: type
) -> None:
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(500))
    calls = _count_signing(monkeypatch)
    with pytest.raises(BedrockCredentialsError, match="default credential chain") as info:
        await _build().ainvoke("hi")
    assert isinstance(info.value.__cause__, cause)
    assert len(calls) == 1
    assert rec.requests == []
    assert _svc().should_retry(info.value) is False


@pytest.mark.asyncio
async def test_unrelated_runtime_error_passes_through(
    bedrock_env, monkeypatch, no_retry_sleep
) -> None:
    # With credentials in the chain, a RuntimeError from the attempt is not about them.
    _mock_mantle(monkeypatch, lambda _: httpx.Response(500))
    error = RuntimeError("something else")
    _signing_fails(monkeypatch, error)
    with pytest.raises(RuntimeError) as info:
        await _build().ainvoke("hi")
    assert info.value is error


def test_credential_refresh_blip_is_retried_by_the_sync_client(
    bedrock_env, monkeypatch
) -> None:
    monkeypatch.setattr(
        bedrock_chat._MantleClient, "_calculate_retry_timeout",
        lambda self, *args, **kwargs: 0.0,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_stream_body("anthropic.claude-opus-5-5"),
        )

    real = bedrock_chat._MantleClient

    def factory(**kwargs: Any) -> anthropic.AnthropicBedrockMantle:
        return real(**{**kwargs, "http_client": httpx.Client(transport=httpx.MockTransport(respond))})

    monkeypatch.setattr(bedrock_chat, "_MantleClient", factory)
    calls = _signing_fails(monkeypatch, _retrieval_error())
    msg = _build().invoke("hi")
    assert msg.content == "hello"
    assert len(calls) == 2
    assert len(requests) == 1
    assert json.loads(requests[0].content)["model"] == "anthropic.claude-opus-5-5"


# --- SDK backoff ------------------------------------------------------------------------


def test_bedrock_backoff_doubles_up_to_a_minute() -> None:
    from composer.llm.bedrock import retry_delay_seconds
    assert [retry_delay_seconds(n) for n in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]


def _retry_waits(client: Any, response_headers: httpx.Headers | None = None) -> list[float]:
    from anthropic._models import FinalRequestOptions
    options = FinalRequestOptions(method="post", url="/v1/messages")
    retries = client.max_retries
    return [
        client._calculate_retry_timeout(remaining, options, response_headers)
        for remaining in range(retries, 0, -1)
    ]


def test_bedrock_clients_back_off_for_minutes_without_retry_after(bedrock_env) -> None:
    llm = _build()
    for client in (llm._client, llm._async_client):
        waits = _retry_waits(client)
        assert len(waits) == 8
        # Jitter takes off up to a quarter of each wait.
        assert 0.75 * 183 <= sum(waits) <= 183
        assert max(waits) <= 60


def test_bedrock_clients_honour_retry_after(bedrock_env) -> None:
    llm = _build()
    assert set(_retry_waits(llm._async_client, httpx.Headers({"retry-after": "7"}))) == {7.0}


def test_anthropic_client_keeps_the_sdk_backoff(monkeypatch) -> None:
    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    llm = _build()
    assert type(llm) is ChatAnthropic
    assert sum(_retry_waits(llm._async_client)) <= 0.5 + 1 + 2 + 4 + 8 * 4


# --- AWS API errors while credentials are fetched ---------------------------------------


def _sts_error(code: str, status: int) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "..."}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "AssumeRole",
    )


@pytest.mark.parametrize("code, status", [
    ("Throttling", 400),
    ("RequestLimitExceeded", 400),
    ("SomethingNew", 503),
    ("SomethingNew", 429),
])
@pytest.mark.asyncio
async def test_transient_sts_error_while_signing_is_retried_by_the_sdk(
    bedrock_env, monkeypatch, no_retry_sleep, code: str, status: int
) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_stream_body("anthropic.claude-opus-5-5"),
    ))
    calls = _signing_fails(monkeypatch, _sts_error(code, status))
    msg = await _build().ainvoke("hi")
    assert msg.content == "hello"
    assert len(calls) == 2
    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_deterministic_sts_error_while_signing_is_not_retried(
    bedrock_env, monkeypatch, no_retry_sleep
) -> None:
    rec = _mock_mantle(monkeypatch, lambda _: httpx.Response(500))
    calls = _signing_fails(monkeypatch, _sts_error("AccessDenied", 403))
    with pytest.raises(BedrockCredentialsError) as info:
        await _build().ainvoke("hi")
    assert isinstance(info.value.__cause__, botocore.exceptions.ClientError)
    assert _svc().should_retry(info.value) is False
    assert len(calls) == 1
    assert rec.requests == []
