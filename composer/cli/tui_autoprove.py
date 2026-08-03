"""Entry point for the auto-prove multi-agent pipeline TUI."""

import asyncio
import logging

import composer.bind as _

from composer.diagnostics.timing import RunSummary
from composer.ui.autoprove_app import AutoProveApp
from composer.spec.source.autoprove_common import _entry_point

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> int:
    summary = RunSummary()
    async with _entry_point(summary) as pipeline:
        app = AutoProveApp()

        async def work():
            try:
                result = await pipeline(app.make_handler)
                msg = (
                    f"Auto-prove complete: {result.n_components} components, "
                    f"{result.n_properties} properties"
                )
                if result.failures:
                    msg += f", {len(result.failures)} failures"
                app.notify(msg)
                app.mark_pipeline_done()
            except Exception as exc:
                # A toast alone loses the failure the moment it fades — and the traceback with it.
                _log.exception("pipeline failed")
                app.notify(f"Pipeline failed: {exc}", severity="error")
                app.mark_pipeline_done()

        app.set_work(work)
        await app.run_async()
        print(summary.format())
        return 0

def main() -> int:
    return asyncio.run(_main())
