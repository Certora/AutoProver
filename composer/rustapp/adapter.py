"""Adapter: wrap a Rust wheel as a :class:`PipelineBackend`.

Three phase objects mirror the CVL / foundry backends, but each delegates to the
Rust module:

* :class:`RustBackend`        — ``PipelineBackend`` (guidance, phases, store, ``prepare_system``).
* :class:`RustPreparedSystem` — builds the formalizer (thin; no app-specific setup).
* :class:`RustFormalizer`     — ``formalize`` drives the Rust decider through the
  IoC loop; ``fetch_verdicts`` / ``finalize`` call the module's sync FFI.

App-specific orchestration (shared fixtures, crate-harness prep, fuzz budgets) lives
in the application package — e.g. :mod:`composer.crucible.backend` — not here.

``RealEffects`` binds the loop's effects to live services: ``emit`` via LangGraph's
stream writer, ``call_llm`` via the run's model, an in-memory scratch cache, and
injectable ``run_prover`` / ``run_feedback`` hooks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, cast, get_args, override

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
from composer.sandbox.config import SandboxConfig
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.loop import Effects, GaveUp as LoopGaveUp, drive_session
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import WorkflowContext
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import Outcome, ReportBackend, RuleName
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import PropertyFormulation

_log = logging.getLogger(__name__)

# A run_prover / run_feedback hook: async, backend-shaped JSON in and out.
ProverHook = Callable[[str, Any, "list[str] | None"], Awaitable[dict]]
FeedbackHook = Callable[[str, Any, Any], Awaitable[dict]]

# Derived from the ReportBackend literal so the two can't drift (single source of truth).
_REPORT_BACKENDS: frozenset[str] = frozenset(get_args(ReportBackend.__value__))


def as_report_backend(tag: str) -> ReportBackend:
    """Validate a wheel's free-form ``backend_tag`` against the closed report set."""
    if tag not in _REPORT_BACKENDS:
        raise ValueError(
            f"unknown report backend_tag {tag!r}; expected one of {sorted(_REPORT_BACKENDS)}"
        )
    return cast(ReportBackend, tag)


