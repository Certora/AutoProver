"""Tests for the cloud-job preprocessing watchdog.

The watchdog probes the prover's treeview (written only once rule checking begins) to
detect jobs stuck in preprocessing, cancels them, and classifies them distinctly so no
conf workaround retries an identical doomed job.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prover_output_utility.exceptions import JobNotFoundError
from prover_output_utility.models import JobStatus as ProverJobStatus

from certora_autosetup.utils import job_problem_fixes
from certora_autosetup.utils.cloud_runner import CloudProverRunner, _WaitOutcome
from certora_autosetup.utils.job_problem_fixes import on_job_problem
from certora_autosetup.utils.preprocessing_watchdog import (
    PreprocessingWatchdog,
    WatchdogVerdict,
)
from certora_autosetup.utils.runner_types import (
    JobHandle,
    JobStatus,
    ProverResult,
    RunnerType,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_watchdog(clock, probe, budget=1800, grace=300, interval=90, max_errors=3):
    return PreprocessingWatchdog(
        budget_seconds=budget,
        grace_seconds=grace,
        probe_interval_seconds=interval,
        probe_treeview=probe,
        log=lambda *a, **k: None,
        max_consecutive_probe_errors=max_errors,
        clock=clock,
    )


class TestWatchdogStateMachine:
    def test_queue_time_never_starts_the_clock(self):
        clock = FakeClock()
        probe = MagicMock()
        wd = make_watchdog(clock, probe)
        for _ in range(10):
            assert wd.observe(is_running=False) is WatchdogVerdict.WAITING
            clock.advance(600)  # far beyond budget while queued
        probe.assert_not_called()

    def test_no_probes_during_grace(self):
        clock = FakeClock()
        probe = MagicMock()
        wd = make_watchdog(clock, probe, grace=300)
        assert wd.observe(is_running=True) is WatchdogVerdict.WAITING
        clock.advance(299)
        assert wd.observe(is_running=True) is WatchdogVerdict.WAITING
        probe.assert_not_called()

    def test_probe_cadence_rate_limited(self):
        clock = FakeClock()
        probe = MagicMock(side_effect=JobNotFoundError("not yet"))
        wd = make_watchdog(clock, probe, grace=0, interval=90)
        for _ in range(9):  # 9 ticks x 10s = 80s after the first probe
            wd.observe(is_running=True)
            clock.advance(10)
        assert probe.call_count == 1  # first probe only; next allowed at +90s
        clock.advance(10)
        wd.observe(is_running=True)
        assert probe.call_count == 2

    def test_treeview_with_rules_makes_watchdog_dormant(self):
        clock = FakeClock()
        probe = MagicMock(return_value={"rules": [{"name": "sanity"}]})
        wd = make_watchdog(clock, probe, grace=0)
        assert wd.observe(is_running=True) is WatchdogVerdict.PREPROCESSING_DONE
        clock.advance(10_000)
        assert wd.observe(is_running=True) is WatchdogVerdict.PREPROCESSING_DONE
        assert probe.call_count == 1  # dormant: no further probes ever

    def test_empty_rules_treeview_is_still_preprocessing(self):
        # Verified live: the cloud serves treeViewStatus.json with rules: [] for the
        # whole preprocessing phase — existence alone must NOT count as done.
        clock = FakeClock()
        probe = MagicMock(return_value={"rules": [], "contract": "Vault"})
        wd = make_watchdog(clock, probe, budget=1800, grace=0, interval=90)
        verdict = wd.observe(is_running=True)
        while verdict is WatchdogVerdict.WAITING and clock.now < 1000 + 3600:
            clock.advance(90)
            verdict = wd.observe(is_running=True)
        assert verdict is WatchdogVerdict.PREPROCESSING_TIMEOUT

    def test_budget_exceeded_without_treeview(self):
        clock = FakeClock()
        probe = MagicMock(side_effect=JobNotFoundError("not yet"))
        wd = make_watchdog(clock, probe, budget=1800, grace=0, interval=90)
        verdict = wd.observe(is_running=True)
        while verdict is WatchdogVerdict.WAITING and clock.now < 1000 + 3600:
            clock.advance(90)
            verdict = wd.observe(is_running=True)
        assert verdict is WatchdogVerdict.PREPROCESSING_TIMEOUT

    def test_not_found_is_not_an_error(self):
        clock = FakeClock()
        probe = MagicMock(side_effect=JobNotFoundError("not yet"))
        wd = make_watchdog(clock, probe, grace=0, interval=0, max_errors=3)
        for _ in range(10):
            assert wd.observe(is_running=True) is not WatchdogVerdict.DISABLED
            clock.advance(1)

    def test_disable_after_consecutive_errors_and_reset_on_success(self):
        clock = FakeClock()
        # two transport errors, then a not-found (resets the counter), then more errors
        probe = MagicMock(side_effect=[
            RuntimeError("boom"), RuntimeError("boom"), JobNotFoundError("not yet"),
            RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom"),
        ])
        wd = make_watchdog(clock, probe, grace=0, interval=0, max_errors=3)
        verdicts = []
        for _ in range(6):
            verdicts.append(wd.observe(is_running=True))
            clock.advance(1)
        assert verdicts[:5] == [WatchdogVerdict.WAITING] * 5
        assert verdicts[5] is WatchdogVerdict.DISABLED
        # disabled is terminal
        assert wd.observe(is_running=True) is WatchdogVerdict.DISABLED
        assert probe.call_count == 6


def _job_info(status):
    return SimpleNamespace(status=status, start_time=None, finish_time=None,
                           is_completed=False)


def make_cloud_runner(tmp_path):
    runner = CloudProverRunner(
        project_root=tmp_path,
        config_manager=MagicMock(),
        cloud_server="production",
        disable_cache=True,
    )
    return runner


class TestWaitLoopIntegration:
    def _run_wait(self, runner, prover_api, timeout=30):
        async def go():
            with patch("asyncio.sleep", new=AsyncMock()):
                return await runner._wait_for_job_completion_with_api(
                    prover_api, "https://prover.certora.com/output/1/abc", timeout
                )
        return asyncio.run(go())

    def test_stuck_preprocessing_is_cancelled_and_classified(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 1  # 1s of RUNNING without treeview
        runner.preprocessing_grace = 0
        runner.preprocessing_probe_interval = 0
        runner._cancel_cloud_job = AsyncMock(return_value=True)

        prover_api = MagicMock()
        prover_api.get_job_info.return_value = _job_info(ProverJobStatus.RUNNING)
        prover_api.get_treeview_status.side_effect = JobNotFoundError("not yet")

        t0 = time.monotonic()
        outcome, _, _ = self._run_wait(runner, prover_api, timeout=60)
        assert outcome is _WaitOutcome.PREPROCESSING_TIMEOUT
        assert time.monotonic() - t0 < 30  # nowhere near the 60s overall timeout
        runner._cancel_cloud_job.assert_awaited_once()

    def test_treeview_appearance_prevents_cancellation(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 1
        runner.preprocessing_grace = 0
        runner.preprocessing_probe_interval = 0
        runner._cancel_cloud_job = AsyncMock(return_value=True)

        prover_api = MagicMock()
        # RUNNING with a treeview available, then the job succeeds
        prover_api.get_treeview_status.return_value = {"rules": [{"name": "r"}]}
        prover_api.get_job_info.side_effect = (
            [_job_info(ProverJobStatus.RUNNING)] * 3
            + [_job_info(ProverJobStatus.SUCCEEDED)]
        )

        outcome, _, _ = self._run_wait(runner, prover_api, timeout=60)
        assert outcome is _WaitOutcome.COMPLETED
        runner._cancel_cloud_job.assert_not_awaited()

    def test_watchdog_disabled_by_zero_budget(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 0
        prover_api = MagicMock()
        prover_api.get_job_info.return_value = _job_info(ProverJobStatus.SUCCEEDED)
        outcome, _, _ = self._run_wait(runner, prover_api)
        assert outcome is _WaitOutcome.COMPLETED
        prover_api.get_treeview_status.assert_not_called()


def make_preprocessing_timeout_result(tmp_path, contract="Vault"):
    conf = tmp_path / "Vault.conf"
    conf.write_text('{"optimistic_loop": true, "loop_iter": 3}')
    job_spec = SimpleNamespace(
        contract_name=contract,
        phase="Sanity Test Run - warmup",
        config_file=SimpleNamespace(path=conf, content_hash="deadbeef"),
    )
    handle = JobHandle(
        job_id="https://prover.certora.com/output/1/abc",
        config_file=str(conf),
        config_content_hash="deadbeef",
        phase=job_spec.phase,
        submitted_at=time.time(),
        runner_type=RunnerType.CLOUD,
        status=JobStatus.PREPROCESSING_TIMEOUT,
    )
    return ProverResult(
        job_handle=handle,
        success=False,
        report_path=None,
        output_data={"job_url": handle.job_id, "preprocessing_timeout": True,
                     "return_code": 0},
        job_spec=job_spec,
        error_message="Preprocessing timeout",
        duration=100.0,
    )


class TestRetrySuppression:
    def test_on_job_problem_skips_workarounds(self, tmp_path):
        result = make_preprocessing_timeout_result(tmp_path)
        sentinel = MagicMock(return_value=True)
        with patch.object(job_problem_fixes, "_WORKAROUNDS", [sentinel]):
            assert on_job_problem(result, MagicMock(), MagicMock()) is False
        sentinel.assert_not_called()

    def test_status_round_trips_through_job_handle_serialization(self, tmp_path):
        result = make_preprocessing_timeout_result(tmp_path)
        restored = JobHandle.from_dict(result.job_handle.to_dict())
        assert restored.status is JobStatus.PREPROCESSING_TIMEOUT


class TestReporterRow:
    def test_sanity_row_for_preprocessing_timeout(self, tmp_path):
        from certora_autosetup.reporting.reporter import Reporter

        prover_api = MagicMock()
        reporter = Reporter(
            log=lambda *a, **k: None,
            verbose=False,
            skip_breadcrumbs=True,
            reports_dir=tmp_path / "reports",
            prover_api=prover_api,
        )
        result = make_preprocessing_timeout_result(tmp_path)
        rows = reporter._collect_sanity_rows([result], sanity_advanced=None)
        assert len(rows) == 1
        assert "PREPROCESSING TIMEOUT" in rows[0].sanity_status
        assert rows[0].job_url == result.job_url
        prover_api.get_job_report.assert_not_called()

    def test_job_report_fetch_failure_skips_row_not_summary(self, tmp_path):
        from certora_autosetup.reporting.reporter import Reporter

        prover_api = MagicMock()
        prover_api.get_job_report.side_effect = RuntimeError("cancelled job, no report")
        reporter = Reporter(
            log=lambda *a, **k: None,
            verbose=False,
            skip_breadcrumbs=True,
            reports_dir=tmp_path / "reports",
            prover_api=prover_api,
        )
        result = make_preprocessing_timeout_result(tmp_path)
        result.job_handle.status = JobStatus.FAILED  # generic failure, not watchdog
        rows = reporter._collect_sanity_rows([result], sanity_advanced=None)
        assert rows == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
