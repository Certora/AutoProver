"""Adapter: wrap a Rust wheel (a :class:`~autoprover_sdk.Backend`) as a
:class:`~composer.pipeline.core.PipelineBackend`.

The Rust wheel is a **passive service** (``docs/rust-backend-api.md``): Python owns the
author→compile→judge→validate loop and every LLM turn, and calls the wheel's pure callouts
(``descriptor`` / ``units`` / ``author_prompt`` / ``judge_prompt`` / ``finalize``) plus the two
blocking ones (``compile`` / ``validate``) that run the toolchain via ``run-confined``. There is
no IoC ``resume`` loop and no ``Effects`` protocol.

Three phase objects mirror the CVL / foundry backends:

* :class:`RustBackend`        — ``PipelineBackend`` (guidance, phases, store, ``prepare_system``).
* :class:`RustPreparedSystem` — builds the formalizer (thin; no app-specific setup).
* :class:`RustFormalizer`     — ``formalize`` runs the loop; ``fetch_verdicts`` reads the verdicts
  ``validate`` baked into the result.

App-specific orchestration (a shared setup artifact, crate prep) lives in the application package
— e.g. :mod:`composer.crucible.backend`.
"""

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
from composer.sandbox.command import DEFAULT_TIMEOUT_S
from composer.sandbox.config import SandboxConfig
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import WorkflowContext
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import Outcome, ReportBackend, RuleName
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import PropertyFormulation
from composer.spec.util import slugify_filename

_log = logging.getLogger(__name__)

# Author→compile revise budget (was the Rust sessions' SETUP/PC_MAX_ATTEMPTS).
DEFAULT_MAX_ATTEMPTS = 7

# Derived from the ReportBackend literal so the two can't drift (single source of truth).
_REPORT_BACKENDS: frozenset[str] = frozenset(get_args(ReportBackend.__value__))


def as_report_backend(tag: str) -> ReportBackend:
    """Validate a wheel's free-form ``backend_tag`` against the closed report set."""
    if tag not in _REPORT_BACKENDS:
        raise ValueError(
            f"unknown report backend_tag {tag!r}; expected one of {sorted(_REPORT_BACKENDS)}"
        )
    return cast(ReportBackend, tag)


# ---------------------------------------------------------------------------
# Shared loop helpers (used by RustFormalizer.formalize and app setup artifacts).
# ---------------------------------------------------------------------------

def make_emitter() -> Callable[[str, dict], None]:
    """A ``emit(kind, payload)`` that streams a domain event to the current task's panel.
    Routes out-of-graph (the loop isn't inside a LangGraph run) via ``push_custom_update``,
    keyed by the active ``run_task`` id — the same routing the old ``RealEffects.emit`` used."""
    from composer.diagnostics.timing import get_current_task_id
    from composer.io.context import push_custom_update

    def emit(kind: str, payload: dict) -> None:
        push_custom_update({"type": kind, **payload}, thread_id=get_current_task_id() or "rust")

    return emit


