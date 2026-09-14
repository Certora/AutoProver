"""A wedged prover subprocess must not hang the run, and must not leak its JVM.

`certoraRun` is a python front end that shells out to a JVM for the build and
type-check phase. Every await on that chain used to be unbounded, so when the JVM
stopped responding the whole run stopped with it: the caller waited on
certoraRun, certoraRun waited on the JVM, and the JVM waited on a lock it never
got. One observed run sat that way for three days on a second of CPU.

Two properties matter, and the second is the one that is easy to get wrong:
the wait has to end, and the kill has to reach the grandchild. Signalling only
the direct child leaves the JVM holding the pipe its parent was reading.
"""

import asyncio
import os
import signal

import pytest

from composer.prover.core import ProverSubprocessTimeout, _bounded_subprocess

pytestmark = pytest.mark.asyncio


# Every test here bounds itself. A regression in the group kill makes the helper
# hang rather than fail -- the orphaned grandchild holds the pipe its parent is
# read-waiting on -- and a test guarding against a hang must not hang CI.
_TEST_DEADLINE_S = 30


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_a_hung_subprocess_raises_instead_of_hanging(tmp_path) -> None:
    async with asyncio.timeout(_TEST_DEADLINE_S):
        with pytest.raises(ProverSubprocessTimeout, match="exceeded"):
            async with _bounded_subprocess(
                "sleep", "300", cwd=str(tmp_path), timeout=0.2
            ) as proc:
                await proc.wait()


async def test_the_grandchild_is_killed_too(tmp_path) -> None:
    """Stands in for the JVM that certoraRun spawns."""
    # The shell prints its child's pid, then outlives the timeout.
    script = "sleep 300 & echo $! ; wait"
    async with asyncio.timeout(_TEST_DEADLINE_S):
        with pytest.raises(ProverSubprocessTimeout):
            async with _bounded_subprocess(
                "sh", "-c", script,
                cwd=str(tmp_path),
                timeout=1.0,
                stdout=asyncio.subprocess.PIPE,
            ) as proc:
                assert proc.stdout is not None
                grandchild = int((await proc.stdout.readline()).decode().strip())
                assert _alive(grandchild), "grandchild should be running before the timeout"
                await proc.wait()

    # The group signal has to have reached it. It exits asynchronously, so allow
    # a moment rather than asserting on the instant the exception surfaces.
    for _ in range(50):
        if not _alive(grandchild):
            break
        await asyncio.sleep(0.1)
    assert not _alive(grandchild), "grandchild survived the timeout — killpg did not reach it"


async def test_a_subprocess_that_finishes_is_left_alone(tmp_path) -> None:
    async with _bounded_subprocess("true", cwd=str(tmp_path), timeout=30) as proc:
        assert await proc.wait() == 0


async def test_the_child_leads_its_own_process_group(tmp_path) -> None:
    """`process_group=0` is what makes the group kill reach a grandchild; if a
    refactor drops it, the child shares our group and killpg would signal the
    test runner instead."""
    async with _bounded_subprocess(
        "sh", "-c", "sleep 30", cwd=str(tmp_path), timeout=30
    ) as proc:
        assert os.getpgid(proc.pid) == proc.pid
        assert os.getpgid(proc.pid) != os.getpgid(0)
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
