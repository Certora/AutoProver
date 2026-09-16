"""AnthropicService.should_retry classifies the two failures a stream can deliver.

A connection that drops while the response body streams surfaces as a raw
``httpx.RemoteProtocolError`` (the SDK does not wrap streamed-body failures as
``anthropic.APIConnectionError``); an error the server reports part-way through a stream rides
the already-open 200 response, so the exception's status code says nothing about it. Left
unclassified either one would crash the run instead of resuming from checkpoint.
See composer/llm/anthropic.py."""

import anthropic
import httpx

from composer.llm.anthropic import AnthropicService


def _svc() -> AnthropicService:
    return AnthropicService()


def test_mid_stream_drop_is_retryable():
    exc = httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    assert _svc().should_retry(exc) is True


def test_connection_error_still_retryable():
    exc = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    assert _svc().should_retry(exc) is True


def test_deterministic_error_not_retryable():
    assert _svc().should_retry(ValueError("bad prompt")) is False


def _stream_error(error_type: str, status_code: int = 200) -> anthropic.APIStatusError:
    """The exception the SDK raises for an error event inside an open stream:
    built from the streaming response, so it keeps that response's status."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        f"Error code: {status_code}",
        response=httpx.Response(status_code, request=request),
        body={"type": "error", "error": {"type": error_type, "message": "..."}},
    )


def test_mid_stream_overloaded_is_retryable():
    assert _svc().should_retry(_stream_error("overloaded_error")) is True


def test_mid_stream_server_error_is_retryable():
    assert _svc().should_retry(_stream_error("api_error")) is True


def test_mid_stream_request_error_not_retryable():
    assert _svc().should_retry(_stream_error("invalid_request_error")) is False


def test_status_code_still_decides_when_it_can():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.APIStatusError(
        "Error code: 529",
        response=httpx.Response(529, request=request),
        body=None,
    )
    assert _svc().should_retry(overloaded) is True


def test_unparsed_body_not_retryable():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIStatusError(
        "gateway said no",
        response=httpx.Response(200, request=request),
        body="gateway said no",
    )
    assert _svc().should_retry(exc) is False
