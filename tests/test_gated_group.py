"""Tests for ``gated_group``, the shared primitive behind every concurrency point that shares one
fate: the first failure cancels the siblings, and the caller sees that failure itself.

Stubs only — no LLM, no DB, no backend wheel.
"""

import asyncio
import traceback

import pytest

from composer.io.multi_job import gated_group

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


async def test_a_failure_cancels_the_sibling_and_surfaces_itself():
    # The whole point of the gate: the sibling stops the moment the run is doomed, and the caller
    # gets the exception it would have got from a plain await, not a wrapper it has to unpack.
    boom = RuntimeError("one member failed")
    sibling = _Step(30, None, "value")

    with pytest.raises(RuntimeError) as caught:
        async with gated_group() as group:
            group.create_task(_Step(0.01, boom).run())
            group.create_task(sibling.run())

    assert caught.value is boom
    assert sibling.cancelled and not sibling.finished


async def test_a_double_failure_keeps_the_group():
    # Two members raise before either cancellation lands, so neither is "the" cause and both
    # exceptions reach the caller.
    first = RuntimeError("first member failed")
    second = RuntimeError("second member failed")

    with pytest.raises(BaseExceptionGroup) as caught:
        async with gated_group() as group:
            group.create_task(_Step(0, first).run())
            group.create_task(_Step(0, second).run())

    assert set(caught.value.exceptions) == {first, second}


async def test_cancelling_the_holder_cancels_the_members():
    # A caller that goes away (Ctrl-C, an enclosing timeout) takes the whole group with it.
    members = [_Step(30, None), _Step(30, None)]

    async def hold():
        async with gated_group() as group:
            for m in members:
                group.create_task(m.run())

    holder = asyncio.create_task(hold())
    await asyncio.gather(*(m.started.wait() for m in members))
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    assert all(m.cancelled and not m.finished for m in members)


async def test_the_surfaced_failure_keeps_its_cause():
    # A member that wraps its root cause is common — a wrapper's message is often only a pointer at
    # the cause it was raised from — so the chain has to survive the unwrap, and the group must not
    # appear in the rendered traceback either.
    root = ValueError("the root cause")

    async def wrap():
        try:
            raise root
        except ValueError as exc:
            raise RuntimeError("the wrapper") from exc

    with pytest.raises(RuntimeError) as caught:
        async with gated_group() as group:
            group.create_task(wrap())
            group.create_task(_Step(30, None).run())

    assert caught.value.__cause__ is root
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert "the root cause" in rendered
    assert "ExceptionGroup" not in rendered
