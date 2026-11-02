"""Prover callbacks that record each run in the active run summary."""

import time
from typing import override

from composer.diagnostics.timing import get_run_summary
from composer.prover.core import ProverCallbacks
from composer.prover.ptypes import RuleResult


class RunAccounting(ProverCallbacks):
    """Records each prover run's link, queue-free runtime, and wall clock.

    ``summary.format()``, the per-task link, and ``job_info.json``'s
    ``prover_usage`` read these records. Every override calls ``super()``, so
    this combines with other callbacks through multiple inheritance.

    A run whose ``on_prover_run`` never fired records no wall clock.
    """

    _started_mono: float | None = None

    @property
    def elapsed(self) -> float | None:
        """Seconds since the running prover started, or ``None`` outside a run."""
        if self._started_mono is None:
            return None
        return time.perf_counter() - self._started_mono

    @override
    async def on_prover_run(self, args: list[str]) -> None:
        self._started_mono = time.perf_counter()
        await super().on_prover_run(args)

    @override
    async def on_prover_link(self, link: str) -> None:
        get_run_summary().record_prover_link(link)
        await super().on_prover_link(link)

    @override
    async def on_prover_runtime(self, ms: int) -> None:
        """The prover's start-to-end time, without queue wait.

        The wall clock in :meth:`on_prover_result` includes time spent waiting
        in the queue.
        """
        get_run_summary().record_prover_runtime(ms)
        await super().on_prover_runtime(ms)

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        if (elapsed := self.elapsed) is not None:
            get_run_summary().add_prover_call(elapsed)
            self._started_mono = None
        await super().on_prover_result(results)
