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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, NotRequired, override

from graphcore.graph import FlowInput
from langgraph.graph import MessagesState

from composer.pipeline.core import (
    CorePhases,
    Formalizer,
    GaveUp,
    PipelineRun,
    PreparedSystem,
    SystemAnalysisSpec,
)
from composer.pipeline.ecosystem import Ecosystem
from composer.rustapp.command import DEFAULT_TIMEOUT_S, run_local_command
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.loop import Effects, GaveUp as LoopGaveUp, drive_session
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.rustapp.store import RustArtifactStore
from composer.spec.context import WorkflowContext
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import Outcome, RuleName
from composer.spec.system_model import FeatureUnit
from composer.spec.types import PropertyFormulation

_log = logging.getLogger(__name__)

# A run_prover / run_feedback hook: async, backend-shaped JSON in and out.
ProverHook = Callable[[str, Any, "list[str] | None"], Awaitable[dict]]
FeedbackHook = Callable[[str, Any, Any], Awaitable[dict]]


# State + input for the tool-enabled `call_llm` agent (module scope so the
# NotRequired result annotation type-checks). ``result`` is the agent's final text.
class _LlmState(MessagesState):
    result: NotRequired[str]


class _LlmInput(FlowInput):
    pass


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
        # Per-formalize workdir for RunCommand effects (a session materializes its
        # crate once and runs several commands against it). Created lazily.
        self._workdir: Path | None = None
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
        from composer.spec.graph_builder import bind_standard, run_to_completion
        from composer.spec.util import uniq_thread_id

        env = self._run.env
        tools = list(getattr(env, "all_tools", None) or env.rag_tools)

        graph = (
            bind_standard(
                env.builder_heavy(),
                _LlmState,
                doc="Your complete final answer as a single string (e.g. the authored source file).",
            )
            .with_input(_LlmInput)
            .with_tools(tools)
            .compile_async()
        )

        content = messages if isinstance(messages, str) else json.dumps(messages)
        res = await run_to_completion(
            graph,
            _LlmInput(input=[content]),
            thread_id=uniq_thread_id("rust-llm"),
            recursion_limit=self._ctx.recursion_limit,
            description="Rust backend authoring turn",
        )
        result = res.get("result")
        return result if isinstance(result, str) else json.dumps(result)

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
        # Lazily create a per-formalize scratch workdir. Left on disk (under the OS
        # temp dir) for post-run inspection; sandbox/cleanup is phase 6 (§7.4).
        if self._workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="autoprover-cmd-"))
        return self._workdir

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
    ):
        super().__init__(RustFormalResult, descriptor.backend_tag)
        self._module = module
        self._descriptor = descriptor
        self._hooks = _RustFormalizerCfg(prover=prover, feedback=feedback)

    @override
    async def formalize(
        self,
        label: str,
        feat: FeatureUnit,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[RustFormalResult],
        run: PipelineRun,
    ) -> RustFormalResult | GaveUp:
        session_input = json.dumps(
            {
                "label": label,
                "component": feat.feature_json(),
                "props": [
                    {"title": p.title, "sort": p.sort, "description": p.description}
                    for p in props
                ],
                "config": {},
            }
        )
        session = self._module.new_session(session_input)
        effects = RealEffects(
            ctx, run, prover=self._hooks.prover, feedback=self._hooks.feedback
        )
        result = await drive_session(session, effects)
        if isinstance(result, LoopGaveUp):
            return GaveUp(reason=result.reason)
        return RustFormalResult.from_formalized(result.data)

    @override
    async def fetch_verdicts(
        self, inp: ReportComponentInput[RustFormalResult]
    ) -> dict[RuleName, Verdict]:
        if inp.formalized is None:
            return {}
        payload = json.dumps(
            {
                "name": inp.name,
                "unit_file": inp.formalized.unit_file,
                "run_link": inp.formalized.run_link,
                "property_units": inp.formalized.result.property_units(),
            }
        )
        raw = json.loads(await asyncio.to_thread(self._module.fetch_verdicts, payload))
        return {
            unit: Verdict(
                outcome=Outcome(v["outcome"]),
                line=v.get("line"),
                duration_seconds=v.get("duration_seconds"),
                unit_file=v.get("unit_file"),
            )
            for unit, v in raw.items()
        }

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

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[RustFormalResult, FeatureUnit]:
        return RustFormalizer(
            self.backend.module,
            self.backend.descriptor,
            prover=self.backend.prover,
            feedback=self.backend.feedback,
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
    artifact_store: RustArtifactStore
    ecosystem: Ecosystem[Any, Any, Any]
    prover: ProverHook | None = None
    feedback: FeedbackHook | None = None

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
        return RustPreparedSystem(self.ecosystem.locate_main(analyzed, run.source), self)

    def to_artifact_id(self, c: FeatureUnit) -> RustArtifact:
        return RustArtifact(
            c.slug,
            self.descriptor.artifact_layout.artifact_prefix,
            self.descriptor.artifact_layout.artifact_extension,
        )