def _strip_fence(text: str) -> str:
    """Strip a leading/trailing ``​```lang`` code fence if the model wrapped its answer
    (the authored artifact is written verbatim into a source file, so a fence would break it)."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        body = t[first_nl + 1 :] if first_nl != -1 else t
        return body.removesuffix("```").rstrip().removesuffix("```").rstrip()
    return t


def unique_slugs(props: list[PropertyFormulation]) -> list[str]:
    """One unique kebab slug per property (basis for its unit/feature name). Titles are unique
    at extraction; a slug collision (punctuation/casing) gets a numeric suffix."""
    slugs: list[str] = []
    seen: dict[str, int] = {}
    for p in props:
        base = slugify_filename(p.title) or "inv"
        n = seen.get(base, 0)
        seen[base] = n + 1
        slugs.append(base if n == 0 else f"{base}_{n}")
    return slugs


def _first_line(s: str) -> str:
    return next((ln for ln in s.splitlines() if ln.strip()), "").strip()


def _parse_judge(review: str) -> tuple[bool, str]:
    """Interpret a judge reply as (accept, feedback). Accepts a JSON ``{accept, feedback}`` or a
    plain reply led by ``ACCEPT`` / ``REJECT``. (No backend enables the judge today.)"""
    try:
        obj = json.loads(review)
        if isinstance(obj, dict):
            return bool(obj.get("accept")), str(obj.get("feedback", ""))
    except (json.JSONDecodeError, ValueError):
        pass
    return (not review.strip().upper().startswith("REJECT")), review


async def _author_turn(
    module: Any, input_json: str, failure: dict | None, *, env: Any, recursion_limit: int, backend_name: str
) -> str:
    """One authoring turn: render the backend's prompt (with any prior failure as revise
    context), run the tool-enabled LLM agent, and strip a code fence off the result."""
    from composer.rustapp._llm_agent import run_llm_agent

    prompt = json.loads(
        module.author_prompt(input_json, json.dumps(failure) if failure is not None else None)
    )
    reply = await run_llm_agent(env, prompt, recursion_limit=recursion_limit, backend_name=backend_name)
    return _strip_fence(reply)


async def _judge_turn(
    module: Any, input_json: str, spec: str, *, env: Any, recursion_limit: int, backend_name: str
) -> tuple[bool, str]:
    """Optional LLM review of a spec: ``(accept, feedback)``. ``(True, "")`` when the backend
    declares no judge (``judge_prompt`` → ``None``, the default)."""
    from composer.rustapp._llm_agent import run_llm_agent

    jp = module.judge_prompt(input_json, spec)
    if not jp:
        return True, ""
    review = await run_llm_agent(env, json.loads(jp), recursion_limit=recursion_limit, backend_name=backend_name)
    return _parse_judge(review)


async def author_and_compile(
    module: Any,
    input_dict: dict,
    *,
    env: Any,
    sandbox_dict: dict,
    workdir: Path,
    recursion_limit: int,
    backend_name: str,
    emit: Callable[[str, dict], None],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    command_sem: asyncio.Semaphore | None = None,
) -> str | GaveUp:
    """Author an artifact's spec, gate it with the backend's ``compile`` (retry on failure) and
    optional ``judge``. Returns the compiled spec text, or :class:`GaveUp`. Used for artifacts
    that have no fuzz units to validate — e.g. Crucible's shared setup fixture (a compile-only
    gate). The component path fuses the build gate into ``validate`` instead (see
    :meth:`RustFormalizer.formalize`)."""
    input_json = json.dumps(input_dict)
    sandbox_json = json.dumps(sandbox_dict)
    failure: dict | None = None
    for _ in range(max_attempts):
        spec = await _author_turn(
            module, input_json, failure, env=env, recursion_limit=recursion_limit, backend_name=backend_name
        )
        result = json.loads(
            await _run_blocking(
                lambda: module.compile(input_json, spec, str(workdir), sandbox_json), command_sem
            )
        )
        if result.get("status") != "ok":
            errors = result.get("errors", "")
            failure = {"draft": spec, "errors": errors}
            emit("build_output", {"line": _first_line(errors) or "build failed; revising"})
            continue
        ok, feedback = await _judge_turn(
            module, input_json, spec, env=env, recursion_limit=recursion_limit, backend_name=backend_name
        )
        if not ok:
            failure = {"draft": spec, "errors": feedback}
            continue
        return spec
    return GaveUp(reason=f"{backend_name}: did not pass compile/judge in {max_attempts} attempts")


async def _run_blocking(thunk: Callable[[], str], sem: asyncio.Semaphore | None) -> str:
    """Run a blocking wheel call (``compile``/``validate`` — they spawn ``run-confined`` and
    release the GIL) off the event loop, serialized by ``sem`` when the backend shares one
    workdir/crate across concurrent units."""
    if sem is not None:
        async with sem:
            return await asyncio.to_thread(thunk)
    return await asyncio.to_thread(thunk)


# ---------------------------------------------------------------------------
# The formalizer.
# ---------------------------------------------------------------------------

class RustFormalizer(Formalizer[RustFormalResult]):
    """Drives a Rust :class:`~autoprover_sdk.Backend` through the author→compile→judge→validate
    loop. Ecosystem-agnostic: the unit is any :class:`FeatureUnit`, marshalled via
    ``feature_json()``."""

    def __init__(
        self,
        module: Any,
        descriptor: AppDescriptor,
        *,
        sandbox: SandboxConfig | None = None,
        command_timeout_s: int = DEFAULT_TIMEOUT_S,
        fuzz_timeout_s: int = 30,
        command_sem: asyncio.Semaphore | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        super().__init__(RustFormalResult, as_report_backend(descriptor.backend_tag))
        self._module = module
        self._descriptor = descriptor
        self._sandbox = sandbox
        self._command_timeout_s = command_timeout_s
        self._fuzz_timeout_s = fuzz_timeout_s
        self._command_sem = command_sem
        self._max_attempts = max_attempts

    # -- hooks an application backend overrides ----------------------------

    def _context(self, run: PipelineRun) -> dict:
        """The ``AuthorInput.context`` blob for a component (backend dependencies).
        Base: just the program. Crucible adds the shared fixture + fuzz budget."""
        return {"program": str(run.source.contract_name)}

    def _before_formalize(self, feat: FeatureUnit, slugs: list[str]) -> None:
        """Place any crate scaffolding before compile/validate. Base: nothing.
        Crucible reserves each unit's Cargo feature."""
        return None

    def _sandbox_spec(self, workdir: Path) -> dict:
        if self._sandbox is None or not self._sandbox.enabled:
            return {"run_confined": None, "timeout_s": self._command_timeout_s}
        return self._sandbox.backend_spec(workdir, timeout_s=self._command_timeout_s)

    # -- the loop ----------------------------------------------------------

    @override
    async def formalize(
        self,
        label: str,
        feat: FeatureUnit,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[RustFormalResult],
        run: PipelineRun,
    ) -> RustFormalResult | GaveUp:
        workdir = Path(run.source.project_root)
        slugs = unique_slugs(props)
        self._before_formalize(feat, slugs)

        input_dict = {
            "kind": "component",
            "program": str(run.source.contract_name),
            "component": feat.feature_json(),
            "props": [
                {"title": p.title, "sort": p.sort, "description": p.description, "slug": s}
                for p, s in zip(props, slugs)
            ],
            "context": self._context(run),
        }
        input_json = json.dumps(input_dict)
        sandbox_dict = self._sandbox_spec(workdir)
        sandbox_json = json.dumps(sandbox_dict)
        emit = make_emitter()
        units = json.loads(self._module.units(input_json))

        # Fused author → judge → validate loop: validate's build IS the compile gate (no
        # separate dry-run per component — that ~2×'d the e2e). The units share one build, so
        # a BuildFailed from any unit re-authors the whole spec.
        failure: dict | None = None
        for _ in range(self._max_attempts):
            spec = await _author_turn(
                self._module, input_json, failure, env=run.env,
                recursion_limit=ctx.recursion_limit, backend_name=self._descriptor.name,
            )
            ok, feedback = await _judge_turn(
                self._module, input_json, spec, env=run.env,
                recursion_limit=ctx.recursion_limit, backend_name=self._descriptor.name,
            )
            if not ok:
                failure = {"draft": spec, "errors": feedback}
                continue

            verdicts: dict[str, dict] = {}
            property_units: list[tuple[str, list[str]]] = []
            build_failed: str | None = None
            for u in units:
                unit = u["unit"]
                res = json.loads(
                    await _run_blocking(
                        lambda unit=unit, spec=spec: self._module.validate(
                            input_json, spec, unit, str(workdir), sandbox_json
                        ),
                        self._command_sem,
                    )
                )
                if res.get("kind") == "build_failed":
                    build_failed = res.get("errors", "")
                    break
                verdict = res["verdict"]
                verdicts[unit] = verdict
                property_units.append((u["property"], [unit]))
                emit(
                    "verdict",
                    {"outcome": verdict.get("outcome"), "name": u["property"],
                     "line": f'{u["property"]}: {verdict.get("outcome")}'},
                )
            if build_failed is not None:
                failure = {"draft": spec, "errors": build_failed}
                emit("build_output", {"line": _first_line(build_failed) or "build failed; revising"})
                continue
            return RustFormalResult(artifact_text=spec, units=property_units, verdicts=verdicts)

        return GaveUp(
            reason=f"{self._descriptor.name}: did not compile/pass judge in {self._max_attempts} attempts"
        )

    @override
    async def fetch_verdicts(
        self, inp: ReportComponentInput[RustFormalResult]
    ) -> dict[RuleName, Verdict]:
        formalized = inp.formalized
        if formalized is None:
            return {}
        return {
            unit: Verdict(
                outcome=Outcome(v["outcome"]),
                line=v.get("line"),
                duration_seconds=v.get("duration_seconds"),
                unit_file=v.get("unit_file") or formalized.unit_file,
            )
            for unit, v in formalized.result.verdicts.items()
        }

    @override
    async def finalize(self, outcomes, run: PipelineRun) -> None:
        from composer.pipeline.core import Delivered

        summary = [
            {
                "name": o.feat.display_name,
                "delivered": isinstance(o.result, Delivered),
                "unit_file": o.result.unit_file if isinstance(o.result, Delivered) else None,
                "run_link": o.result.run_link if isinstance(o.result, Delivered) else None,
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
    """Generic prepared system: build a formalizer. Applications that need a setup artifact or
    crate prep override :meth:`RustBackend.prepare_system` (see :mod:`composer.crucible.backend`)."""

    backend: "RustBackend"
    analyzed: BaseApplication | None = None

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[RustFormalResult]:
        b = self.backend
        return RustFormalizer(
            b.module,
            b.descriptor,
            sandbox=b.sandbox,
            command_timeout_s=b.command_timeout_s,
            fuzz_timeout_s=b.fuzz_timeout_s,
        )


@dataclass
class RustBackend:
    """A :class:`PipelineBackend` backed by a Rust wheel. Structurally satisfies the protocol —
    the driver never imports it. Ecosystem-agnostic: it locates the main and marshals units
    through the resolved ``ecosystem`` + the ``FeatureUnit`` protocol.

    Subclass (or replace via ``backend_cls``) when the app needs non-generic prep — e.g.
    Crucible's shared fixture + harness crate."""

    module: Any
    descriptor: AppDescriptor
    _phase: type
    _core_phases: CorePhases
    artifact_store: ArtifactStore[Any, RustFormalResult]
    ecosystem: Ecosystem[Any, Any, Any]
    # Wall-clock ceiling for a single compile/validate (a first harness build can be minutes).
    command_timeout_s: int = DEFAULT_TIMEOUT_S
    fuzz_timeout_s: int = 30
    # How to confine every toolchain run (docs/command-sandbox.md). None → unsandboxed.
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
        return RustPreparedSystem(
            self.ecosystem.locate_main(analyzed, run.source), self, analyzed
        )

    def to_artifact_id(self, c: FeatureUnit) -> RustArtifact:
        return RustArtifact(
            c.slug,
            self.descriptor.artifact_layout.artifact_prefix,
            self.descriptor.artifact_layout.artifact_extension,
        )


# Retained for callers that referenced the hook types (now unused by the loop).
ProverHook = Callable[[str, Any, "list[str] | None"], Awaitable[dict]]
FeedbackHook = Callable[[str, Any, Any], Awaitable[dict]]
