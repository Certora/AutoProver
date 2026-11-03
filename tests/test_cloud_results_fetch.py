"""Downloading a finished cloud job's results: a failed read is retried, then reported, never raised
into the run."""

import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from prover_output_utility.exceptions import ProverAPIError
from prover_output_utility.models import JobStatus

from composer.prover import cloud, core
from composer.prover.cloud import CloudJobError, CloudResultsUnavailable, cloud_results
from composer.prover.core import ProverCallbacks, ProverOptions, ProverReport
from composer.prover.results import AlertReport, ErrorAlert, FetchedResults

_TREE_VIEW = Path(__file__).parent / "data" / "solana_cex" / "loop_unwinding" / "Reports"
_LINK = "https://prover.certora.com/jobStatus/37632/793b747e3a2c41a4819b6a0ec9705ad7?anonymousKey=k"
_HINT = (
    "TAC Clock::get expected to extract 5 TAC variables from call "
    "sol_get_clock_sysvar  /* 0x1c4b0 */"
)


class _FlakyAPI:
    def __init__(
        self, failures: int, report: str | ProverAPIError = "{}", tree_view: bool = False
    ) -> None:
        self.failures = failures
        self.report = report
        self.tree_view = tree_view
        self.calls = 0
        self.alert_calls = 0

    def fetch_sources_and_treeview_files(self, job_id: str, dest: Path) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProverAPIError("Read timed out")
        if self.tree_view:
            shutil.copytree(_TREE_VIEW, dest / "Reports")

    def fetch_output_file(self, job_id: str, name: str) -> str:
        self.alert_calls += 1
        if isinstance(self.report, ProverAPIError):
            raise self.report
        return self.report


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    def install(
        failures: int,
        *,
        status: str = "SUCCEEDED",
        report: str | ProverAPIError = "{}",
        tree_view: bool = False,
    ) -> _FlakyAPI:
        fake = _FlakyAPI(failures, report, tree_view)
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

    async with cloud_results(_LINK, poll_timeout=1) as fetched:
        assert fetched.root.is_dir()

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
async def test_a_failed_job_carries_its_error_alert(api) -> None:
    fake = api(0, status="FAILED", report=_clock_report())

    with pytest.raises(CloudJobError) as raised:
        async with cloud_results(_LINK, poll_timeout=1):
            pytest.fail("a job without verdicts should not be yielded")

    assert raised.value.status == JobStatus.FAILED
    assert raised.value.link == _LINK
    assert raised.value.alerts == (ErrorAlert("General", _HINT),)
    assert fake.alert_calls == 1
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_a_halted_job_yields_the_verdicts_it_finished(api) -> None:
    api(0, status="HALTED", tree_view=True)

    async with cloud_results(_LINK, poll_timeout=1) as fetched:
        assert fetched.halted
        assert (fetched.root / "Reports" / "treeView").is_dir()


@pytest.mark.asyncio
async def test_a_succeeded_job_is_not_halted(api) -> None:
    api(0, tree_view=True)

    async with cloud_results(_LINK, poll_timeout=1) as fetched:
        assert not fetched.halted


@pytest.mark.asyncio
async def test_a_halted_job_with_no_tree_view_carries_its_error_alert(api) -> None:
    fake = api(0, status="HALTED", report=_clock_report())

    with pytest.raises(CloudJobError) as raised:
        async with cloud_results(_LINK, poll_timeout=1):
            pytest.fail("a job without verdicts should not be yielded")

    assert raised.value.status == JobStatus.HALTED
    assert raised.value.alerts == (ErrorAlert("General", _HINT),)
    assert fake.calls == 1


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


@pytest.mark.asyncio
@pytest.mark.parametrize("halted", [True, False])
async def test_the_report_says_when_the_job_was_cut_off(monkeypatch, halted: bool) -> None:
    async def submitted(*_args, **_kwargs):
        return {"sort": "success", "link": _LINK, "is_local_link": False}, ""

    @asynccontextmanager
    async def fetched(*_args, **_kwargs):
        yield FetchedResults(_TREE_VIEW.parent, None, AlertReport(), halted=halted)

    monkeypatch.setattr(core, "run_prover_inner", submitted)
    monkeypatch.setattr(core, "cloud_results", fetched)

    report = await core.run_prover(
        Path("."), [], "tc", ProverOptions(app="solana", server="prod"), ProverCallbacks(), None
    )

    assert isinstance(report, ProverReport)
    assert report.result_str.startswith(core._HALTED_NOTE) == halted
