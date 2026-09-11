"""Tests for the budget check on the cloud-poll callback.

Waiting on a cloud prover job is one tool call, so the author's monitor gets no turn
to sample the budget while it blocks. The callback samples it on every status tick
instead.
"""

import asyncio
import time

import pytest

from composer.diagnostics.budget import (
    BudgetExceeded,
    exhausted_constraint,
    time_budget,
)
from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.spec.source.prover import _SpecCallbacks


def _callbacks(summary: RunSummary) -> _SpecCallbacks:
    return _SpecCallbacks(lambda _e: None, "toolu_test", summary, {})


def _summary_started_ago(seconds: float) -> RunSummary:
    """A run summary whose clock already reads ``seconds`` of wall time."""
    return RunSummary(started_at_mono=time.perf_counter() - seconds)


def test_no_budget_installed_never_raises() -> None:
    install_run_summary(_summary_started_ago(10_000))
    assert exhausted_constraint() is None


def test_poll_raises_once_the_time_budget_is_spent() -> None:
    summary = _summary_started_ago(7_300)
    install_run_summary(summary)
    with time_budget(7_200), pytest.raises(BudgetExceeded, match="Time budget"):
        asyncio.run(_callbacks(summary).on_cloud_poll("RUNNING", "Cloud job 0059382f: RUNNING"))


def test_poll_is_quiet_while_inside_the_budget() -> None:
    summary = _summary_started_ago(60)
    install_run_summary(summary)
    with time_budget(7_200):
        asyncio.run(_callbacks(summary).on_cloud_poll("RUNNING", "Cloud job 0059382f: RUNNING"))
