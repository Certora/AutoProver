"""
Progress display for PreAudit's signal collection phase.

Shows a tqdm progress bar with active job count, elapsed time,
and time-budget-based progress percentage.

Supports an optional ProgressReporter for SaaS mode (S3-based progress).
"""

import os
import sys
import threading
import time
from typing import Callable

from tqdm import tqdm

from certora_autosetup.utils.constants import ALL_LOGS_IN_STDOUT_ENV

# Refresh cap (seconds) for tqdm bars when all logs go to stdout (cloud mode). Without
# this, tqdm redraws every ~0.1s and floods CloudWatch with near-identical lines.
CLOUD_TQDM_MININTERVAL = 30.0


def make_tqdm(*args, **kwargs):
    """``tqdm(...)`` that throttles to one refresh per 30s when logs go to stdout (cloud).

    A drop-in replacement for ``tqdm(...)`` for ``.update()``-driven bars. In cloud
    logging mode (``PREAUDIT_ALL_LOGS_IN_STDOUT`` set) it caps the refresh rate so the
    bar prints at most once every 30s instead of on every iteration; locally it behaves
    exactly like ``tqdm``. Callers may still override ``mininterval``/``maxinterval``.
    """
    if ALL_LOGS_IN_STDOUT_ENV in os.environ:
        kwargs.setdefault("mininterval", CLOUD_TQDM_MININTERVAL)
        kwargs.setdefault("maxinterval", CLOUD_TQDM_MININTERVAL)
    return tqdm(*args, **kwargs)


class CollectingSignalsProgress:
    """tqdm-based progress display during preaudit execution.

    Display (two lines):
        Collecting signals:  12%|████████████████░░░░░░░░░░░░░░░░|
          5 active, 3 done | elapsed 18:30 / budget 450:00

    Progress is based on elapsed time vs a total time budget.
    Once preaudit finishes, progress jumps to 100%.

    When a ``progress_reporter`` is provided (SaaS mode), updates are
    forwarded to that reporter in addition to the tqdm display.
    """

    REFRESH_INTERVAL = 0.5  # seconds

    def __init__(
        self,
        time_budget_seconds: float,
        get_active_count: Callable[[], int],
        get_completed_count: Callable[[], int],
        progress_reporter=None,
    ):
        self._time_budget = time_budget_seconds
        self._get_active = get_active_count
        self._get_completed = get_completed_count
        self._progress_reporter = progress_reporter
        self._start_time = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished = False
        self._bar: tqdm | None = None  # type: ignore[type-arg]
        self._stats: tqdm | None = None  # type: ignore[type-arg]
        # Cloud logging mode: stdout is piped (CloudWatch), not a TTY. An animated tqdm
        # bar that redraws every 0.5s becomes one log line per refresh, so instead we
        # emit a single line only when the integer percentage changes.
        self._quiet = ALL_LOGS_IN_STDOUT_ENV in os.environ
        self._last_logged_pct = -1

    def start(self) -> None:
        """Start the progress display in a background thread."""
        self._start_time = time.time()
        self._stop_event.clear()
        self._finished = False
        # In cloud mode, skip the tqdm bars entirely — _render() logs on percent change.
        if not self._quiet:
            self._bar = tqdm(
                total=100,
                desc="Collecting signals",
                bar_format="{desc}: {percentage:3.0f}%|{bar}|",
                position=1,
                leave=False,
                file=sys.stderr,
            )
            self._stats = tqdm(
                total=0,
                bar_format="  {desc}",
                position=0,
                leave=False,
                file=sys.stderr,
            )
        if self._progress_reporter is not None:
            self._progress_reporter.start_phase(
                phase="collecting_signals",
                phase_display="Collecting signals",
                total=100,
                time_budget_seconds=self._time_budget,
            )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, show_100: bool = True) -> None:
        """Stop the progress display. Optionally show 100% completion line."""
        if self._finished:
            return
        self._finished = True
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._bar:
            self._bar.close()
        if self._stats:
            self._stats.close()
        if show_100:
            elapsed = time.time() - self._start_time
            completed = self._get_completed()
            sys.stderr.write(f"Collecting signals: 100% | {completed} runs completed | elapsed {self._format_time(elapsed)}\n")
            sys.stderr.flush()
        if self._progress_reporter is not None:
            self._progress_reporter.finish_phase()

    def _run(self) -> None:
        """Background thread: update the display periodically."""
        # In cloud mode the thread only needs to wake every 30s — there is no animated
        # bar to keep smooth, just the occasional percent-change log line.
        interval = CLOUD_TQDM_MININTERVAL if self._quiet else self.REFRESH_INTERVAL
        while not self._stop_event.is_set():
            self._render()
            self._stop_event.wait(interval)

    def _render(self) -> None:
        """Render one frame of the progress display."""
        elapsed = time.time() - self._start_time
        pct = min(99, int(100 * elapsed / self._time_budget)) if self._time_budget > 0 else 0
        active = self._get_active()
        completed = self._get_completed()

        if self._quiet:
            # Cloud mode: one line, only when the integer percentage actually moves.
            if pct != self._last_logged_pct:
                sys.stderr.write(
                    f"Collecting signals: {pct}% | {active} active, {completed} done | "
                    f"elapsed {self._format_time(elapsed)} / budget {self._format_time(self._time_budget)}\n"
                )
                sys.stderr.flush()
                self._last_logged_pct = pct
        else:
            if self._bar:
                self._bar.n = pct
                self._bar.refresh()
            if self._stats:
                self._stats.set_description_str(
                    f"{active} active, {completed} done | elapsed {self._format_time(elapsed)} / budget {self._format_time(self._time_budget)}"
                )
                self._stats.refresh()
        if self._progress_reporter is not None:
            self._progress_reporter.update(completed=completed, active=active)

    @staticmethod
    def _format_time(seconds: float) -> str:
        """Format seconds as MM:SS."""
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"
