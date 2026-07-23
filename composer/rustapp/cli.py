"""Generic ``main()`` glue for a Rust application.

Two shapes, differing only in who owns the event loop (identical to the built-in
apps' ``composer/cli/*.py``):

* :func:`tui_main` — the pipeline runs as a background worker inside the Textual
  app, streaming into it.
* :func:`console_main` — the pipeline runs directly, printing on completion.

A Rust application ships a two-line CLI:

    from composer.rustapp.cli import tui_main
    def main() -> int:
        return tui_main("my_app")

``import composer.bind`` runs first (import-time DI / test-tape bootstrap), exactly
as the built-in ``main()``s require.
"""

import asyncio

import composer.bind as _  # noqa: F401  (side-effecting DI/tape bootstrap; must load first)

from composer.diagnostics.timing import RunSummary
from composer.pipeline.core import CorePipelineResult
from composer.rustapp.adapter import as_report_backend
from composer.rustapp.entry import EnvBuilder, rust_entry_point
from composer.rustapp.frontend import GenericRustApp, GenericRustConsoleHandler
from composer.rustapp.host import build_application
from composer.rustapp.result import RustFormalResult
from composer.rustapp.results import format_verdict_lines, summarize_verdicts


def _event_kinds(app) -> set[str]:
    return {e.kind for e in app.descriptor.event_kinds}


def _notice_kinds(app) -> set[str]:
    return {e.kind for e in app.descriptor.event_kinds if e.notice}


def _component_label(app) -> str:
    """The counts-block noun for one formalized unit ("Components" / "Instructions")."""
    return (app.descriptor.component_noun or "component").capitalize() + "s"


def _verdict_lines(app, result: CorePipelineResult[RustFormalResult]) -> list[str]:
    """Per-unit verdict tally + listing when the results carry verdicts; empty otherwise
    (a run-service backend, or a wheel that bakes none)."""
    return format_verdict_lines(
        summarize_verdicts(result, as_report_backend(app.descriptor.backend_tag))
    )


async def _tui_main(module_name: str, *, env_builder: EnvBuilder | None = None) -> int:
    summary = RunSummary()
    app_meta = build_application(module_name)
    async with rust_entry_point(app_meta, summary, env_builder=env_builder) as pipeline:
        tui = GenericRustApp(
            phase_labels=app_meta.phase_labels,
            section_order=app_meta.section_order,
            header_text=app_meta.header_text,
            event_kinds=_event_kinds(app_meta),
            notice_kinds=_notice_kinds(app_meta),
        )
        result: CorePipelineResult[RustFormalResult] | None = None

        async def work():
            nonlocal result
            try:
                result = await pipeline(tui.make_handler)
                noun = (app_meta.descriptor.component_noun or "component")
                msg = (
                    f"{app_meta.name} complete: {result.n_components} {noun}s, "
                    f"{result.n_properties} properties"
                )
                if tally := summarize_verdicts(
                    result, as_report_backend(app_meta.descriptor.backend_tag)
                ).tally:
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
            for line in _verdict_lines(app_meta, result):
                print(line)
            for f in result.failures:
                print(f"  FAILED: {f}")
        return 0


async def _console_main(module_name: str, *, env_builder: EnvBuilder | None = None) -> int:
    summary = RunSummary()
    app_meta = build_application(module_name)
    async with rust_entry_point(app_meta, summary, env_builder=env_builder) as run:
        result = await run(GenericRustConsoleHandler(_event_kinds(app_meta)).make_handler)
        print(f"\n{'=' * 60}")
        print(summary.format())
        print(f"\n  {_component_label(app_meta)}: {result.n_components}")
        print(f"  Properties: {result.n_properties}")
        for line in _verdict_lines(app_meta, result):
            print(line)
        if result.failures:
            print(f"  Failures:   {len(result.failures)}")
            for f in result.failures:
                print(f"    - {f}")
        print(f"{'=' * 60}")
        return 0


def tui_main(module_name: str, *, env_builder: EnvBuilder | None = None) -> int:
    """Run ``module_name`` as a Textual TUI application. Blocks until the run ends."""
    return asyncio.run(_tui_main(module_name, env_builder=env_builder))


def console_main(module_name: str, *, env_builder: EnvBuilder | None = None) -> int:
    """Run ``module_name`` in console (no-TUI) mode. Blocks until the run ends."""
    return asyncio.run(_console_main(module_name, env_builder=env_builder))
