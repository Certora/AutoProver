"""Downloading a finished cloud job's results: a failed read is retried, then reported, never raised
into the run."""

from pathlib import Path

import pytest
from prover_output_utility.exceptions import ProverAPIError

from composer.prover import cloud
from composer.prover.cloud import CloudResultsUnavailable, cloud_results

_LINK = "https://prover.certora.com/jobStatus/37632/793b747e3a2c41a4819b6a0ec9705ad7?anonymousKey=k"


class _FlakyAPI:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def fetch_sources_and_treeview_files(self, job_id: str, dest: Path) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProverAPIError("Read timed out")

    def fetch_output_file(self, job_id: str, name: str) -> str:
        return "{}"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    def install(failures: int) -> _FlakyAPI:
        fake = _FlakyAPI(failures)
        monkeypatch.setattr(cloud, "results_api", lambda: fake)
        return fake

    async def succeeded(*_args, **_kwargs) -> dict:
        return {"jobStatus": "SUCCEEDED"}

    async def no_sleep(_s: float) -> None:
        pass

    monkeypatch.setattr(cloud, "poll_job", succeeded)
    monkeypatch.setattr(cloud.asyncio, "sleep", no_sleep)
    return install


@pytest.mark.asyncio
async def test_a_transient_read_failure_is_retried(api) -> None:
    fake = api(failures=2)

    async with cloud_results(_LINK, poll_timeout=1) as (dest, _):
        assert dest.is_dir()

    assert fake.calls == 3


@pytest.mark.asyncio
async def test_a_read_that_keeps_failing_names_the_job(api) -> None:
    fake = api(failures=100)

    with pytest.raises(CloudResultsUnavailable) as raised:
        async with cloud_results(_LINK, poll_timeout=1):
            pytest.fail("results should not have been yielded")

    assert raised.value.link == _LINK
    assert fake.calls == cloud._FETCH_MAX_ATTEMPTS
