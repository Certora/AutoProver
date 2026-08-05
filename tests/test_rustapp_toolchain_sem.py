"""The blocking-callout guard in ``composer.rustapp.adapter._run_blocking``.

``compile``/``validate`` are synchronous wheel calls that spawn a toolchain (``run-confined``), so
they run in a worker thread. A wheel that declares ``serialize_toolchain`` shares one workdir /
cargo target across its units, and then those calls must not overlap — the semaphore is the only
thing keeping two concurrent components out of the same build directory.
"""

import asyncio
import time

import pytest

from composer.rustapp.adapter import _run_blocking


@pytest.mark.asyncio
async def test_without_a_semaphore_the_call_just_runs_off_the_loop():
    assert await _run_blocking(lambda: "out", None) == "out"


@pytest.mark.asyncio
async def test_the_semaphore_is_released_so_later_calls_are_not_blocked():
    sem = asyncio.Semaphore(1)
    assert await _run_blocking(lambda: "a", sem) == "a"
    # Would hang here if the guard leaked the permit.
    assert await _run_blocking(lambda: "b", sem) == "b"
    assert not sem.locked()


@pytest.mark.asyncio
async def test_the_semaphore_keeps_concurrent_calls_out_of_the_shared_workdir():
    sem = asyncio.Semaphore(1)
    live = 0
    peak = 0

    def thunk() -> str:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        time.sleep(0.01)  # long enough that unguarded threads would overlap
        live -= 1
        return "done"

    results = await asyncio.gather(*(_run_blocking(thunk, sem) for _ in range(4)))
    assert results == ["done"] * 4
    assert peak == 1


@pytest.mark.asyncio
async def test_a_raising_callout_does_not_leave_the_semaphore_held():
    sem = asyncio.Semaphore(1)

    def boom() -> str:
        raise RuntimeError("toolchain died")

    with pytest.raises(RuntimeError, match="toolchain died"):
        await _run_blocking(boom, sem)
    assert not sem.locked()
