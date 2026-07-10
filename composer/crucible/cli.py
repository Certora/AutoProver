"""``main()`` glue for the Crucible application.

Builds one :class:`~composer.rustapp.host.RustApplication` (shared phase enum for
frontend + pipeline), injects the Crucible env + the sBPF pre-build via
``run_pipeline_fn``, then drives the console handler.

``import composer.bind`` runs first (import-time DI / test-tape bootstrap).
"""

from __future__ import annotations

import asyncio

import composer.bind as _  # noqa: F401  (side-effecting DI/tape bootstrap; must load first)

from composer.crucible.pipeline import (
    build_crucible_application,
    build_crucible_env,
    run_crucible_pipeline,
)
from composer.diagnostics.timing import RunSummary
from composer.rustapp.entry import rust_entry_point
from composer.rustapp.frontend import GenericRustConsoleHandler


async def _console_crucible() -> int:
    summary = RunSummary()
    # One application object: phase enum identity is shared by any future TUI and
    # the pipeline. Timeouts from CLI flags are applied inside run_crucible_pipeline.
    app = build_crucible_application()
    event_kinds = {e.kind for e in app.descriptor.event_kinds}

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

    async with rust_entry_point(
        app, summary, run_pipeline_fn=_run_pipeline_fn, env_builder=build_crucible_env
    ) as run:
        result = await run(GenericRustConsoleHandler(event_kinds).make_handler)
        print(f"\n{'=' * 60}")
        print(summary.format())
        print(f"\n  Instructions: {result.n_components}")
        print(f"  Properties:   {result.n_properties}")
        if result.failures:
            print(f"  Failures:     {len(result.failures)}")
            for f in result.failures:
                print(f"    - {f}")
        print(f"{'=' * 60}")
        return 0


def console_crucible() -> int:
    """Run the Crucible (Solana fuzzing) application in console mode."""
    return asyncio.run(_console_crucible())
