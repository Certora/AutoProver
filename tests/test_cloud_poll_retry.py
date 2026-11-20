"""Polling a running cloud job: a transient failure to read its status is read again, not raised
into the run that is waiting on the job."""

import aiohttp
import pytest

from composer.prover import cloud
from composer.prover.cloud import CloudJob

_JOB = CloudJob(base_url="https://prover.example", user_id="1", job_id="abcdef0123", anonymous_key="k")


def _reset() -> Exception:
    return aiohttp.ClientOSError(104, "Connection reset by peer")


def _answered(status: int) -> Exception:
    return aiohttp.ClientResponseError(None, (), status=status)  # type: ignore[arg-type]


class _Reads:
    """Answers each status read with the next item: an exception to raise, or a job status."""

    def __init__(self, *answers: Exception | str) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, session: aiohttp.ClientSession, url: str) -> dict:
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"job_status": answer}


async def _poll(monkeypatch: pytest.MonkeyPatch, reads: _Reads) -> dict:
    monkeypatch.setattr(cloud, "_read_job_data", reads)
    return await cloud.poll_job(_JOB, timeout=10, interval=0)


@pytest.mark.asyncio
async def test_a_reset_connection_is_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _Reads("RUNNING", _reset(), TimeoutError(), _answered(502), "SUCCEEDED")
    assert (await _poll(monkeypatch, reads))["job_status"] == "SUCCEEDED"
    assert reads.calls == 5


@pytest.mark.asyncio
async def test_a_good_read_resets_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = [_reset()] * (cloud._POLL_MAX_CONSECUTIVE_FAILURES - 1)
    reads = _Reads(*flaky, "RUNNING", *flaky, "SUCCEEDED")
    assert (await _poll(monkeypatch, reads))["job_status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_an_outage_that_does_not_end_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _Reads(*[_reset()] * cloud._POLL_MAX_CONSECUTIVE_FAILURES)
    with pytest.raises(aiohttp.ClientOSError):
        await _poll(monkeypatch, reads)
    assert reads.calls == cloud._POLL_MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_a_client_error_is_raised_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _Reads(_answered(403), "SUCCEEDED")
    with pytest.raises(aiohttp.ClientResponseError):
        await _poll(monkeypatch, reads)
    assert reads.calls == 1
