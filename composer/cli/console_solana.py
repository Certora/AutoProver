"""Entry point for the CVLR rule author (Solana) — console (no TUI) mode."""

import asyncio

import composer.bind as _

from composer.diagnostics.timing import RunSummary
from composer.pipeline.ptypes import Curtailed, Delivered
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.spec.cvlr.entry import _entry_point


async def _main() -> int:
    summary = RunSummary()
    async with _entry_point(summary) as run:
        result = await run(GenericRustConsoleHandler(set()).make_handler)
        print(f"\n{'=' * 60}")
        print(summary.format())
        print(f"\n  Components:    {result.n_components}")
        print(f"  Properties:    {result.n_properties}")
        print(f"  Delivered:     {result.n_delivered}")
        for outcome in result.outcomes:
            # A budget-curtailed publish wraps a ``Delivered``, so the harness it did write is
            # still on disk and still worth naming.
            published = outcome.result
            if isinstance(published, Curtailed):
                published = published.partial
            if not isinstance(published, Delivered):
                continue
            print(f"    - {published.deliverable}")
            if (link := published.result.final_link) is not None:
                print(f"      {link}")
        if result.failures:
            print(f"  Failures:      {len(result.failures)}")
            for f in result.failures:
                print(f"    - {f}")
        print(f"{'=' * 60}")
        if result.all_failed:
            print("  RUN FAILED: every component failed to generate or gave up.")
            return 1
        return 0


def main() -> int:
    return asyncio.run(_main())
