"""What the driver does when property extraction fails for some components but not others.

Extraction fans out one task per component and the units are independent, so a component that
raises should cost that component and nothing else. The run carries on with the survivors and
reports the failure the way ``_tally`` reports a formalization failure.

Cancellation and the budget stops are not component failures — they end the run, and swallowing
one here would turn a deliberate halt into a quiet partial result.

Reference incident: a single mid-stream Anthropic 500 in one of five components discarded two
finished extractions and two still in flight.
"""

import asyncio

import pytest

import composer.pipeline.core as core
from composer.diagnostics.budget import BudgetExceeded, BudgetPressureAbort
from composer.pipeline.core import _Batch, _settle_extraction, run_pipeline

# The driver stubs live with the overlap tests; reuse them rather than restating a whole backend.
from tests.test_pipeline_overlap import ECOSYSTEM, _Backend, _Prepared, _Run, _Step


class _Unit:
    """Only the field the failure rollup reads."""

    def __init__(self, name: str):
        self.display_name = name


def _batch(unit: _Unit) -> _Batch:
    # _settle_extraction only partitions; it never looks inside a batch.
    return _Batch(unit, [], None)  # type: ignore[arg-type]


def test_a_failed_component_does_not_cost_the_others():
    good, bad, also_good = _Unit("good"), _Unit("bad"), _Unit("also-good")
    boom = RuntimeError("Internal server error")

    batches, failed = _settle_extraction(
        [good, bad, also_good],
        [_batch(good), boom, _batch(also_good)],  # type: ignore[list-item]
    )

    assert [b.feat for b in batches] == [good, also_good]
    assert failed == [(bad, boom)]


def test_a_component_with_no_properties_is_not_a_failure():
    # `_one` returns None for a component that yielded nothing to formalize. That is an empty
    # result, not an error, and it is dropped exactly as it was before.
    quiet = _Unit("quiet")
    batches, failed = _settle_extraction([quiet], [None])
    assert batches == []
    assert failed == []


def test_every_component_failing_is_reported_not_hidden():
    one, two = _Unit("one"), _Unit("two")
    batches, failed = _settle_extraction(
        [one, two], [RuntimeError("a"), RuntimeError("b")]  # type: ignore[list-item]
    )
    assert batches == []
    assert [u for u, _ in failed] == [one, two]


@pytest.mark.parametrize(
    "signal",
    [asyncio.CancelledError(), BudgetExceeded("out of budget"), BudgetPressureAbort()],
    ids=["cancelled", "budget-exceeded", "budget-pressure"],
)
def test_run_wide_stops_still_propagate(signal: BaseException):
    unit = _Unit("unit")
    with pytest.raises(type(signal)):
        _settle_extraction([unit], [signal])


def test_a_run_wide_stop_wins_over_a_component_failure():
    # The stop ends the run whichever order the tasks settled in.
    first, second = _Unit("first"), _Unit("second")
    with pytest.raises(BudgetExceeded):
        _settle_extraction(
            [first, second],
            [RuntimeError("component"), BudgetExceeded("out of budget")],  # type: ignore[list-item]
        )


@pytest.mark.asyncio
async def test_a_total_extraction_wipeout_names_the_failures(monkeypatch):
    """"Nothing was extracted" on its own hides the reason. When every component raised, the
    error the operator sees should say so and name them."""
    lost = _Unit("Signature Verification")

    async def fake_extract_all(*_a, **_kw):
        return [], [(lost, RuntimeError("Internal server error"))]

    async def fake_analysis(*_a, **_kw):
        return "analyzed"

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    backend = _Backend(_Prepared(_Step(0, None, "formalizer")))
    with pytest.raises(ValueError) as caught:
        await run_pipeline(backend, _Run(), max_bug_rounds=1, ecosystem=ECOSYSTEM)  # type: ignore[arg-type]

    message = str(caught.value)
    assert "Signature Verification" in message
    assert "Internal server error" in message
