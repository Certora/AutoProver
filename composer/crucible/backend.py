"""Crucible-specific formalization prep — lives *here*, not in the generic adapter.

Owns the shared-fixture **setup artifact** (authored + dry-run-compiled once, via the same
author→compile loop the components use), harness-crate store prep (manifest placement, offline
cargo warm, fixture install), and per-invariant Cargo-feature reservation on the shared crate.

The generic :class:`~composer.rustapp.adapter.RustFormalizer` runs the author→compile→validate
loop; :class:`CrucibleFormalizer` adds the crate-store side effects (fixture context + feature
reservation), and serializes the toolchain runs against the one shared crate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import override

from composer.crucible.store import CrucibleArtifactStore
from composer.io.multi_job import TaskInfo
from composer.pipeline.core import Formalizer, GaveUp, PipelineRun, PreparedSystem
from composer.rustapp.adapter import (
    RustBackend,
    RustFormalizer,
    author_and_compile,
    make_emitter,
)
from composer.rustapp.result import RustFormalResult
from composer.spec.system_model import BaseApplication, FeatureUnit


class CrucibleFormalizer(RustFormalizer):
    """Like :class:`RustFormalizer`, but threads the shared fixture into each component's
    context, reserves each invariant's Cargo feature on the shared crate, and serializes the
    toolchain runs (one crate / target dir)."""

    def __init__(self, module, descriptor, *, store: CrucibleArtifactStore, fixture: str, **kw):
        # Shared harness crate: compile/validate builds serialize on one target dir.
        super().__init__(module, descriptor, command_sem=asyncio.Semaphore(1), **kw)
        self._store = store
        self._fixture = fixture

    @override
    def _context(self, run: PipelineRun) -> dict:
        # The decider's compile/validate need the shared fixture + the fuzz budget.
        return {
            "program": str(run.source.contract_name),
            "fixture": self._fixture,
            "fuzz_timeout": self._fuzz_timeout_s,
        }

    @override
    def _before_formalize(self, feat: FeatureUnit, slugs: list[str]) -> None:
        # Pre-place Cargo.toml declaring each invariant's feature (cumulative) so the
        # decider can write main.rs + build/fuzz each `c_<slug>`.
        for slug in slugs:
            self._store.prepare_component(slug)


@dataclass
class CruciblePreparedSystem(PreparedSystem[RustFormalResult]):
    """Authors the shared fixture once (a ``kind="setup"`` artifact), then returns a
    :class:`CrucibleFormalizer` carrying it."""

    backend: "CrucibleBackend"
    analyzed: BaseApplication

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[RustFormalResult]:
        b = self.backend
        store = b.crucible_store
        # Pre-place the harness manifest (deps + probe feature) so the setup artifact can write
        # main.rs and dry-run; with a sandbox on, warm harness deps with network now so the
        # confined build runs offline (docs/command-sandbox.md §5).
        store.write_setup_manifest()
        if b.sandbox is not None and b.sandbox.enabled:
            await store.warm_dependencies()

        workdir = Path(run.source.project_root)
        sandbox_dict = (
            b.sandbox.backend_spec(workdir, timeout_s=b.command_timeout_s)
            if (b.sandbox is not None and b.sandbox.enabled)
            else {"run_confined": None, "timeout_s": b.command_timeout_s}
        )
        setup_input = {
            "kind": "setup",
            "program": str(run.source.contract_name),
            "component": self.analyzed.model_dump(mode="json"),
            "props": [],
            "context": {},
        }
        emit = make_emitter()
        fixture = await run.runner(
            TaskInfo(f"{b.descriptor.name}-setup", "Build Harness", b._core_phases["formalization"]),
            lambda: author_and_compile(
                b.module,
                setup_input,
                env=run.env,
                sandbox_dict=sandbox_dict,
                workdir=workdir,
                recursion_limit=run.ctx.recursion_limit,
                backend_name=b.descriptor.name,
                emit=emit,
            ),
        )
        if isinstance(fixture, GaveUp):
            raise RuntimeError(f"{b.descriptor.name} setup gave up: {fixture.reason}")
        store.set_shared_fixture(fixture)

        return CrucibleFormalizer(
            b.module,
            b.descriptor,
            store=store,
            fixture=fixture,
            sandbox=b.sandbox,
            command_timeout_s=b.command_timeout_s,
            fuzz_timeout_s=b.fuzz_timeout_s,
        )


@dataclass
class CrucibleBackend(RustBackend):
    """Rust backend that runs Crucible's setup fixture + shared-crate harness path."""

    @property
    def crucible_store(self) -> CrucibleArtifactStore:
        store = self.artifact_store
        if not isinstance(store, CrucibleArtifactStore):
            raise TypeError(
                f"CrucibleBackend requires a CrucibleArtifactStore, got {type(store).__name__}"
            )
        return store

    @override
    async def prepare_system(
        self, analyzed: BaseApplication, run: PipelineRun
    ) -> PreparedSystem[RustFormalResult]:
        return CruciblePreparedSystem(
            self.ecosystem.locate_main(analyzed, run.source), self, analyzed
        )
