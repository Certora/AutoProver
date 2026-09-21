"""Tests for the shared cloud-job early-stop layer.

Covers the two checkers (PreprocessingCheck, FirstViolationCheck), the CloudProverRunner
poll-loop integration that consults them and cancels the job, the retry suppression in
on_job_problem, and the reporter's preprocessing-timeout row.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prover_output_utility.models import JobStatus as ProverJobStatus

from certora_autosetup.utils import job_problem_fixes
from certora_autosetup.utils.cloud_runner import CloudProverRunner
from certora_autosetup.utils.early_stop import (
    EarlyStop,
    FirstViolationCheck,
    PreprocessingCheck,
)
from certora_autosetup.utils.job_problem_fixes import on_job_problem
from certora_autosetup.utils.preprocessing_watchdog import WatchdogVerdict
from certora_autosetup.utils.prover_runner import ProverRunner
from certora_autosetup.utils.runner_types import (
    REASON_FIRST_VIOLATION,
    REASON_PREPROCESSING_TIMEOUT,
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


def _check(rule, violated):
    # mirrors prover_output_utility.models.CheckResult for the fields the checker reads.
    return SimpleNamespace(rule_name=rule, is_violated=violated)


def _job_info(status):
    return SimpleNamespace(status=status, start_time=None, finish_time=None, is_completed=False)


# ---- EarlyStop dataclass ---------------------------------------------------------------------

def test_early_stop_defaults():
    e = EarlyStop(reason="x")
    assert e.cancel is True
    assert e.drop_submission_cache is False
    assert e.message == ""


# ---- PreprocessingCheck: maps the watchdog verdict onto an EarlyStop --------------------------

def test_preprocessing_check_emits_early_stop_on_timeout_verdict():
    check = PreprocessingCheck(
        MagicMock(), "https://p/1/a", budget_seconds=1800,
        probe_interval_seconds=90, log=lambda *a, **k: None,
    )
    check._watchdog = SimpleNamespace(observe=lambda is_running: WatchdogVerdict.PREPROCESSING_TIMEOUT)
    e = check.observe(_job_info(ProverJobStatus.RUNNING))
    assert e is not None
    assert e.reason == REASON_PREPROCESSING_TIMEOUT
    assert e.drop_submission_cache is True and e.cancel is True


def test_preprocessing_check_none_for_non_timeout_verdicts():
    check = PreprocessingCheck(
        MagicMock(), "https://p/1/a", budget_seconds=1800,
        probe_interval_seconds=90, log=lambda *a, **k: None,
    )
    for v in (WatchdogVerdict.WAITING, WatchdogVerdict.PREPROCESSING_DONE, WatchdogVerdict.DISABLED):
        check._watchdog = SimpleNamespace(observe=lambda is_running, _v=v: _v)
        assert check.observe(_job_info(ProverJobStatus.RUNNING)) is None


def test_preprocessing_check_passes_is_running_flag():
    # Only a RUNNING status feeds is_running=True to the watchdog; QUEUED must pass False.
    seen = []
    check = PreprocessingCheck(
        MagicMock(), "https://p/1/a", budget_seconds=1800,
        probe_interval_seconds=90, log=lambda *a, **k: None,
    )
    check._watchdog = SimpleNamespace(observe=lambda is_running: seen.append(is_running) or WatchdogVerdict.WAITING)
    check.observe(_job_info(ProverJobStatus.RUNNING))
    check.observe(_job_info(ProverJobStatus.QUEUED))
    assert seen == [True, False]


# ---- FirstViolationCheck ---------------------------------------------------------------------

def _violation_check(api, clock, interval=30):
    return FirstViolationCheck(
        api, "https://p/1/a", probe_interval_seconds=interval,
        log=lambda *a, **k: None, clock=clock,
    )


def test_violation_check_fires_on_violated():
    checks = [_check("ok", False), _check("bad", True), _check("also", False)]
    api = SimpleNamespace(get_all_checks=lambda url: checks)
    e = _violation_check(api, FakeClock()).observe(_job_info(ProverJobStatus.RUNNING))
    assert e is not None
    assert e.reason == REASON_FIRST_VIOLATION
    assert "bad" in e.message


def test_violation_check_none_when_no_violation():
    api = SimpleNamespace(get_all_checks=lambda url: [_check("a", False), _check("b", False)])
    assert _violation_check(api, FakeClock()).observe(_job_info(ProverJobStatus.RUNNING)) is None


def test_violation_check_only_when_running():
    api = MagicMock()
    api.get_all_checks.return_value = [_check("bad", True)]
    check = _violation_check(api, FakeClock())
    assert check.observe(_job_info(ProverJobStatus.QUEUED)) is None
    api.get_all_checks.assert_not_called()  # no fetch while queued


def test_violation_check_rate_limited():
    clock = FakeClock()
    api = MagicMock()
    api.get_all_checks.return_value = [_check("a", False)]  # nothing violated yet
    check = _violation_check(api, clock, interval=30)
    check.observe(_job_info(ProverJobStatus.RUNNING))          # fetches at t=0
    assert api.get_all_checks.call_count == 1
    clock.advance(10)
    check.observe(_job_info(ProverJobStatus.RUNNING))          # within 30s, no fetch
    assert api.get_all_checks.call_count == 1
    clock.advance(25)
    check.observe(_job_info(ProverJobStatus.RUNNING))          # 35s > 30s, fetch again
    assert api.get_all_checks.call_count == 2


def test_violation_check_never_raises_on_fetch_error():
    def boom(url):
        raise RuntimeError("job cancelled, checks missing")
    api = SimpleNamespace(get_all_checks=boom)
    assert _violation_check(api, FakeClock()).observe(_job_info(ProverJobStatus.RUNNING)) is None


def test_violation_check_no_violation_during_empty_preprocessing():
    # During preprocessing get_all_checks returns nothing -> no gating on the watchdog needed.
    api = SimpleNamespace(get_all_checks=lambda url: [])
    assert _violation_check(api, FakeClock()).observe(_job_info(ProverJobStatus.RUNNING)) is None


# ---- rule-results parsing on a cancelled/gappy job (standard pipeline) ------------------------

def test_parse_rule_results_empty_on_fetch_error():
    def boom(job):
        raise RuntimeError("cannot fetch checks for cancelled job")
    stub = SimpleNamespace(prover_api=SimpleNamespace(get_all_checks=boom),
                           log=lambda *a, **k: None)
    assert ProverRunner.parse_rule_results_from_job(stub, "cancelled-job-id") == []


# ---- CloudProverRunner poll-loop integration -------------------------------------------------

def make_cloud_runner(tmp_path):
    return CloudProverRunner(
        project_root=tmp_path,
        config_manager=MagicMock(),
        cloud_server="production",
        disable_cache=True,
    )


def _run_wait(runner, prover_api, timeout=60):
    async def go():
        with patch("asyncio.sleep", new=AsyncMock()):
            return await runner._wait_for_job_completion_with_api(
                prover_api, "https://prover.certora.com/output/1/abc", timeout
            )
    return asyncio.run(go())


class TestWaitLoopIntegration:
    def test_stuck_preprocessing_is_cancelled_and_classified(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 1  # 1s of RUNNING without treeview
        runner.preprocessing_probe_interval = 0
        runner.stop_on_first_violation = False
        runner._cancel_cloud_job = AsyncMock(return_value=True)

        prover_api = MagicMock()
        prover_api.get_job_info.return_value = _job_info(ProverJobStatus.RUNNING)
        from prover_output_utility.exceptions import JobNotFoundError
        prover_api.get_treeview_status.side_effect = JobNotFoundError("not yet")

        t0 = time.monotonic()
        success, early = _run_wait(runner, prover_api, timeout=60)
        assert success is False
        assert early is not None and early.reason == REASON_PREPROCESSING_TIMEOUT
        assert time.monotonic() - t0 < 30  # nowhere near the 60s overall timeout
        runner._cancel_cloud_job.assert_awaited_once()

    def test_treeview_appearance_prevents_cancellation(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 1
        runner.preprocessing_probe_interval = 0
        runner.stop_on_first_violation = False
        runner._cancel_cloud_job = AsyncMock(return_value=True)

        prover_api = MagicMock()
        prover_api.get_treeview_status.return_value = {"rules": [{"name": "r"}]}
        prover_api.get_job_info.side_effect = (
            [_job_info(ProverJobStatus.RUNNING)] * 3
            + [_job_info(ProverJobStatus.SUCCEEDED)]
        )

        success, early = _run_wait(runner, prover_api, timeout=60)
        assert success is True and early is None
        runner._cancel_cloud_job.assert_not_awaited()

    def test_watchdog_disabled_by_zero_budget(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 0
        runner.stop_on_first_violation = False
        prover_api = MagicMock()
        prover_api.get_job_info.return_value = _job_info(ProverJobStatus.SUCCEEDED)
        success, early = _run_wait(runner, prover_api)
        assert success is True and early is None
        prover_api.get_treeview_status.assert_not_called()

    def test_stop_on_first_violation_cancels(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 0  # isolate the violation path
        runner.stop_on_first_violation = True
        runner.violation_probe_interval = 0
        runner._cancel_cloud_job = AsyncMock(return_value=True)

        prover_api = MagicMock()
        prover_api.get_job_info.return_value = _job_info(ProverJobStatus.RUNNING)
        prover_api.get_all_checks.return_value = [_check("ok", False), _check("bad", True)]

        success, early = _run_wait(runner, prover_api, timeout=60)
        assert success is False
        assert early is not None and early.reason == REASON_FIRST_VIOLATION
        runner._cancel_cloud_job.assert_awaited_once()

    def test_violation_ignored_when_flag_off(self, tmp_path):
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 0
        runner.stop_on_first_violation = False
        runner._cancel_cloud_job = AsyncMock(return_value=True)

        prover_api = MagicMock()
        prover_api.get_all_checks.return_value = [_check("bad", True)]
        prover_api.get_job_info.side_effect = (
            [_job_info(ProverJobStatus.RUNNING)] * 2
            + [_job_info(ProverJobStatus.SUCCEEDED)]
        )
        success, early = _run_wait(runner, prover_api, timeout=60)
        assert success is True and early is None
        runner._cancel_cloud_job.assert_not_awaited()
        prover_api.get_all_checks.assert_not_called()  # checker not even built

    def test_both_checkers_enabled_violation_after_preprocessing(self, tmp_path):
        # Both on: while preprocessing (empty checks, no treeview) neither fires; once a
        # violated check appears the violation checker stops the job.
        runner = make_cloud_runner(tmp_path)
        runner.preprocessing_budget = 10_000  # generous: watchdog won't fire in this short test
        runner.preprocessing_probe_interval = 0
        runner.stop_on_first_violation = True
        runner.violation_probe_interval = 0
        runner._cancel_cloud_job = AsyncMock(return_value=True)

        from prover_output_utility.exceptions import JobNotFoundError
        prover_api = MagicMock()
        prover_api.get_job_info.return_value = _job_info(ProverJobStatus.RUNNING)
        prover_api.get_treeview_status.side_effect = JobNotFoundError("not yet")
        # first few polls: no checks (preprocessing); then a violation appears
        checks_seq = [[], [], [_check("ok", False), _check("bad", True)]]
        prover_api.get_all_checks.side_effect = lambda url: checks_seq.pop(0) if checks_seq else [_check("bad", True)]

        success, early = _run_wait(runner, prover_api, timeout=60)
        assert success is False
        assert early is not None and early.reason == REASON_FIRST_VIOLATION
        runner._cancel_cloud_job.assert_awaited_once()


# ---- retry suppression -----------------------------------------------------------------------

def make_early_stop_result(tmp_path, reason=REASON_PREPROCESSING_TIMEOUT, contract="Vault"):
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
        status=JobStatus.EARLY_STOPPED,
    )
    return ProverResult(
        job_handle=handle,
        success=False,
        report_path=None,
        output_data={"job_url": handle.job_id, "return_code": 0},
        job_spec=job_spec,
        stopped_early=True,
        early_stop_reason=reason,
        duration=100.0,
    )


class TestRetrySuppression:
    def test_on_job_problem_skips_workarounds_for_any_early_stop(self, tmp_path):
        for reason in (REASON_PREPROCESSING_TIMEOUT, REASON_FIRST_VIOLATION):
            result = make_early_stop_result(tmp_path, reason=reason)
            sentinel = MagicMock(return_value=True)
            with patch.object(job_problem_fixes, "_WORKAROUNDS", [sentinel]):
                assert on_job_problem(result, MagicMock(), MagicMock()) is False
            sentinel.assert_not_called()

    def test_early_stopped_status_round_trips_through_serialization(self, tmp_path):
        result = make_early_stop_result(tmp_path)
        restored = JobHandle.from_dict(result.job_handle.to_dict())
        assert restored.status is JobStatus.EARLY_STOPPED

    def test_is_preprocessing_timeout_property(self, tmp_path):
        assert make_early_stop_result(tmp_path).is_preprocessing_timeout is True
        assert make_early_stop_result(
            tmp_path, reason=REASON_FIRST_VIOLATION
        ).is_preprocessing_timeout is False


# ---- reporter row ----------------------------------------------------------------------------

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
        result = make_early_stop_result(tmp_path)
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
        # a generic failure (not a preprocessing early stop) whose report fetch fails
        result = make_early_stop_result(tmp_path, reason=REASON_FIRST_VIOLATION)
        result.job_handle.status = JobStatus.FAILED
        rows = reporter._collect_sanity_rows([result], sanity_advanced=None)
        assert rows == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
