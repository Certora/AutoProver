"""``main()`` glue for the Crucible application.

Reuses the generic rust-app entry point (arg parsing, service setup, the SOLANA
ecosystem front half) but injects the Crucible pipeline (the crate store +
resolved deps + build/fuzz timeouts) via ``run_pipeline_fn``. The env's
``forbidden_read`` comes from the resolved ecosystem (Rust/Cargo), so no custom
env builder is needed.

``import composer.bind`` runs first (import-time DI / test-tape bootstrap).
"""

from __future__ import annotations

import asyncio

import composer.bind as _  # noqa: F401  (side-effecting DI/tape bootstrap; must load first)

from composer.crucible.pipeline import CRUCIBLE_MODULE, run_crucible_pipeline
from composer.diagnostics.timing import RunSummary
from composer.rustapp.entry import rust_entry_point
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.rustapp.host import build_application


async def _run_pipeline_fn(*, source_input, ctx, handler_factory, env, args):
    return await run_crucible_pipeline(
        source_input,
        ctx,
        handler_factory,
        env,
        fuzz_timeout_s=int(getattr(args, "fuzz_timeout", 30) or 30),
        max_concurrent=args.max_concurrent,
        max_bug_rounds=args.max_bug_rounds,
        interactive=args.interactive,
    )


async def _console_crucible() -> int:
    summary = RunSummary()
    app_meta = build_application(CRUCIBLE_MODULE)
    event_kinds = {e.kind for e in app_meta.descriptor.event_kinds}
    async with rust_entry_point(app_meta, summary, run_pipeline_fn=_run_pipeline_fn) as run:
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
