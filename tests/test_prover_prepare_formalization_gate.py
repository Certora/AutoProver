"""Tests for the prover's pre-formalization fan-out (``ProverPrepared.prepare_formalization``).

Setup and structural-invariant formulation run concurrently and gate each other: every spec is
written against the setup's config, so neither side outlives the other's failure.

Stubs only — the two sides are monkeypatched, and execution never reaches past the fan-out on
either path, so no run, no LLM and no prover are needed.
"""

import asyncio
from typing import Any, cast

import pytest

from composer.spec.source.pipeline import ProverPrepared

pytestmark = pytest.mark.asyncio


class _Step:
    """A stub async step that takes ``delay`` seconds and then fails or returns ``value``."""

    def __init__(self, delay: float, error: Exception | None, value: object = None):
        self.delay, self.error, self.value = delay, error, value
        self.finished = False
        self.cancelled = False
        #: Set once the step is running, so a test that cancels mid-flight can wait for the
        #: thing it means to cancel instead of guessing how long the driver takes to get there.
        self.started = asyncio.Event()

    async def run(self):
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        self.finished = True
        return self.value


def _prepared() -> ProverPrepared:
    """A ``ProverPrepared`` whose fields are all unreachable: both sides of the fan-out are
    replaced, and nothing below it runs."""
    none = cast(Any, None)
    return ProverPrepared(
        main=none, _sys_desc=none, _harnessed=none, _prover_tool=none, _analyzed=none, _deps=none,
    )


async def _fan_out(monkeypatch, *, setup: _Step, invariants: _Step) -> BaseException:
    """Run the fan-out with both sides stubbed; hand back what it raised."""
    prepared = _prepared()

    # Patched on the class, so each stub takes the instance the driver calls it on.
    async def fake_autosetup(_self, _run):
        return await setup.run()

    async def fake_invariants(_self, _run):
        return await invariants.run()

    monkeypatch.setattr(ProverPrepared, "_autosetup", fake_autosetup)
    monkeypatch.setattr(ProverPrepared, "_invariants", fake_invariants)
    with pytest.raises(BaseException) as caught:  # noqa: PT011 — the outcome under test
        await prepared.prepare_formalization(cast(Any, None))
    return caught.value


async def test_a_setup_failure_cancels_the_invariants(monkeypatch):
    # The invariants agent has nowhere to deliver once the setup is dead, so it stops there instead
    # of spending out its own run — and the caller sees the setup's own error, not a group.
    boom = RuntimeError("the setup failed")
    invariants = _Step(30, None)

    raised = await _fan_out(monkeypatch, setup=_Step(0.01, boom), invariants=invariants)

    assert raised is boom
    assert invariants.cancelled and not invariants.finished


async def test_an_invariants_failure_cancels_the_setup(monkeypatch):
    # Symmetric: the setup builds a config the failed side's specs would have been written against.
    boom = RuntimeError("invariant formulation failed")
    setup = _Step(30, None)

    raised = await _fan_out(monkeypatch, setup=setup, invariants=_Step(0.01, boom))

    assert raised is boom
    assert setup.cancelled and not setup.finished
