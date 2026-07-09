"""Adapter: wrap a Rust wheel as a :class:`PipelineBackend`.

Three phase objects mirror the CVL / foundry backends, but each delegates to the
Rust module:

* :class:`RustBackend`        — ``PipelineBackend`` (guidance, phases, store, ``prepare_system``).
* :class:`RustPreparedSystem` — builds the formalizer.
* :class:`RustFormalizer`     — ``formalize`` drives the Rust decider through the
  IoC loop; ``fetch_verdicts`` / ``finalize`` call the module's sync FFI.

``RealEffects`` binds the loop's effects to live services: ``emit`` via LangGraph's
stream writer, ``call_llm`` via the run's model, an in-memory scratch cache, and
injectable ``run_prover`` / ``run_feedback`` hooks (a self-contained Tier-1 Rust
backend never asks for those; a run-service-backed one is wired per deployment).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, cast, override

from composer.io.multi_job import TaskInfo

from composer.pipeline.core import (
    CorePhases,
    Formalizer,
    GaveUp,
    PipelineRun,
    PreparedSystem,
    SystemAnalysisSpec,
)
from composer.pipeline.ecosystem import Ecosystem
from composer.sandbox.command import DEFAULT_TIMEOUT_S, run_local_command
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.loop import Effects, GaveUp as LoopGaveUp, drive_session
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import WorkflowContext
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import Outcome, RuleName
from composer.spec.system_model import FeatureUnit
from composer.spec.types import PropertyFormulation

_log = logging.getLogger(__name__)

# A run_prover / run_feedback hook: async, backend-shaped JSON in and out.
ProverHook = Callable[[str, Any, "list[str] | None"], Awaitable[dict]]
FeedbackHook = Callable[[str, Any, Any], Awaitable[dict]]


class RealEffects:
    """The production :class:`Effects`. One instance per ``formalize`` call (it
    holds the call's cache scope)."""

    def __init__(
        self,
        ctx: WorkflowContext[RustFormalResult],
        run: PipelineRun,
        *,
        prover: ProverHook | None = None,
        feedback: FeedbackHook | None = None,
        command_sem: asyncio.Semaphore | None = None,
        command_timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self._ctx = ctx
        self._run = run
        self._prover = prover
        self._feedback = feedback
        self._command_sem = command_sem
        self._command_timeout_s = command_timeout_s
        # Loop-scratch cache (within one formalize). Cross-run persistence is the
        # driver's result-level cache, keyed by formalized_type — not this.
        self._scratch: dict[str, Any] = {}

    async def call_llm(self, messages: Any) -> str:
        """Run one bounded, **tool-enabled** authoring turn and return its final text.

        Unlike a bare ``ainvoke``, this binds the env's tool belt (source navigation
        + RAG search over the backend's knowledge base) and runs an agent to
        completion, so the decider's prompt can pull in framework docs / read the
        program. This is the ``docs/crucible-application.md`` §7.5 framework change —
        shared by every Rust backend, so a large-corpus backend (CVLR-Solana) reuses
        it by shipping only a knowledge DB. Must run inside a ``with_handler`` scope
        (the caller wraps it in ``run.runner``)."""
        from composer.rustapp._llm_agent import run_llm_agent

        return await run_llm_agent(
            self._run.env, messages, recursion_limit=self._ctx.recursion_limit
        )

    async def run_prover(self, spec: str, config: Any, rules: list[str] | None) -> dict:
        if self._prover is None:
            raise NotImplementedError(
                "This Rust backend requested a `run_prover` effect but no prover hook "
                "was supplied to RealEffects. Either make the Rust backend self-contained "
                "(do verification inside Rust, no RunProver command) or pass `prover=` when "
                "constructing the RustFormalizer."
            )
        return await self._prover(spec, config, rules)

    async def run_feedback(self, spec: str, skipped: Any, rebuttals: Any) -> dict:
        if self._feedback is None:
            raise NotImplementedError(
                "This Rust backend requested a `run_feedback` effect but no feedback hook "
                "was supplied to RealEffects."
            )
        return await self._feedback(spec, skipped, rebuttals)

    def _ensure_workdir(self) -> Path:
        # Build-based backends (Crucible) run commands in the project tree — the
        # generated harness references the program crate + the built `.so` by
        # relative path, and the artifact store writes into it. Isolation is the
        # sandbox's job (phase 6, §7.4), which will bind-mount what a command needs.
        return Path(self._run.source.project_root)

    async def run_command(
        self, program: str, args: list[str], files: dict[str, str]
    ) -> dict:
        result = await run_local_command(
            program,
            args,
            files,
            workdir=self._ensure_workdir(),
            timeout_s=self._command_timeout_s,
            sem=self._command_sem,
        )
        return result.as_observation()

    async def cache_get(self, key: str) -> Any | None:
        return self._scratch.get(key)

    async def cache_put(self, key: str, value: Any) -> None:
        self._scratch[key] = value

    async def emit(self, event_kind: str, payload: dict) -> None:
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
        except Exception:  # not inside a graph stream scope
            _log.debug("emit(%s) dropped: no stream writer in scope", event_kind)
            return
        writer({"type": event_kind, **payload})


@dataclass
class _RustFormalizerCfg:
    prover: ProverHook | None = None
    feedback: FeedbackHook | None = None


class RustFormalizer(Formalizer[RustFormalResult, FeatureUnit]):
    """Drives the Rust decider. ``formalize`` builds a session from the marshalled
    unit + properties and runs the IoC loop; ``fetch_verdicts`` / ``finalize``
    are off-thread sync FFI calls. Ecosystem-agnostic: the unit is any
    :class:`FeatureUnit` and is marshalled via ``feature_json()``."""

    def __init__(
        self,
        module: Any,
        descriptor: AppDescriptor,
        *,
        prover: ProverHook | None = None,
        feedback: FeedbackHook | None = None,
        component_config: dict | None = None,
        command_timeout_s: int = DEFAULT_TIMEOUT_S,
        store: Any = None,
    ):
        super().__init__(RustFormalResult, descriptor.backend_tag)
        self._module = module
        self._descriptor = descriptor
        self._hooks = _RustFormalizerCfg(prover=prover, feedback=feedback)
        # A base config merged into every per-component session (e.g. the shared
        # fixture authored by the setup session, the program name, the fuzz budget).
        self._component_config = component_config or {}
        self._command_timeout_s = command_timeout_s
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
        # Pre-place the manifest declaring this component's feature so its session
        # can write main.rs + fuzz (the decider can't render host-resolved deps).
        prep = getattr(self._store, "prepare_component", None)
        if prep is not None:
            prep(feat.slug)

        session_input = json.dumps(
            {
                "label": label,
                "component": feat.feature_json(),
                "props": [
                    {"title": p.title, "sort": p.sort, "description": p.description}
                    for p in props
                ],
                "config": {**self._component_config, "slug": feat.slug},
            }
        )
        session = self._module.new_session(session_input)
        effects = RealEffects(
            ctx, run, prover=self._hooks.prover, feedback=self._hooks.feedback,
            command_timeout_s=self._command_timeout_s,
        )
        result = await drive_session(session, effects)
        if isinstance(result, LoopGaveUp):
            return GaveUp(reason=result.reason)
        return RustFormalResult.from_formalized(result.data)

    @override
    async def fetch_verdicts(
        self, inp: ReportComponentInput[RustFormalResult]
    ) -> dict[RuleName, Verdict]:
        formalized = inp.formalized
        if formalized is None:
            return {}

        def _mk(v: dict) -> Verdict:
            return Verdict(
                outcome=Outcome(v["outcome"]),
                line=v.get("line"),
                duration_seconds=v.get("duration_seconds"),
                unit_file=v.get("unit_file") or formalized.unit_file,
            )

        # Self-contained backend (e.g. Crucible fuzzer): the verdict is known at
        # formalize time and baked into the result — use it directly, no FFI call.
        baked = formalized.result.verdicts
        if baked:
            return {unit: _mk(v) for unit, v in baked.items()}

        # Otherwise defer to the wheel (a run-service-backed backend).
        payload = json.dumps(
            {
                "name": inp.name,
                "unit_file": formalized.unit_file,
                "run_link": formalized.run_link,
                "property_units": formalized.result.property_units(),
            }
        )
        raw = json.loads(await asyncio.to_thread(self._module.fetch_verdicts, payload))
        return {unit: _mk(v) for unit, v in raw.items()}

    @override
    async def finalize(self, outcomes, run: PipelineRun) -> None:
        from pathlib import Path

        from composer.pipeline.core import Delivered

        summary = [
            {
                "name": o.feat.display_name,
                "delivered": isinstance(o.result, Delivered),
                "unit_file": (
                    o.result.unit_file if isinstance(o.result, Delivered) else None
                ),
                "run_link": (
                    o.result.run_link if isinstance(o.result, Delivered) else None
                ),
            }
            for o in outcomes
        ]
        raw = await asyncio.to_thread(self._module.finalize, json.dumps(summary))
        if not raw:
            return
        files: dict[str, str] = json.loads(raw)
        root = Path(run.source.project_root)
        for rel, contents in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)


@dataclass
class RustPreparedSystem(PreparedSystem[RustFormalResult, Any]):
    backend: "RustBackend"
    analyzed: Any = None

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[RustFormalResult, FeatureUnit]:
        b = self.backend
        # Base config threaded into every per-component session.
        component_config: dict = {
            "program": str(run.source.contract_name),
            "fuzz_timeout": b.fuzz_timeout_s,
        }

        # Author the program-wide shared setup (e.g. the Crucible fixture) once, if
        # the wheel declares a setup session — reusing the same IoC loop. The result
        # (fixture source) is set on the store and threaded into per-component config.
        new_setup = getattr(b.module, "new_setup_session", None)
        if new_setup is not None and self.analyzed is not None:
            # Pre-place the harness manifest (deps + probe feature) so the setup
            # session can write main.rs and dry-run (the decider can't render deps).
            wsm = getattr(b.artifact_store, "write_setup_manifest", None)
            if wsm is not None:
                wsm()
            setup_input = json.dumps(
                {
                    "program": str(run.source.contract_name),
                    "analyzed": self.analyzed.model_dump(mode="json"),
                    "config": {},
                }
            )
            session = new_setup(setup_input)
            if session is not None:

                async def _drive():
                    eff = RealEffects(
                        cast(Any, run.ctx), run, prover=b.prover, feedback=b.feedback,
                        command_timeout_s=b.command_timeout_s,
                    )
                    return await drive_session(session, eff)

                result = await run.runner(
                    TaskInfo(f"{b.descriptor.name}-setup", "Build Harness", b._core_phases["formalization"]),
                    _drive,
                )
                if isinstance(result, LoopGaveUp):
                    raise RuntimeError(f"{b.descriptor.name} setup session gave up: {result.reason}")
                fixture = result.data.get("artifact_text", "")
                component_config["fixture"] = fixture
                set_fixture = getattr(b.artifact_store, "set_shared_fixture", None)
                if set_fixture is not None:
                    set_fixture(fixture)

        return RustFormalizer(
            b.module,
            b.descriptor,
            prover=b.prover,
            feedback=b.feedback,
            component_config=component_config,
            command_timeout_s=b.command_timeout_s,
            store=b.artifact_store,
        )


@dataclass
class RustBackend:
    """A :class:`PipelineBackend` backed by a Rust wheel. Structurally satisfies the
    protocol — the driver never imports it. Ecosystem-agnostic: it locates the main
    and marshals units through the resolved ``ecosystem`` + the ``FeatureUnit``
    protocol, so an ``evm`` and a ``solana`` wheel share this one adapter."""

    module: Any
    descriptor: AppDescriptor
    _phase: type
    _core_phases: CorePhases
    artifact_store: ArtifactStore[Any, RustFormalResult]
    ecosystem: Ecosystem[Any, Any, Any]
    prover: ProverHook | None = None
    feedback: FeedbackHook | None = None
    # Wall-clock ceiling for a single RunCommand effect (a first harness build +
    # fuzz can be minutes); and the per-component fuzzing budget.
    command_timeout_s: int = DEFAULT_TIMEOUT_S
    fuzz_timeout_s: int = 30

    @property
    def backend_guidance(self) -> str:
        return self.descriptor.backend_guidance

    @property
    def analysis_spec(self) -> SystemAnalysisSpec:
        return SystemAnalysisSpec(self.descriptor.analysis_key)

    @property
    def core_phases(self) -> CorePhases:
        return self._core_phases

    async def prepare_system(
        self, analyzed: Any, run: PipelineRun
    ) -> PreparedSystem[RustFormalResult, Any]:
        # The ecosystem locates its own Main (EVM contract, Solana program, …).
        return RustPreparedSystem(self.ecosystem.locate_main(analyzed, run.source), self, analyzed)

    def to_artifact_id(self, c: FeatureUnit) -> RustArtifact:
        return RustArtifact(
            c.slug,
            self.descriptor.artifact_layout.artifact_prefix,
            self.descriptor.artifact_layout.artifact_extension,
        )
