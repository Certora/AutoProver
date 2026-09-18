#!/usr/bin/env python3
"""
Shared early-stop infrastructure for cloud prover jobs.

While a cloud job is still RUNNING, the poll loop consults an ordered list of
``EarlyStopChecker``s once per tick. A checker inspects the job and may decide the wait
should end now, before the job reaches a terminal status. Each checker is a per-job instance
bound at construction to the job's URL and a ``ProverOutputAPI`` (so a job URL is all it needs
to fetch whatever it looks at — treeview, checks, ...) and keeps its own cross-tick state.

A checker returns an :class:`EarlyStop` decision (stop waiting, optionally cancel the cloud
job, with a reason) or ``None`` to keep waiting. The poll loop performs the cancellation and
the single result-building site turns the decision into a ``ProverResult``.

Two checkers live here today:
  * :class:`PreprocessingCheck`  — a job stuck in prover preprocessing (empty treeview past a
    budget) will never produce results; stop and cancel it.
  * :class:`FirstViolationCheck` — one VIOLATED rule already suffices to trigger a result
    revision, so stop instead of waiting for the remaining rules.
"""

import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Protocol

from prover_output_utility.models import JobStatus as ProverJobStatus  # type: ignore[import-untyped]

from .preprocessing_watchdog import PreprocessingWatchdog, WatchdogVerdict
from .runner_types import REASON_FIRST_VIOLATION, REASON_PREPROCESSING_TIMEOUT


@dataclass
class EarlyStop:
    """A checker's decision to stop waiting on a still-running job.

    Attributes:
        reason: Short machine tag identifying the stop condition, one of the ``REASON_*`` tags.
        cancel: Whether the poll loop cancels the cloud job; False stops waiting but leaves the
            job running on the cloud.
        drop_submission_cache: Whether to drop the cached submission so a deliberate future run
            submits a fresh job instead of re-attaching to this one.
        message: Human-readable one-liner for logs.
    """

    reason: str
    cancel: bool = True
    drop_submission_cache: bool = False
    message: str = ""


class EarlyStopChecker(Protocol):
    """A stateful, per-job early-stop condition.

    Construct one per job (bound to its URL + API), then feed it each poll tick via
    :meth:`observe`. It returns an :class:`EarlyStop` to end the wait, or ``None`` to keep
    waiting. ``observe`` runs inside a thread-pool executor, so it may block on network I/O.
    """

    def observe(self, job_info: Any) -> Optional[EarlyStop]:
        ...


class PreprocessingCheck:
    """EarlyStopChecker: stop a job stuck in prover preprocessing.

    A thin adapter over :class:`PreprocessingWatchdog` (the treeview state machine), bound to
    the job URL + API so it can probe the treeview itself.
    """

    def __init__(
        self,
        prover_api: Any,
        job_url: str,
        *,
        budget_seconds: float,
        probe_interval_seconds: float,
        log: Callable[..., None],
    ):
        self._budget_seconds = budget_seconds
        self._watchdog = PreprocessingWatchdog(
            budget_seconds=budget_seconds,
            probe_interval_seconds=probe_interval_seconds,
            probe_treeview=lambda: prover_api.get_treeview_status(job_url),
            log=log,
        )

    def observe(self, job_info: Any) -> Optional[EarlyStop]:
        is_running = getattr(job_info, "status", None) == ProverJobStatus.RUNNING
        if self._watchdog.observe(is_running) is WatchdogVerdict.PREPROCESSING_TIMEOUT:
            return EarlyStop(
                reason=REASON_PREPROCESSING_TIMEOUT,
                cancel=True,
                drop_submission_cache=True,
                message=(
                    f"prover reported no rules in its treeview within "
                    f"{self._budget_seconds:.0f}s of running (stuck in preprocessing)"
                ),
            )
        return None


class FirstViolationCheck:
    """EarlyStopChecker: stop the job the moment any rule is VIOLATED.

    One violation already suffices to trigger a result revision, so there is no value in
    waiting for the remaining rules. The result builder collects the (partial) rule results
    the standard way after the stop. The ``get_all_checks`` probe is rate-limited to
    ``probe_interval_seconds``; during preprocessing it returns nothing, so this needs no
    gating on the preprocessing check.
    """

    def __init__(
        self,
        prover_api: Any,
        job_url: str,
        *,
        probe_interval_seconds: float,
        log: Callable[..., None],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._prover_api = prover_api
        self._job_url = job_url
        self._probe_interval_seconds = probe_interval_seconds
        self._log = log
        self._clock = clock
        self._last_probe: Optional[float] = None

    def observe(self, job_info: Any) -> Optional[EarlyStop]:
        if getattr(job_info, "status", None) != ProverJobStatus.RUNNING:
            return None

        now = self._clock()
        if self._last_probe is not None and now - self._last_probe < self._probe_interval_seconds:
            return None
        self._last_probe = now

        try:
            all_checks: List[Any] = list(self._prover_api.get_all_checks(self._job_url) or [])
        except Exception as e:
            # An in-flight job's checks may be missing/partial — not an error, just try again.
            self._log(f"stop_on_first_violation: partial-check fetch failed for {self._job_url}: {e}", "DEBUG")
            return None

        violated = [c for c in all_checks if c.is_violated]
        if not violated:
            return None

        names = ", ".join(sorted({c.rule_name for c in violated})[:3])
        return EarlyStop(
            reason=REASON_FIRST_VIOLATION,
            cancel=True,
            message=f"{len(violated)} rule(s) VIOLATED ({names})",
        )
