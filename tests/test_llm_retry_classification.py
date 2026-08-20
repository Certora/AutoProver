"""Which provider failures the run-wide retry floor resumes from
(``ProviderService.should_retry``).

This predicate is the whole of the policy's judgement: ``composer.io.context``'s floor asks it
whether to resume a graph from its last checkpoint or let the failure kill the task, and a task here
is a component that may have been authoring for hours. What the retry *machinery* then does is
``test_graph_retry.py``'s subject; this is only about the classification.

No network — the exceptions are constructed directly, which is also the point: these are the shapes
the SDK and its transport actually raise.
"""

import anthropic
import httpx
import pytest

from composer.llm.anthropic import _get_service


@pytest.fixture
def service():
    return _get_service()


def _status_error(code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "boom", response=httpx.Response(code, request=request), body=None
    )


def test_a_stall_mid_stream_is_retryable(service):
    # We stream, and the SDK's retries and exception wrapping both end when it hands over the
    # response stream — so a provider that goes quiet part-way through surfaces raw from the
    # transport. It means what APITimeoutError means, and cost two klend components before it was
    # classified: each had been authoring ~2h, and each died on the first occurrence.
    assert service.should_retry(httpx.ReadTimeout("timed out"))
    assert service.should_retry(httpx.ConnectTimeout("timed out"))


def test_provider_side_failures_are_retryable(service):
    assert service.should_retry(anthropic.APIConnectionError(request=httpx.Request("POST", "/")))
    assert service.should_retry(_status_error(500))
    assert service.should_retry(_status_error(529))  # overloaded
    assert service.should_retry(_status_error(429))


def test_a_deterministic_request_error_is_not_retryable(service):
    # The failure that motivated `Verdict.prompt_detail` was a 400: an over-long prompt is
    # over-long on every attempt, so resuming from the checkpoint would just spend the same tokens
    # to fail the same way.
    assert not service.should_retry(_status_error(400))
    assert not service.should_retry(_status_error(404))
    assert not service.should_retry(ValueError("a bug in our own code"))
