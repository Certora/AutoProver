"""Ambient run state across executions: the run summary and the cost budget are
restored from the store on entry, saved periodically, and flushed on exit, so a
run's ledger and its budgets span the processes that work on it.

An in-memory store stands in for Postgres. "Processes" are successive context
managers over the same store and run id.
"""

import asyncio
import time

import pytest
from langgraph.store.memory import InMemoryStore

from composer.diagnostics.ambient import AmbientStateSaver
from composer.diagnostics.budget import (
    _budget_accumulator,
    _cost_centers,
    accumulate_cost,
    budget_pressure,
    named_budget,
    total_budget,
)
from composer.diagnostics.timing import RunSummary, TokenTotals, install_run_summary, set_current_task_id
from composer.io.thread_logging import ambient_state_ns, default_logging_ns

pytestmark = pytest.mark.asyncio

NS = ambient_state_ns(default_logging_ns(None), "run-1")


def usage(model: str, inp: int, out: int) -> dict:
    return {
        "model_name": model, "total_input_tokens": inp, "total_output_tokens": out,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }


def test_summary_round_trips_including_in_flight_state() -> None:
    s = RunSummary(run_id="run-1", execution_id="e1")
    with set_current_task_id("t-done"):
        s.record_token_usage(usage("m", 10, 5))  # type: ignore[arg-type]
        s.add_prover_call(2.0)
        s.record_prover_runtime(300)
        s.record_prover_link("http://x")
    s.record_phase(task_id="t-done", label="done", phase="P", wall_s=3.0, queue_wait_s=0.5)
    with set_current_task_id("t-open"):
        s.record_token_usage(usage("m", 7, 1))  # type: ignore[arg-type]
        s.add_prover_call(1.0)
    s.prior_wall_s = 100.0

    out: dict = {}
    s.save_to(out)
    r = RunSummary(run_id="run-1", execution_id="e2")
    r.restore_from(out)

    assert r.prior_wall_s >= 100.0
    assert [p.task_id for p in r.phases] == ["t-done"]
    assert r.phases[0].token_usage_by_model == {"m": TokenTotals(input=10, output=5)}
    assert r.phases[0].prover_calls == 1 and r.phases[0].final_link == "http://x"
    assert r.token_usage_by_model == {"m": TokenTotals(input=17, output=6)}
    assert r.prover_total_calls == 2 and r.prover_reported_ms_total == 300
    # The open task's spend is still in flight, and folds into its record when it ends.
    r.record_phase(task_id="t-open", label="open", phase="P", wall_s=1.0, queue_wait_s=0.0)
    assert r.phases[-1].token_usage_by_model == {"m": TokenTotals(input=7, output=1)}
    assert r.phases[-1].prover_calls == 1


def test_summary_ignores_another_runs_record() -> None:
    other = RunSummary(run_id="run-2")
    out: dict = {}
    RunSummary(run_id="run-1", prover_total_calls=9).save_to(out)
    other.restore_from(out)
    assert other.prover_total_calls == 0


async def test_summary_persists_across_executions_and_wall_time_accrues() -> None:
    store = InMemoryStore()
    saver = AmbientStateSaver(store, NS, interval_s=60)

    async with install_run_summary(RunSummary(run_id="run-1", execution_id="e1"), saver) as s1:
        with set_current_task_id("a"):
            s1.record_token_usage(usage("m", 1, 1))  # type: ignore[arg-type]
        s1.record_phase(task_id="a", label="a", phase="P", wall_s=1.0, queue_wait_s=0.0)
        s1.started_at_mono = time.perf_counter() - 30.0  # this execution ran for 30s
    assert (await store.aget(NS, "run_summary")) is not None

    async with install_run_summary(RunSummary(run_id="run-1", execution_id="e2"), saver) as s2:
        assert [p.task_id for p in s2.phases] == ["a"]
        assert s2.token_usage_by_model == {"m": TokenTotals(input=1, output=1)}
        assert 30.0 <= s2.total_wall_s() < 31.0, "prior executions' time counts, the gap between them does not"


async def test_summary_is_saved_periodically_while_running() -> None:
    store = InMemoryStore()
    saver = AmbientStateSaver(store, NS, interval_s=0.05)
    async with install_run_summary(RunSummary(run_id="run-1"), saver) as s:
        s.prover_total_calls = 3
        await asyncio.sleep(0.2)
        saved = await store.aget(NS, "run_summary")
        assert saved is not None and saved.value["prover_total_calls"] == 3


async def test_cost_budget_spend_carries_across_executions() -> None:
    store = InMemoryStore()
    saver = AmbientStateSaver(store, NS, interval_s=60)

    async with total_budget(10.0, {"phase": 8.0}, saver=saver):
        with named_budget("phase"):
            accumulate_cost(6.0)
        accumulate_cost(1.0)  # outside any center: the pool alone
    saved = await store.aget(NS, "cost_budget")
    assert saved is not None and saved.value == {"schema": 1, "pool": 7.0, "centers": {"phase": 6.0}}

    async with total_budget(10.0, {"phase": 8.0, "later": 5.0}, saver=saver):
        centers = _cost_centers.get()
        assert centers is not None
        assert centers["phase"].curr_cost == 6.0 and centers["later"].curr_cost == 0.0
        pool = _budget_accumulator.get()
        assert pool is not None and pool.curr_cost == 7.0
        with named_budget("later"):
            accumulate_cost(2.5)
            assert budget_pressure(), "9.5 of 10 spent across two executions"


async def test_no_saver_means_no_persistence() -> None:
    store = InMemoryStore()
    async with install_run_summary(RunSummary(run_id="run-1")):
        pass
    async with total_budget(1.0, {}):
        pass
    assert await store.aget(NS, "run_summary") is None
    assert await store.aget(NS, "cost_budget") is None
