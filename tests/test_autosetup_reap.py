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
