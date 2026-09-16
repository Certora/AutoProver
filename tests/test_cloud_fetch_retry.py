"""A dropped connection while fetching a finished job must not cost the run its unit.

The artifacts of a succeeded job are immutable, and retrieving them is a pure read into a scratch
directory — but it makes hundreds of requests, and a peer that goes away partway through fails the
whole call. Nothing retried it: ``install_retry_policy`` classifies *LLM-provider* failures, so a
prover-API blip propagated out of the authoring graph and took the unit down with it. That was
observed: a four-hour run lost one of its three parallel units this way, on the unit's first
submission, to a job that had already succeeded — and the loss was discovered two hours later.

What is asserted here is the shape of the retry rather than any timing: transient failures are
re-asked, a failure a second attempt cannot change is not, and exhausting the attempts still raises
so a genuinely unreachable endpoint is not hidden behind a loop.
"""

from pathlib import Path

import pytest
from prover_output_utility.exceptions import JobNotFoundError, ProverAPIError

from composer.prover import cloud

pytestmark = pytest.mark.asyncio


class _Api:
    """Stands in for ``ProverOutputAPI``, failing its first ``failures`` calls."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self._error = error or ProverAPIError("Remote end closed connection without response")
        self._failures = failures
        self.calls = 0

    def fetch_sources_and_treeview_files(self, job_id: str, dest: Path) -> Path:
        self.calls += 1
        if self.calls <= self._failures:
            raise self._error
        return dest


@pytest.fixture
def instant_backoff(monkeypatch):
    """The waits are real seconds; the test asserts on attempts, not on how long they took."""
    slept: list[float] = []

    async def record(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(cloud.asyncio, "sleep", record)
    return slept


def _install(monkeypatch, api: _Api) -> None:
    monkeypatch.setattr(cloud, "_results_api", lambda: api)


async def test_a_dropped_connection_is_asked_again(monkeypatch, instant_backoff, tmp_path):
    api = _Api(failures=1)
    _install(monkeypatch, api)

    await cloud._fetch_results("job-1234abcd", tmp_path)

    assert api.calls == 2
    assert instant_backoff == [cloud._FETCH_BACKOFF_S]


async def test_the_backoff_grows(monkeypatch, instant_backoff, tmp_path):
    api = _Api(failures=2)
    _install(monkeypatch, api)

    await cloud._fetch_results("job-1234abcd", tmp_path)

    assert api.calls == 3
    assert instant_backoff == [cloud._FETCH_BACKOFF_S, 2 * cloud._FETCH_BACKOFF_S]


async def test_an_unreachable_endpoint_still_fails(monkeypatch, instant_backoff, tmp_path):
    api = _Api(failures=cloud._FETCH_ATTEMPTS)
    _install(monkeypatch, api)

    with pytest.raises(ProverAPIError):
        await cloud._fetch_results("job-1234abcd", tmp_path)

    assert api.calls == cloud._FETCH_ATTEMPTS


async def test_a_missing_job_is_not_retried(monkeypatch, instant_backoff, tmp_path):
    """A second attempt cannot make a job exist, and the backoff is time the run does not have."""
    api = _Api(failures=1, error=JobNotFoundError("no such job"))
    _install(monkeypatch, api)

    with pytest.raises(JobNotFoundError):
        await cloud._fetch_results("job-1234abcd", tmp_path)

    assert api.calls == 1
    assert instant_backoff == []


async def test_a_transport_error_outside_the_wrapper_is_retried(
    monkeypatch, instant_backoff, tmp_path
):
    """``requests`` raises ``IOError`` subclasses, which POU does not always wrap."""
    api = _Api(failures=1, error=ConnectionResetError("Connection reset by peer"))
    _install(monkeypatch, api)

    await cloud._fetch_results("job-1234abcd", tmp_path)

    assert api.calls == 2
