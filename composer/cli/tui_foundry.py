"""Entry point for the foundry test-generation pipeline TUI."""

import asyncio
from typing import cast
import pathlib

import composer.bind as _

from composer.diagnostics.timing import RunSummary
from composer.foundry.entry import _entry_point
from composer.foundry.pipeline import FoundryPipelineResult
from composer.ui.foundry_app import FoundryApp
from composer.pipeline.ptypes import Delivered

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _main() -> int:
    summary = RunSummary()
    async with _entry_point(summary) as pipeline:
        app = FoundryApp()
        result: FoundryPipelineResult | None = cast(FoundryPipelineResult | None, None)
        written: list[pathlib.Path] | None = cast(list[pathlib.Path] | None, None)

        async def work():
            nonlocal result
            nonlocal written
            try:
                result = await pipeline(app.make_handler)
                written = [d.result.deliverable for d in result.outcomes if isinstance(d.result, Delivered)]
                msg = (
                    f"Foundry tests complete: {result.n_components} components, "
                    f"{result.n_properties} properties, "
                    f"{len(written)} files written"
                )
                if result.failures:
                    msg += f", {len(result.failures)} failures"
                app.notify(msg)
                app._pipeline_done = True
            except Exception as exc:
                app.notify(f"Pipeline failed: {exc}", severity="error")
                app._pipeline_done = True

        app.set_work(work)
        await app.run_async()
        print(summary.format())
        # The written paths / failures matter after the TUI is gone — echo
        # them into terminal scrollback the way console-foundry does.
        if result is not None:
            assert written is not None
            for p in written:
                print(f"  written: {p}")
            for f in result.failures:
                print(f"  FAILED: {f}")
        return 0


def main() -> int:
    return asyncio.run(_main())
