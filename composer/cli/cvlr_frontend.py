"""The CVLR rule author's two frontends, console and TUI, for whichever chain a script names.

``console-solana`` / ``tui-solana`` and ``console-soroban`` / ``tui-soroban`` are these two
functions with a :class:`~composer.spec.cvlr.chains.CvlrChain` bound. The chain is the script's
choice rather than a flag, because it decides the argument the script takes — a Solana program or a
Soroban contract — and a flag would make one parser describe both.
"""

import logging
import pathlib
from typing import Any, cast

import composer.bind as _

from composer.diagnostics.timing import RunSummary
from composer.pipeline.ptypes import Curtailed, Delivered
from composer.rustapp.frontend import GenericRustApp, GenericRustConsoleHandler
from composer.spec.cvlr.chains import CvlrChain
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


async def run_console(chain: CvlrChain[Any, Any, Any]) -> int:
    summary = RunSummary()
    async with _entry_point(summary, chain) as run:
        result = await run(GenericRustConsoleHandler(set()).make_handler)
        print(f"\n{'=' * 60}")
        print(summary.format())
        print(f"\n  Components:    {result.n_components}")
        print(f"  Properties:    {result.n_properties}")
        print(f"  Delivered:     {result.n_delivered}")
        for path, link in _delivered(result):
            print(f"    - {path}")
            if link is not None:
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


async def run_tui(chain: CvlrChain[Any, Any, Any]) -> int:
    summary = RunSummary()
    async with _entry_point(summary, chain) as pipeline:
        app = GenericRustApp(
            phase_labels=cast(dict, CVLR_PHASE_LABELS),
            section_order=CVLR_SECTION_ORDER,
            header_text=f"CVLR Rule Author ({chain.tag}) | ESC: summary | q: quit (when done)",
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
        # scrollback the way the console frontend does.
        if result is not None:
            for path, link in _delivered(result):
                print(f"  written: {path}")
                if link is not None:
                    print(f"           {link}")
            for f in result.failures:
                print(f"  FAILED: {f}")
        return 0
