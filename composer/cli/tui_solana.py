"""Entry point for the CVLR rule author (Solana) — TUI mode."""

import asyncio
import logging
import pathlib
from typing import cast

import composer.bind as _

from composer.diagnostics.timing import RunSummary
from composer.pipeline.ptypes import Curtailed, Delivered
from composer.rustapp.frontend import GenericRustApp
from composer.spec.cvlr.entry import CvlrPipelineResult, _entry_point
from composer.spec.cvlr.pipeline import CvlrPhase

_log = logging.getLogger(__name__)

#: ``PREFLIGHT`` is here rather than folded into analysis because it is a *concurrent* phase, not a
#: preceding one — the scaffold-and-compile gate shares a task group with system analysis, and a
#: reader watching one section stall wants to see which of the two it is.
CVLR_PHASE_LABELS: dict[CvlrPhase, str] = {
    CvlrPhase.DISCOVER_DESIGN_DOC: "Design Doc Discovery",
    CvlrPhase.ANALYSIS: "System Analysis",
    CvlrPhase.PREFLIGHT: "Preflight",
    CvlrPhase.EXTRACTION: "Property Extraction",
    CvlrPhase.FORMALIZATION: "Rule Authoring",
}

CVLR_SECTION_ORDER: list[str] = [
    "Design Doc Discovery",
    "System Analysis",
    "Preflight",
    "Property Extraction",
    "Rule Authoring",
]


def _delivered(result: CvlrPipelineResult) -> list[tuple[pathlib.Path, str | None]]:
    """Each published harness and its prover link. A budget-curtailed publish wraps a
    ``Delivered``, whose harness is on disk and worth naming."""
    out: list[tuple[pathlib.Path, str | None]] = []
    for outcome in result.outcomes:
        published = outcome.result
        if isinstance(published, Curtailed):
            published = published.partial
        if isinstance(published, Delivered):
            out.append((published.deliverable, published.result.final_link))
    return out


async def _main() -> int:
    summary = RunSummary()
    async with _entry_point(summary) as pipeline:
        app = GenericRustApp(
            phase_labels=cast(dict, CVLR_PHASE_LABELS),
            section_order=CVLR_SECTION_ORDER,
            header_text="CVLR Rule Author | ESC: summary | q: quit (when done)",
            event_kinds=set(),
        )
        result: CvlrPipelineResult | None = None

        async def work():
            nonlocal result
            try:
                result = await pipeline(app.make_handler)
                msg = (
                    f"CVLR authoring complete: {result.n_components} components, "
                    f"{result.n_properties} properties, {result.n_delivered} delivered"
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
        # The harnesses and prover links matter after the TUI is gone — echo them into terminal
        # scrollback the way console-solana does.
        if result is not None:
            for path, link in _delivered(result):
                print(f"  written: {path}")
                if link is not None:
                    print(f"           {link}")
            for f in result.failures:
                print(f"  FAILED: {f}")
        return 0


def main() -> int:
    return asyncio.run(_main())
