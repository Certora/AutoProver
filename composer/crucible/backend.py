"""Crucible-specific formalization prep — lives *here*, not in the generic adapter.

Owns:

* the shared-fixture setup session (``new_setup_session`` IoC loop);
* harness-crate store prep (manifest placement, offline cargo warm, fixture install);
* per-component feature reservation + command serialization (one shared crate).

The generic :class:`~composer.rustapp.adapter.RustFormalizer` only drives a session;
:class:`CrucibleFormalizer` adds the crate-store side effects around it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import override

from composer.crucible.store import CrucibleArtifactStore
from composer.io.multi_job import TaskInfo
from composer.pipeline.core import Formalizer, GaveUp, PipelineRun, PreparedSystem
from composer.rustapp.adapter import RealEffects, RustBackend, RustFormalizer
from composer.rustapp.loop import GaveUp as LoopGaveUp, drive_session
from composer.rustapp.result import RustFormalResult
from composer.spec.context import WorkflowContext
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import PropertyFormulation


class CrucibleFormalizer(RustFormalizer):
    """Like :class:`RustFormalizer`, but pre-places the component's Cargo feature
    and serializes command runs against the shared harness crate."""

    def __init__(self, store: CrucibleArtifactStore, **kwargs):
        # Shared harness: concurrent sessions must not interleave main.rs writes /
        # builds. LLM authoring turns still run concurrently (sem only wraps RunCommand).
        super().__init__(**kwargs, command_sem=asyncio.Semaphore(1))
        self._store = store

    @override
    async def formalize(
        self,
        label: str,
        feat: FeatureUnit,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[RustFormalResult],
        run: PipelineRun,
    ) -> RustFormalResult | GaveUp:
        # Pre-place Cargo.toml declaring this component's feature so the session can
        # write main.rs + fuzz (the decider can't render host-resolved deps).
        self._store.prepare_component(feat.slug)
        return await super().formalize(label, feat, props, ctx, run)


@dataclass
class CruciblePreparedSystem(PreparedSystem[RustFormalResult]):
    """Authors the shared fixture once, then returns a :class:`CrucibleFormalizer`."""

    backend: "CrucibleBackend"
    analyzed: BaseApplication

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[RustFormalResult]:
        b = self.backend
        store = b.crucible_store
        component_config: dict = {
            "program": str(run.source.contract_name),
            "fuzz_timeout": b.fuzz_timeout_s,
        }

        # Pre-place the harness manifest (deps + probe feature) so the setup session
        # can write main.rs and dry-run (the decider can't render host-resolved deps).
        store.write_setup_manifest()
        # With a sandbox on (network off), warm harness deps with network now so the
        # confined `crucible run` can build offline (docs/command-sandbox.md §5).
        if b.sandbox is not None and b.sandbox.enabled:
            await store.warm_dependencies()

        setup_input = json.dumps(
            {
                "program": str(run.source.contract_name),
                "analyzed": self.analyzed.model_dump(mode="json"),
                "config": {},
            }
        )
        session = b.module.new_setup_session(setup_input)
        if session is not None:

            async def _drive():
                eff = RealEffects(
                    run.ctx,
                    run,
                    prover=b.prover,
                    feedback=b.feedback,
                    command_timeout_s=b.command_timeout_s,
                    sandbox=b.sandbox,
                    backend_name=b.descriptor.name,
                )
                return await drive_session(session, eff)

            result = await run.runner(
                TaskInfo(
                    f"{b.descriptor.name}-setup",
                    "Build Harness",
                    b._core_phases["formalization"],
                ),
                _drive,
            )
            if isinstance(result, LoopGaveUp):
                raise RuntimeError(
                    f"{b.descriptor.name} setup session gave up: {result.reason}"
                )
            fixture = result.data.get("artifact_text", "")
            component_config["fixture"] = fixture
            store.set_shared_fixture(fixture)

        return CrucibleFormalizer(
            store,
            module=b.module,
            descriptor=b.descriptor,
            prover=b.prover,
            feedback=b.feedback,
            component_config=component_config,
            command_timeout_s=b.command_timeout_s,
            sandbox=b.sandbox,
        )


@dataclass
class CrucibleBackend(RustBackend):
    """Rust backend that runs Crucible's setup + crate-harness formalization path."""

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
