"""CloudProverRunner.stop_on_first_violation: cancel a still-running job the moment a rule VIOLATES,
and fetch partial results robustly (a cancelled/gappy job must degrade, never crash).
"""
import asyncio
from types import SimpleNamespace

from prover_output_utility.models import JobStatus as ProverJobStatus

from certora_autosetup.utils.cloud_runner import CloudProverRunner
from certora_autosetup.utils.prover_runner import ProverRunner


def _check(rule, status):
    return SimpleNamespace(status=SimpleNamespace(value=status), rule_name=rule)


def _stub(**attrs):
    s = SimpleNamespace(**attrs)
    s.log = lambda msg, level="INFO": None
    return s


# ---- _partial_violated_checks: filter + never-raise ------------------------------------------------

def test_partial_violated_checks_filters_violated():
    api = SimpleNamespace(get_all_checks=lambda url: [
        _check("a", "VERIFIED"), _check("b", "VIOLATED"), _check("c", "RUNNING"), _check("b", "VIOLATED"),
    ])
    stub = _stub()
    allc, viol = CloudProverRunner._partial_violated_checks(stub, api, "u")
    assert len(allc) == 4
    assert sorted({c.rule_name for c in viol}) == ["b"]
    assert len(viol) == 2


def test_partial_violated_checks_never_raises_on_fetch_error():
    def boom(url):
        raise RuntimeError("job cancelled, tree missing")
    stub = _stub()
    allc, viol = CloudProverRunner._partial_violated_checks(stub, SimpleNamespace(get_all_checks=boom), "u")
    assert allc == [] and viol == []


# ---- parse robustness on a cancelled/gappy job -----------------------------------------------------

def test_parse_rule_results_empty_on_fetch_error():
    def boom(job):
        raise RuntimeError("cannot fetch checks for cancelled job")
    stub = _stub(prover_api=SimpleNamespace(get_all_checks=boom))
    out = ProverRunner.parse_rule_results_from_job(stub, "cancelled-job-id")
    assert out == []


# ---- the poll-loop hook: cancel + return partial checks on first violation --------------------------

def test_wait_cancels_and_returns_partial_on_first_violation():
    cancelled = {"called": False}
    async def fake_cancel(url):
        cancelled["called"] = True
        return True
    checks = [_check("ok", "VERIFIED"), _check("bad", "VIOLATED")]
    api = SimpleNamespace(
        get_job_info=lambda url: SimpleNamespace(status=ProverJobStatus.RUNNING, start_time=1.0, finish_time=None),
        get_all_checks=lambda url: checks,
    )
    stub = _stub(stop_on_first_violation=True, _cancel_cloud_job=fake_cancel)
    stub._partial_violated_checks = CloudProverRunner._partial_violated_checks.__get__(stub)
    success, _s, _f, early = asyncio.run(
        CloudProverRunner._wait_for_job_completion_with_api(stub, api, "u", 60)
    )
    assert success is False
    assert cancelled["called"] is True
    assert early is not None and [c.rule_name for c in early] == ["ok", "bad"]


def test_wait_ignores_violation_when_flag_off():
    # flag off -> a RUNNING poll with a violated check must NOT cancel; a subsequent SUCCEEDED ends it.
    seq = [ProverJobStatus.RUNNING, ProverJobStatus.SUCCEEDED]
    def get_job_info(url):
        return SimpleNamespace(status=seq.pop(0), start_time=1.0, finish_time=2.0)
    cancelled = {"called": False}
    async def fake_cancel(url):
        cancelled["called"] = True
        return True
    api = SimpleNamespace(get_job_info=get_job_info, get_all_checks=lambda url: [_check("bad", "VIOLATED")])
    stub = _stub(stop_on_first_violation=False, _cancel_cloud_job=fake_cancel)
    stub._partial_violated_checks = CloudProverRunner._partial_violated_checks.__get__(stub)
    # poll_interval is 10s; shrink the wait by making the 2nd poll SUCCEED. asyncio.sleep(10) would stall
    # the test, so patch asyncio.sleep to a no-op for this call.
    import certora_autosetup.utils.cloud_runner as cr
    orig_sleep = asyncio.sleep
    async def nosleep(_): return None
    asyncio.sleep = nosleep
    try:
        success, _s, _f, early = asyncio.run(
            CloudProverRunner._wait_for_job_completion_with_api(stub, api, "u", 60)
        )
    finally:
        asyncio.sleep = orig_sleep
    assert success is True and early is None and cancelled["called"] is False
