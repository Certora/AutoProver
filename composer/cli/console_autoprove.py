"""Entry point for the auto-prove pipeline — console (no TUI) mode."""

import asyncio

import composer.bind as _

from composer.diagnostics.timing import RunSummary
from composer.ui.autoprove_console import AutoProveConsoleHandler
from composer.spec.source.autoprove_common import _entry_point


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> int:
    summary = RunSummary()
    async with _entry_point(summary) as launch:
        # Headless (--mailbox): questions, acks and refinement turns go through the
        # run's mailbox; the console output below is the job log.
        handler = (
            AutoProveConsoleHandler.over_mailbox(launch.headless.mailbox, launch.headless.warm)
            if launch.headless is not None else AutoProveConsoleHandler()
        )
        result = await launch(handler.make_handler)
        print(f"\n{'=' * 60}")
        print(summary.format())
        print(f"\n  Components:  {result.n_components}")
        print(f"  Properties:  {result.n_properties}")
        if result.failures:
            print(f"  Failures:    {len(result.failures)}")
            for f in result.failures:
                print(f"    - {f}")
        if result.awaiting_input:
            print(f"  Awaiting input: {len(result.awaiting_input)}")
            for a in result.awaiting_input:
                print(f"    - {a}")
        if result.stalled:
            print(f"  Stalled:     {len(result.stalled)}")
            for s in result.stalled:
                print(f"    - {s}")
        print(f"{'=' * 60}")
        if result.all_failed:
            print("  RUN FAILED: every component failed to generate or gave up.")
            return 1
        if result.unfinished:
            print("  RUN SUSPENDED: resume it once the questions above are answered.")
        return 0


def main() -> int:
    return asyncio.run(_main())

