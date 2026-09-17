"""Tests for the bounded reap of the AutoSetup subprocess (``composer/spec/source/autosetup.py``).

The wait for the child sits in a ``finally``, which is also the path a cancelled run takes on its
way out. Whatever the child is doing, that wait has to end.
"""

import asyncio
import sys

import pytest

from composer.spec.source import autosetup

pytestmark = pytest.mark.asyncio


async def _spawn(code: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
    )


async def test_a_child_that_exits_reports_its_status():
    proc = await _spawn("raise SystemExit(3)")
    assert await autosetup._reap(proc) == 3


async def test_a_child_that_will_not_exit_is_killed(monkeypatch):
    monkeypatch.setattr(autosetup, "_REAP_TIMEOUT_S", 0.2)
    # Ignores SIGTERM, so only the kill ends it.
    proc = await _spawn(
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
    )
    returncode = await asyncio.wait_for(autosetup._reap(proc), timeout=10)
    assert returncode is not None and returncode < 0


async def test_the_reap_ends_inside_a_cancelled_task(monkeypatch):
    """The shape a run takes on its way out: the wait sits in a ``finally`` of a cancelled task.

    ``asyncio.timeout`` there has to raise ``TimeoutError`` off its own timer rather than pass the
    task's pending cancellation through, or the escalation never happens and the task never
    finishes unwinding.
    """
    monkeypatch.setattr(autosetup, "_REAP_TIMEOUT_S", 0.2)
    proc = await _spawn(
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
    )
    reaped: list[int | None] = []

    async def run_and_reap() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            reaped.append(await autosetup._reap(proc))

    task = asyncio.create_task(run_and_reap())
    await asyncio.sleep(0.05)
    task.cancel()

    _, pending = await asyncio.wait({task}, timeout=10)
    assert not pending, "the reap never returned inside a cancelled task"
    assert task.cancelled()
    assert reaped and reaped[0] is not None and reaped[0] < 0


async def test_the_reap_says_how_it_ended(caplog):
    proc = await _spawn("raise SystemExit(0)")
    with caplog.at_level("INFO", logger="composer.spec.source.autosetup"):
        assert await autosetup._reap(proc) == 0
    assert any("AutoSetup child exited 0" in r.getMessage() for r in caplog.records)
