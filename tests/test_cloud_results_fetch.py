"""Downloading a finished cloud job's results: a failed read is retried, then reported, never raised
into the run."""

import json
from pathlib import Path

import pytest
from prover_output_utility.exceptions import ProverAPIError
from prover_output_utility.models import JobStatus

from composer.prover import cloud
from composer.prover.cloud import CloudJobError, CloudResultsUnavailable, cloud_results
from composer.prover.results import ErrorAlert

_LINK = "https://prover.certora.com/jobStatus/37632/793b747e3a2c41a4819b6a0ec9705ad7?anonymousKey=k"
_HINT = (
    "TAC Clock::get expected to extract 5 TAC variables from call "
    "sol_get_clock_sysvar  /* 0x1c4b0 */"
)


class _FlakyAPI:
    def __init__(self, failures: int, report: str | ProverAPIError = "{}") -> None:
        self.failures = failures
        self.report = report
        self.calls = 0
        self.alert_calls = 0

    def fetch_sources_and_treeview_files(self, job_id: str, dest: Path) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProverAPIError("Read timed out")

    def fetch_output_file(self, job_id: str, name: str) -> str:
        self.alert_calls += 1
        if isinstance(self.report, ProverAPIError):
            raise self.report
        return self.report


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    def install(
        failures: int, *, status: str = "SUCCEEDED", report: str | ProverAPIError = "{}"
    ) -> _FlakyAPI:
        fake = _FlakyAPI(failures, report)
        monkeypatch.setattr(cloud, "results_api", lambda: fake)

        async def finished(*_args, **_kwargs) -> dict:
            return {"job_status": status}

        monkeypatch.setattr(cloud, "poll_job", finished)
        return fake

    async def no_sleep(_s: float) -> None:
        pass

    monkeypatch.setattr(cloud.asyncio, "sleep", no_sleep)
    return install


def _clock_report() -> str:
    return json.dumps([
        {
            "type": "Summarization",
            "severity": "WARNING",
            "message": "The following functions are neither inlined nor summarized. [sol_sha256]",
        },
        {
            "type": "General",
            "severity": "ERROR",
            "message": (
                "An internal Prover error occurred, please contact support, "
                "and see low-level crash details below:"
            ),
            "hint": _HINT,
        },
    ])


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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FAILED", "HALTED"])
async def test_a_job_that_ran_carries_its_error_alert(api, status: str) -> None:
    fake = api(0, status=status, report=_clock_report())

    with pytest.raises(CloudJobError) as raised:
        async with cloud_results(_LINK, poll_timeout=1):
            pytest.fail("a job without verdicts should not be yielded")

    assert raised.value.status == JobStatus(status)
    assert raised.value.link == _LINK
    assert raised.value.alerts == (ErrorAlert("General", _HINT),)
    assert fake.alert_calls == 1
    assert fake.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["CANCELED", "UPLOAD_FAILED", "SERVICE_UNAVAILABLE"])
async def test_a_job_rejected_before_it_runs_does_not_fetch_alerts(api, status: str) -> None:
    fake = api(0, status=status, report=_clock_report())

    with pytest.raises(CloudJobError) as raised:
        async with cloud_results(_LINK, poll_timeout=1):
            pytest.fail("a job without verdicts should not be yielded")

    assert raised.value.alerts == ()
    assert fake.alert_calls == 0


@pytest.mark.asyncio
async def test_a_failed_job_with_no_alert_report_keeps_its_link(api) -> None:
    fake = api(0, status="FAILED", report=ProverAPIError("missing"))

    with pytest.raises(CloudJobError) as raised:
        async with cloud_results(_LINK, poll_timeout=1):
            pytest.fail("a job without verdicts should not be yielded")

    assert raised.value.link == _LINK
    assert raised.value.alerts == ()
    assert fake.alert_calls == 1


@pytest.mark.asyncio
async def test_a_poll_timeout_names_the_job_and_skips_the_alert(api, monkeypatch) -> None:
    fake = api(0, report=_clock_report())

    async def timed_out(*_args, **_kwargs) -> dict:
        raise TimeoutError

    monkeypatch.setattr(cloud, "poll_job", timed_out)

    with pytest.raises(CloudJobError) as raised:
        async with cloud_results(_LINK, poll_timeout=1):
            pytest.fail("a timed-out poll should not be yielded")

    assert raised.value.status == JobStatus.UNKNOWN
    assert raised.value.link == _LINK
    assert raised.value.alerts == ()
    assert fake.alert_calls == 0
