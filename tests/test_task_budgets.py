"""A run has two concurrency budgets, and a task spends exactly one of them: ``runner`` charges the
agent semaphore (``--max-concurrent``), ``cpu_runner`` the CPU semaphore (``--max-cpu-tasks``)."""

import asyncio
import enum
from typing import Any, cast

import pytest

from composer.io.multi_job import TaskInfo
from composer.pipeline.ptypes import TaskRunnerHost
from composer.rustapp.frontend import GenericRustConsoleHandler

pytestmark = pytest.mark.asyncio


class _Phase(enum.Enum):
    WORK = "work"


def _host(*, agents: int, cpus: int) -> TaskRunnerHost[_Phase, Any, Any, Any]:
    return TaskRunnerHost(
        ctx=cast(Any, None),
        source=cast(Any, None),
        _handler_factory=GenericRustConsoleHandler(set()).make_handler,
        _agent_semaphore=asyncio.Semaphore(agents),
        _cpu_semaphore=asyncio.Semaphore(cpus),
    )


class _Peak:
    """Counts how many jobs were ever in flight at once."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0

    async def job(self) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)
        # Two hops, so a waiting task gets every chance to slip in if nothing is holding it back.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.live -= 1


def _info(n: int) -> TaskInfo[_Phase]:
    return TaskInfo(task_id=f"task-{n}", label=f"Task {n}", phase=_Phase.WORK)


async def test_cpu_tasks_are_bounded_by_the_cpu_budget():
    host, peak = _host(agents=8, cpus=2), _Peak()

    await asyncio.gather(*(host.cpu_runner(_info(n), peak.job) for n in range(6)))

    assert peak.peak == 2


async def test_a_cpu_task_does_not_spend_an_agent_slot():
    # The point of the split: a build saturating the CPU budget leaves the agents' concurrency
    # exactly as wide as the user asked for.
    host = _host(agents=2, cpus=1)
    agents, builds = _Peak(), _Peak()

    await asyncio.gather(
        *(host.cpu_runner(_info(n), builds.job) for n in range(4)),
        *(host.runner(_info(10 + n), agents.job) for n in range(4)),
    )

    assert builds.peak == 1
    assert agents.peak == 2
