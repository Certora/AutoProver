"""Entry point for the auto-prove pipeline — console (no TUI) mode."""

import asyncio
import logging

import composer.bind as _

from composer.diagnostics.timing import RunSummary
from composer.ui.autoprove_console import AutoProveConsoleHandler
from composer.spec.source.autoprove_common import _entry_point

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> int:
    summary = RunSummary()
    async with _entry_point(summary) as run:
        result = await run(AutoProveConsoleHandler().make_handler)
        print(f"\n{'=' * 60}")
        print(summary.format())
        print(f"\n  Components:  {result.n_components}")
        print(f"  Properties:  {result.n_properties}")
        if result.failures:
            print(f"  Failures:    {len(result.failures)}")
            for f in result.failures:
                print(f"    - {f}")
        print(f"{'=' * 60}")
        if result.all_failed:
            print("  RUN FAILED: every component failed to generate or gave up.")
            return 1
        return 0


def _name_pending(loop: asyncio.AbstractEventLoop) -> None:
    """Say what the loop still holds before it is asked to close.

    Closing cancels whatever is left and then waits for all of it, with no bound. Anything named
    here is something that wait can be spent on.
    """
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if pending:
        _logger.info(
            "%d task(s) still running at shutdown: %s",
            len(pending),
            ", ".join(sorted(t.get_name() for t in pending)),
        )


def main() -> int:
    runner = asyncio.Runner()
    try:
        try:
            return runner.run(_main())
        finally:
            _logger.info("pipeline returned; shutting the event loop down")
            _name_pending(runner.get_loop())
    finally:
        runner.close()
        _logger.info("event loop closed")

