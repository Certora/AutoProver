"""
Shared signal handling for PreAudit orchestration.

Provides SIGINT (Ctrl+C) handling that cancels running cloud jobs,
generates partial reports, and exits cleanly.
"""

import atexit
import signal
import sys
from typing import Callable

from certora_autosetup.conf_runner import ConfRunner
from certora_autosetup.utils.logger import logger


COMPONENT = "SignalHandler"

# Module-level globals for signal handler access
_active_conf_runner: ConfRunner | None = None
_progress_display = None


def set_progress_display(display) -> None:
    """Set or clear the progress display (spinner) for cleanup on Ctrl+C.

    Called by preaudit to register the active spinner so that the signal
    handler can stop it before printing messages.
    """
    global _progress_display
    _progress_display = display


def _signal_handler(signum, frame) -> None:
    """Handle SIGINT (Ctrl+C) by cancelling running jobs and generating partial reports."""
    _ = signum, frame  # Suppress unused variable warnings

    # Stop the progress spinner before printing (clears the spinner line)
    if _progress_display is not None:
        _progress_display.stop(show_100=False)

    log_func = logger.log

    log_func("\nReceived interrupt signal (Ctrl+C)", "INFO", COMPONENT)
    if _active_conf_runner is not None:
        # First, cancel running jobs to save costs
        _active_conf_runner.cleanup_running_jobs()

        # Then generate partial reports for any completed jobs
        with _active_conf_runner.callbacks.job_results_lock:
            has_job_results = len(_active_conf_runner.callbacks.job_results) > 0

        if has_job_results:
            log_func("Generating partial report for completed jobs...", "INFO", COMPONENT)
            try:
                _active_conf_runner.generate_partial_reports()
                log_func("Partial report generated successfully", "INFO", COMPONENT)
            except Exception as e:
                log_func(f"Failed to generate partial report: {e}", "ERROR", COMPONENT)
        else:
            log_func("No completed jobs to report", "INFO", COMPONENT)

    log_func("Exiting...", "INFO", COMPONENT)
    sys.exit(1)


def register_signal_handler(
    conf_runner: ConfRunner,
    log_func: Callable | None = None,
) -> None:
    """Register signal handler and atexit cleanup for graceful shutdown.

    Args:
        conf_runner: The ConfRunner whose jobs should be cancelled on interrupt.
        log_func: Optional custom log function. Defaults to src.utils.logger.log.
            Not currently used by the handler (which always uses logger.log),
            but reserved for future customisation.
    """
    global _active_conf_runner
    _active_conf_runner = conf_runner

    signal.signal(signal.SIGINT, _signal_handler)
    atexit.register(conf_runner.cleanup_running_jobs)
