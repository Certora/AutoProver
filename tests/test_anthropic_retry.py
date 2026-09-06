"""AnthropicService.should_retry classifies a mid-stream connection drop as retryable.

A connection that drops while the response body streams surfaces as a raw
``httpx.RemoteProtocolError`` (the SDK does not wrap streamed-body failures as
``anthropic.APIConnectionError``); left unclassified it would crash the run instead of resuming
from checkpoint. See composer/llm/anthropic.py."""

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
