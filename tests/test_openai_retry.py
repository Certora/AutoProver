"""The OpenAI-family services classify an error the server reports inside an open stream.

The OpenAI SDK shares the Stainless taxonomy with Anthropic's, and so shares the blind spot:
an error event delivered part-way through a stream rides the already-open 200 response, and the
SDK builds the exception from that response — so the status code says nothing and the payload's
``error.type`` is the only discriminator. See tests/test_anthropic_retry.py for the twin, and
composer/llm/openai.py for the roster.

Unlike the Anthropic side, this is not backed by an observed incident: the roster is deliberately
narrow, and a type it does not name stays deterministic rather than being guessed at.
"""

import httpx
import openai
import pytest

from composer.llm.openai import OpenAIService
from composer.llm.openrouter import OpenRouterService


def _stream_error(error_type: str, status_code: int = 200) -> openai.APIStatusError:
    """The exception the SDK raises for an error event inside an open stream: built from the
    streaming response, so it keeps that response's status."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIStatusError(
        f"Error code: {status_code}",
        response=httpx.Response(status_code, request=request),
        body={"error": {"type": error_type, "message": "..."}},
    )


def _services():
    return [OpenAIService(), OpenRouterService()]


@pytest.mark.parametrize("svc", _services(), ids=["openai", "openrouter"])
def test_mid_stream_server_error_is_retryable(svc):
    assert svc.should_retry(_stream_error("server_error")) is True


@pytest.mark.parametrize("svc", _services(), ids=["openai", "openrouter"])
def test_mid_stream_rate_limit_is_retryable(svc):
    assert svc.should_retry(_stream_error("rate_limit_exceeded")) is True


@pytest.mark.parametrize("svc", _services(), ids=["openai", "openrouter"])
def test_mid_stream_request_error_not_retryable(svc):
    assert svc.should_retry(_stream_error("invalid_request_error")) is False


@pytest.mark.parametrize("svc", _services(), ids=["openai", "openrouter"])
def test_quota_exhaustion_not_retryable(svc):
    # Re-running does not refill the account; this one is deterministic.
    assert svc.should_retry(_stream_error("insufficient_quota")) is False


@pytest.mark.parametrize("svc", _services(), ids=["openai", "openrouter"])
def test_status_code_still_decides_when_it_can(svc):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    exc = openai.APIStatusError(
        "Error code: 503",
        response=httpx.Response(503, request=request),
        body=None,
    )
    assert svc.should_retry(exc) is True


@pytest.mark.parametrize("svc", _services(), ids=["openai", "openrouter"])
def test_unparsed_body_not_retryable(svc):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    exc = openai.APIStatusError(
        "gateway said no",
        response=httpx.Response(200, request=request),
        body="gateway said no",
    )
    assert svc.should_retry(exc) is False
