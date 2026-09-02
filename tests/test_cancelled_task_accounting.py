"""Tests for how ``task_logger`` accounts for a task the gate cancelled.

A cancelled task has usually already spent tokens, so it gets a row of its own with the tokens it
spent — but the failure it was stopped for is the one the end-of-run summary reports, and a
cancellation is not a second failure beside it.

Stubs only — no LLM, no DB, no backend wheel.
"""

import asyncio
import logging

import pytest

from composer.diagnostics.timing import (
    CANCELLED,
    RunSummary,
    TokenTotals,
    install_run_summary,
    task_logger,
)
from graphcore.utils import TokenUsageDict

pytestmark = pytest.mark.asyncio

_LOG = logging.getLogger(__name__)


def _usage(model: str, i: int, o: int, cr: int, cw: int) -> TokenUsageDict:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
        "model_name": model,
    }


async def _cancel_one_task(summary: RunSummary) -> None:
    """One running, token-spending task, cancelled where the gate would cancel it."""
    install_run_summary(summary)
    with pytest.raises(asyncio.CancelledError):
        async with task_logger("extract-0", "component", "extraction", _LOG) as log:
            log.task_started()
            summary.record_token_usage(_usage("opus", 100, 10, 5, 2))
            raise asyncio.CancelledError


async def test_a_cancelled_task_is_recorded_with_the_tokens_it_spent():
    # The spend is real whether or not the task got to finish, so it belongs to that task's row
    # rather than being stranded in the in-flight bucket and reported by nobody.
    summary = RunSummary()
    await _cancel_one_task(summary)

    record = summary.phases[0]
    assert (record.task_id, record.phase, record.error) == ("extract-0", "extraction", CANCELLED)
    assert sum(record.token_usage_by_model.values(), TokenTotals()).input == 100
    assert summary.token_usage_summary()["by_phase"] == [
        {"task_id": "extract-0", "phase": "extraction",
         "input": 100, "output": 10, "cache_read": 5, "cache_write": 2}
    ]


async def test_a_cancellation_shows_as_a_status_but_not_as_a_failure():
    # One real failure in a fan-out cancels every sibling; counting those as failures would report
    # the run as N broken tasks instead of the one that broke.
    summary = RunSummary()
    await _cancel_one_task(summary)

    out = summary.format()
    assert CANCELLED in out
    assert "Failures" not in out
