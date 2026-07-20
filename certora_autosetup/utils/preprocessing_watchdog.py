#!/usr/bin/env python3
"""
Preprocessing watchdog for cloud prover jobs.

The prover's treeViewStatus.json carries an empty "rules" list for as long as the run is
in preprocessing (scene construction, storage/pointer analyses, CVL typechecking) — rules
appear in it only once rule checking begins. (Verified live: a stuck-preprocessing cloud
job serves a treeview with `rules: []`; the file itself exists early, so mere existence is
NOT the signal.) A job whose preprocessing blows up sits in RUNNING with an empty rule list
until the prover's global timeout (~2h), and autosetup would otherwise burn its whole
per-job wait budget polling it.

The watchdog piggy-backs on the existing status-poll loop: it starts a clock on the first
RUNNING observation (queue time is free), and after a grace period probes the treeview at a
bounded cadence. First probe showing a non-empty "rules" list ⇒ preprocessing passed,
watchdog goes dormant. No rules within the budget ⇒ PREPROCESSING_TIMEOUT, and the caller
cancels the job. Probe transport errors self-disable the watchdog so the loop degrades to
the old behavior; a JobNotFoundError is NOT an error — it is the normal "treeview not
served yet" response early in a run.
"""

import time
from enum import Enum
from typing import Callable, Optional

from prover_output_utility.exceptions import JobNotFoundError  # type: ignore[import-untyped]


class WatchdogVerdict(Enum):
    WAITING = "waiting"                            # keep polling normally
    PREPROCESSING_DONE = "preprocessing_done"      # treeview seen; dormant forever
    PREPROCESSING_TIMEOUT = "preprocessing_timeout"  # budget exceeded; caller should cancel
    DISABLED = "disabled"                          # probe endpoint unusable; old behavior


class PreprocessingWatchdog:
    """Rate-limited treeview prober; synchronous so it can run inside the poll executor."""

    def __init__(
        self,
        budget_seconds: float,
        grace_seconds: float,
        probe_interval_seconds: float,
        probe_treeview: Callable[[], object],
        log: Callable[..., None],
        max_consecutive_probe_errors: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.budget_seconds = budget_seconds
        self.grace_seconds = grace_seconds
        self.probe_interval_seconds = probe_interval_seconds
        self._probe_treeview = probe_treeview
        self._log = log
        self._max_consecutive_probe_errors = max_consecutive_probe_errors
        self._clock = clock

        self._running_since: Optional[float] = None
        self._last_probe: Optional[float] = None
        self._done = False
        self._disabled = False
        self._consecutive_probe_errors = 0

    def observe(self, is_running: bool) -> WatchdogVerdict:
        """Feed one status-poll tick. `is_running` is True only for RUNNING status —
        queued/posted/starting ticks never start the preprocessing clock."""
        if self._done:
            return WatchdogVerdict.PREPROCESSING_DONE
        if self._disabled:
            return WatchdogVerdict.DISABLED
        if not is_running:
            return WatchdogVerdict.WAITING

        now = self._clock()
        if self._running_since is None:
            self._running_since = now

        running_for = now - self._running_since
        if running_for < self.grace_seconds:
            return WatchdogVerdict.WAITING
        if self._last_probe is not None and now - self._last_probe < self.probe_interval_seconds:
            # Between probes; the budget check only fires on probe ticks so the final
            # verdict is always backed by a fresh "still no treeview" observation.
            return WatchdogVerdict.WAITING

        self._last_probe = now
        try:
            tree_data = self._probe_treeview()
        except JobNotFoundError:
            # Treeview not served yet — expected early in a run.
            self._consecutive_probe_errors = 0
            tree_data = None
        except Exception as e:
            self._consecutive_probe_errors += 1
            self._log(
                f"Preprocessing watchdog: treeview probe failed "
                f"({self._consecutive_probe_errors}/{self._max_consecutive_probe_errors}): {e}",
                "WARNING",
            )
            if self._consecutive_probe_errors >= self._max_consecutive_probe_errors:
                self._disabled = True
                self._log(
                    "Preprocessing watchdog disabled after repeated probe failures; "
                    "falling back to plain status polling",
                    "WARNING",
                )
                return WatchdogVerdict.DISABLED
            return WatchdogVerdict.WAITING

        # The treeview exists (with rules: []) throughout preprocessing; only a
        # non-empty rules list proves rule checking has started.
        if isinstance(tree_data, dict) and tree_data.get("rules"):
            self._done = True
            return WatchdogVerdict.PREPROCESSING_DONE

        if running_for > self.budget_seconds:
            return WatchdogVerdict.PREPROCESSING_TIMEOUT
        return WatchdogVerdict.WAITING