class RealEffects:
    """The production :class:`Effects`. One instance per ``formalize`` / setup call
    (it holds that call's cache scope)."""

    def __init__(
        self,
        ctx: WorkflowContext[Any],
        run: PipelineRun,
        *,
        prover: ProverHook | None = None,
        feedback: FeedbackHook | None = None,
        command_sem: asyncio.Semaphore | None = None,
        command_timeout_s: int = DEFAULT_TIMEOUT_S,
        sandbox: SandboxConfig | None = None,
        backend_name: str = "rust",
    ):
        self._ctx = ctx
        self._run = run
        # The application's name (descriptor.name, e.g. "crucible") — used to label the
        # authoring-turn task in the console instead of a generic "rust backend".
        self._name = backend_name
        self._prover = prover
        self._feedback = feedback
        self._command_sem = command_sem
        self._command_timeout_s = command_timeout_s
        # How to confine RunCommand (docs/command-sandbox.md). None → unsandboxed
        # (the ``none`` provider) — for trusted-input / EVM paths.
        self._sandbox = sandbox
        # Loop-scratch cache (within one formalize). Cross-run persistence is the
        # driver's result-level cache, keyed by formalized_type — not this.
        self._scratch: dict[str, Any] = {}

    async def call_llm(self, messages: Any) -> str:
        """Run one bounded, **tool-enabled** authoring turn and return its final text.

        Binds the env's tool belt (source navigation + optional RAG) and runs an
        agent to completion. Must run inside a ``with_handler`` scope (the caller
        wraps it in ``run.runner``)."""
        from composer.rustapp._llm_agent import run_llm_agent

        return await run_llm_agent(
            self._run.env, messages, recursion_limit=self._ctx.recursion_limit,
            backend_name=self._name,
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
        # Build-based backends run commands in the project tree — the generated
        # harness references the program crate by relative path. Isolation is the
        # sandbox's job (docs/command-sandbox.md).
        return Path(self._run.source.project_root)

    async def run_command(
        self, program: str, args: list[str], files: dict[str, str]
    ) -> dict:
        workdir = self._ensure_workdir()
        provider = policy = None
        if self._sandbox is not None and self._sandbox.enabled:
            provider = self._sandbox.resolve_provider()
            policy = self._sandbox.build_policy(workdir)
        result = await run_local_command(
            program,
            args,
            files,
            workdir=workdir,
            timeout_s=self._command_timeout_s,
            sem=self._command_sem,
            provider=provider,
            policy=policy,
        )
        if result.exit_code != 0:
            # Surface authoring build/dry-run failures — otherwise they only reach the
            # decider (in the revise prompt) and are invisible when a session gives up.
            tail = (result.stderr or result.stdout or "")[-1500:]
            _log.warning(
                "RunCommand failed: %s %s (exit %s) in %s\n%s",
                program, " ".join(args), result.exit_code, workdir, tail,
            )
        return result.as_observation()

    async def cache_get(self, key: str) -> Any | None:
        return self._scratch.get(key)

    async def cache_put(self, key: str, value: Any) -> None:
        self._scratch[key] = value

    async def emit(self, event_kind: str, payload: dict) -> None:
        # The decider emits between graph calls (in the IoC loop), where
        # get_stream_writer() is unavailable — so route the event straight to the
        # task's EventHandler.handle_event via the with_handler scope. The task id is
        # the console label; the queue is per-task, so it reaches the right panel.
        from composer.diagnostics.timing import get_current_task_id
        from composer.io.context import push_custom_update

        delivered = push_custom_update(
            {"type": event_kind, **payload},
            thread_id=get_current_task_id() or "rust",
        )
        if not delivered:
            _log.debug("emit(%s) dropped: no handler scope", event_kind)


@dataclass
class _RustFormalizerCfg:
    prover: ProverHook | None = None
    feedback: FeedbackHook | None = None


class RustFormalizer(Formalizer[RustFormalResult]):
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
        sandbox: SandboxConfig | None = None,
        command_sem: asyncio.Semaphore | None = None,
    ):
        super().__init__(RustFormalResult, as_report_backend(descriptor.backend_tag))
        self._module = module
        self._descriptor = descriptor
        self._hooks = _RustFormalizerCfg(prover=prover, feedback=feedback)
        # Base config merged into every per-component session (application-specific
        # keys — fixture, fuzz budget, … — are supplied by the prepared-system layer).
        self._component_config = component_config or {}
        self._command_timeout_s = command_timeout_s
        self._sandbox = sandbox
        self._command_sem = command_sem

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
                "config": {**self._component_config, "slug": feat.slug},
            }
        )
        session = self._module.new_session(session_input)
        effects = RealEffects(
            ctx,
            run,
            prover=self._hooks.prover,
            feedback=self._hooks.feedback,
            command_timeout_s=self._command_timeout_s,
            sandbox=self._sandbox,
            command_sem=self._command_sem,
            backend_name=self._descriptor.name,
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

        # Self-contained backend: verdicts baked at formalize time — use them directly.
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
class RustPreparedSystem(PreparedSystem[RustFormalResult]):
    """Generic prepared system: build a formalizer with the program name in config.

    Applications that need a setup session or store-side prep override
    :meth:`RustBackend.prepare_system` (see :class:`composer.crucible.backend.CrucibleBackend`).
    """

    backend: "RustBackend"
    analyzed: BaseApplication | None = None

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[RustFormalResult]:
        b = self.backend
        return RustFormalizer(
            b.module,
            b.descriptor,
            prover=b.prover,
            feedback=b.feedback,
            component_config={"program": str(run.source.contract_name)},
            command_timeout_s=b.command_timeout_s,
            sandbox=b.sandbox,
        )


@dataclass
class RustBackend:
    """A :class:`PipelineBackend` backed by a Rust wheel. Structurally satisfies the
    protocol — the driver never imports it. Ecosystem-agnostic: it locates the main
    and marshals units through the resolved ``ecosystem`` + the ``FeatureUnit``
    protocol, so an ``evm`` and a ``solana`` wheel share this one adapter.

    Subclass (or replace via ``backend_cls``) when the app needs non-generic
    formalization prep — e.g. Crucible's shared fixture + harness crate.
    """

    module: Any
    descriptor: AppDescriptor
    _phase: type
    _core_phases: CorePhases
    artifact_store: ArtifactStore[Any, RustFormalResult]
    ecosystem: Ecosystem[Any, Any, Any]
    prover: ProverHook | None = None
    feedback: FeedbackHook | None = None
    # Wall-clock ceiling for a single RunCommand effect (a first harness build can
    # be minutes). Applications may thread additional session config via their
    # own prepared-system layer (e.g. Crucible's fuzz budget).
    command_timeout_s: int = DEFAULT_TIMEOUT_S
    fuzz_timeout_s: int = 30
    # How to confine every RunCommand (docs/command-sandbox.md). None → unsandboxed
    # (trusted-input only).
    sandbox: SandboxConfig | None = None

    @property
    def backend_guidance(self) -> str:
        return self.descriptor.backend_guidance

    @property
    def analysis_spec(self) -> SystemAnalysisSpec:
        return SystemAnalysisSpec(self.descriptor.analysis_key, "rust-properties")

    @property
    def core_phases(self) -> CorePhases:
        return self._core_phases

    async def prepare_system(
        self, analyzed: BaseApplication, run: PipelineRun
    ) -> PreparedSystem[RustFormalResult]:
        # The ecosystem locates its own Main (EVM contract, Solana program, …).
        return RustPreparedSystem(
            self.ecosystem.locate_main(analyzed, run.source), self, analyzed
        )

    def to_artifact_id(self, c: FeatureUnit) -> RustArtifact:
        return RustArtifact(
            c.slug,
            self.descriptor.artifact_layout.artifact_prefix,
            self.descriptor.artifact_layout.artifact_extension,
        )
