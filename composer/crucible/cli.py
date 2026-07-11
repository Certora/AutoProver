"""``main()`` glue for the Crucible application.

Builds one :class:`~composer.rustapp.host.RustApplication` (shared phase enum for
the frontend + pipeline), injects the Crucible env + the sBPF pre-build via
``run_pipeline_fn``, then drives either the console handler (``console-crucible``)
or the Textual TUI (``tui-crucible``).

``import composer.bind`` runs first (import-time DI / test-tape bootstrap).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import composer.bind as _  # noqa: F401  (side-effecting DI/tape bootstrap; must load first)

from composer.crucible.pipeline import (
    build_crucible_application,
    build_crucible_env,
    run_crucible_pipeline,
)
from composer.crucible.results import format_verdict_lines, summarize_verdicts
from composer.diagnostics.timing import RunSummary
from composer.pipeline.core import CorePipelineResult
from composer.rustapp.entry import rust_entry_point
from composer.rustapp.frontend import GenericRustApp, GenericRustConsoleHandler
from composer.rustapp.host import RustApplication
from composer.rustapp.result import RustFormalResult


def _build_app_and_runner() -> tuple[
    RustApplication, Callable[..., Awaitable[CorePipelineResult[RustFormalResult]]]
]:
    """One application (shared phase-enum identity between the frontend and the
    pipeline) plus the ``run_pipeline_fn`` that injects Crucible's sBPF pre-build +
    crate store. Shared by the console and TUI entry points."""
    app = build_crucible_application()

    async def _run_pipeline_fn(*, source_input, ctx, handler_factory, env, args):
        return await run_crucible_pipeline(
            source_input,
            ctx,
            handler_factory,
            env,
            app=app,
            fuzz_timeout_s=int(getattr(args, "fuzz_timeout", 30) or 30),
            max_concurrent=args.max_concurrent,
            max_bug_rounds=args.max_bug_rounds,
            interactive=args.interactive,
        )

    return app, _run_pipeline_fn


async def _console_crucible() -> int:
    summary = RunSummary()
    app, run_pipeline_fn = _build_app_and_runner()
    event_kinds = {e.kind for e in app.descriptor.event_kinds}

    async with rust_entry_point(
        app, summary, run_pipeline_fn=run_pipeline_fn, env_builder=build_crucible_env
    ) as run:
        result = await run(GenericRustConsoleHandler(event_kinds).make_handler)
        print(f"\n{'=' * 60}")
        print(summary.format())
        print(f"\n  Instructions: {result.n_components}")
        print(f"  Properties:   {result.n_properties}")
        for line in format_verdict_lines(summarize_verdicts(result)):
            print(line)
        if result.failures:
            print(f"  Failures:     {len(result.failures)}")
            for f in result.failures:
                print(f"    - {f}")
        print(f"{'=' * 60}")
        return 0


async def _tui_crucible() -> int:
    summary = RunSummary()
    app, run_pipeline_fn = _build_app_and_runner()
    event_kinds = {e.kind for e in app.descriptor.event_kinds}
    notice_kinds = {e.kind for e in app.descriptor.event_kinds if e.notice}

    async with rust_entry_point(
        app, summary, run_pipeline_fn=run_pipeline_fn, env_builder=build_crucible_env
    ) as pipeline:
        tui = GenericRustApp(
            phase_labels=app.phase_labels,
            section_order=app.section_order,
            header_text=app.header_text,
            event_kinds=event_kinds,
            notice_kinds=notice_kinds,
        )
        result: CorePipelineResult[RustFormalResult] | None = None

        async def work():
            nonlocal result
            try:
                result = await pipeline(tui.make_handler)
                msg = (
                    f"crucible complete: {result.n_components} instructions, "
                    f"{result.n_properties} properties"
                )
                if tally := summarize_verdicts(result).tally:
                    msg += f" — {tally}"
                if result.failures:
                    msg += f", {len(result.failures)} failures"
                tui.notify(msg)
            except Exception as exc:  # noqa: BLE001 — surface to the UI, don't crash the loop
                tui.notify(f"Pipeline failed: {exc}", severity="error")
            finally:
                tui._pipeline_done = True

        tui.set_work(work)
        await tui.run_async()
        print(summary.format())
        if result is not None:
            for line in format_verdict_lines(summarize_verdicts(result)):
                print(line)
            for f in result.failures:
                print(f"  FAILED: {f}")
        return 0


def console_crucible() -> int:
    """Run the Crucible (Solana fuzzing) application in console mode."""
    return asyncio.run(_console_crucible())


def tui_crucible() -> int:
    """Run the Crucible (Solana fuzzing) application in the Textual TUI."""
    return asyncio.run(_tui_crucible())
