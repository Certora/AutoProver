"""Tests for the run-level success/failure verdict of the shared pipeline result.

``CorePipelineResult.all_failed`` is the signal the autoprove/foundry entry points
translate into a non-zero exit code: a run in which *every* attempted component failed
to generate a deliverable — either it gave up (``GaveUp``) or it crashed
(``BaseException``) — is a total failure. As long as one component delivered, the run
succeeds regardless of how many others gave up.
"""
from pathlib import Path
from typing import Any, cast

from composer.pipeline.ptypes import (
    ComponentOutcome, CorePipelineResult, Delivered, GaveUp,
)
from composer.spec.system_model import ContractComponentInstance


def _delivered() -> Delivered:
    # all_failed / n_delivered only branch on isinstance(result, Delivered); the wrapped
    # BackendResult is never inspected, so a placeholder is enough.
    return Delivered(result=cast(Any, None), deliverable=Path("composer_c.spec"))


def _outcome(result: Delivered | GaveUp | BaseException) -> ComponentOutcome:
    return ComponentOutcome(feat=cast(ContractComponentInstance, None), props=[], result=result)


def _result(*results: Delivered | GaveUp | BaseException) -> CorePipelineResult:
    outcomes = [_outcome(r) for r in results]
    return CorePipelineResult(
        n_components=len(outcomes), n_properties=0, outcomes=outcomes, failures=[],
    )


def test_all_delivered_is_not_a_failure():
    r = _result(_delivered(), _delivered())
    assert r.n_delivered == 2
    assert r.all_failed is False


def test_one_delivered_among_giveups_is_not_a_failure():
    r = _result(_delivered(), GaveUp(reason="x"), Exception("boom"))
    assert r.n_delivered == 1
    assert r.all_failed is False


def test_all_gave_up_is_a_failure():
    r = _result(GaveUp(reason="a"), GaveUp(reason="b"))
    assert r.n_delivered == 0
    assert r.all_failed is True


def test_all_crashed_is_a_failure():
    r = _result(Exception("boom"), RuntimeError("bang"))
    assert r.n_delivered == 0
    assert r.all_failed is True


def test_giveup_and_crash_mix_with_no_delivery_is_a_failure():
    r = _result(GaveUp(reason="a"), Exception("boom"))
    assert r.all_failed is True


def test_empty_outcomes_is_not_reported_as_failure():
    # The driver raises before returning an empty outcome set; the guard keeps
    # "all of nothing" from being reported as a total failure regardless.
    r = _result()
    assert r.n_delivered == 0
    assert r.all_failed is False
