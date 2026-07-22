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

App-specific orchestration (a shared setup artifact, workspace prep, crate assembly) is
descriptor-driven here — no per-application Python package (``docs/rust-pure-app.md``): the wheel
declares ``setup`` / ``workspace_prep`` / ``deliverable_mode=callout`` / ``finalize`` and the
generic :class:`RustPreparedSystem` runs them.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, NotRequired, cast, get_args, override

from pydantic import Field
from graphcore.graph import FlowInput, tool_state_update
from graphcore.tools.schemas import WithAsyncDependencies, WithInjectedId
from langgraph.graph import MessagesState
from langgraph.types import Command

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
from composer.sandbox.command import DEFAULT_TIMEOUT_S
from composer.sandbox.config import SandboxConfig
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import WorkflowContext
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.ui.tool_display import tool_display
from composer.spec.source.report.schema import Outcome, ReportBackend, RuleName
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import PropertyFormulation
from composer.spec.util import slugify_filename, uniq_thread_id

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
# The LLM authoring turn — the Rust backend's binding of the shared agent primitive
# (bind_standard / run_to_completion), the peer of composer/foundry/author.py. Python
# runs this; the backend only supplies the prompt (its author_prompt/judge_prompt
# callouts). It is the "author" step of the author→compile→judge→validate loop.
# ---------------------------------------------------------------------------

# Neutral fallback system prompt. The backend's prompt payload carries the task-specific
# `instruction` and MAY carry its own `system` prompt; when it doesn't, this applies. It
# conveys only the tool-using-agent + result-tool contract — no domain/language specifics
# (those belong in the backend's prompt).
_DEFAULT_SYS_PROMPT = (
    "You are an authoring agent. Use the available tools to explore the target "
    "program's source and any reference material, then produce the requested artifact. "
    "When done, call the `result` tool with your complete final answer as a single "
    "string — the artifact source only, with no surrounding prose or code fences."
)


def _split_prompt(messages: Any) -> tuple[str | None, str]:
    """Split a backend prompt payload into ``(system, instruction)``.

    The payload is a bare instruction string, or a dict carrying ``instruction`` and
    (optionally) a backend-defined ``system`` prompt. ``system`` is ``None`` when the
    backend doesn't supply one (the caller falls back to :data:`_DEFAULT_SYS_PROMPT`)."""
    if isinstance(messages, dict):
        return messages.get("system"), messages.get("instruction") or json.dumps(messages)
    return None, messages


# NOTE: `bind_standard` introspects this state class's ``__annotations__`` at runtime to
# unwrap ``result: NotRequired[T]`` — so the annotation must stay a real object, not a
# string. This is a concrete reason the repo bans ``from __future__ import annotations``
# (see CLAUDE.md); stringized annotations would break the unwrap here.
class _LlmState(MessagesState):
    result: NotRequired[str]


# In-loop-judge author state: `reviewed_text`/`review_ok` record the last draft the author sent
# to `request_review` and the judge's verdict on it; the result gate (`_review_gate`) blocks
# finalization until the submitted draft is one the judge accepted.
class _JudgedAuthorState(MessagesState):
    result: NotRequired[str]
    reviewed_text: NotRequired[str]
    review_ok: NotRequired[bool]


class _LlmInput(FlowInput):
    pass


# A callable that reviews a candidate artifact: ``(draft) -> (accepted, feedback)``. The host
# builds it (see :func:`_make_judge_hook`) around the wheel's ``judge_prompt``.
type _JudgeHook = Callable[[str], Awaitable[tuple[bool, str]]]


# Appended to the system prompt when the judge runs in-loop, so the author knows the review
# protocol and the finalize gate. Kept generic (no backend/domain specifics).
_REVIEW_PROTOCOL = (
    "\n\nBefore you finalize, a reviewer must accept your work. Call the `request_review` tool "
    "with the exact artifact text you intend to submit; if the review is REJECTED, revise and "
    "call `request_review` again. Only call `result` with a draft the reviewer ACCEPTED — the "
    "`result` call is rejected otherwise."
)


def _review_gate(state: _JudgedAuthorState, result_value: str) -> str | None:
    """Block ``result`` unless the submitted draft is exactly the one the judge accepted."""
    if state.get("review_ok") and state.get("reviewed_text") == result_value:
        return None
    return (
        "Not accepted yet: call `request_review` with this exact draft and get an ACCEPTED "
        "review before calling `result` (revise and re-review if it was REJECTED)."
    )


@tool_display("Requesting review", "Review")
class _RequestReview(WithInjectedId, WithAsyncDependencies[Command, "_JudgeHook"]):
    """Ask the reviewer to evaluate your current draft against the task's criteria. Returns the
    verdict and any feedback; if REJECTED, revise and call this again before finalizing."""

    draft: str = Field(
        description="The complete candidate artifact source to review — the exact text you intend "
        "to submit via `result`, no surrounding prose or code fences."
    )

    @override
    async def run(self) -> Command:
        with self.tool_deps() as judge:
            ok, feedback = await judge(self.draft)
        verdict = "ACCEPTED" if ok else "REJECTED"
        content = f"Review {verdict}.\n\n{feedback}" if feedback else f"Review {verdict}."
        return tool_state_update(
            tool_call_id=self.tool_call_id, content=content,
            reviewed_text=self.draft, review_ok=ok,
        )


async def run_llm_agent(
    env: Any, messages: Any, *, recursion_limit: int, backend_name: str = "rust",
    turn_label: str = "authoring", judge: "_JudgeHook | None" = None, memory_tool: Any = None,
    exclude_tools: frozenset[str] = frozenset(),
) -> str:
    """Run one bounded, tool-enabled turn and return its final text. ``turn_label``
    names the turn's role ("authoring" / "judge") for the UI/log panel.

    Binds the env's tool belt (source navigation + RAG search over the backend's
    knowledge base) and a result tool, and runs an agent to completion — so the
    prompt can pull in framework docs / read the program. Must run inside a
    ``with_handler`` scope (the caller wraps it in ``run.runner``).

    When ``judge`` is given, the turn becomes an in-loop-review author (docs/crucible-judge-in-loop.md):
    a ``request_review`` tool runs the judge in-session and ``result`` is gated on an accepted draft,
    so the author self-revises against feedback. ``memory_tool`` (when given) is added to the belt so
    facts persist across turns/components. ``exclude_tools`` drops named tools from the belt (used to
    clamp the review sub-agent's exploration — docs/crucible-judge-cost.md §3)."""
    tools = [t for t in (getattr(env, "all_tools", None) or env.rag_tools) if t.name not in exclude_tools]
    if memory_tool is not None:
        tools.append(memory_tool)
    system, instruction = _split_prompt(messages)
    doc = "Your complete final answer as a single string (e.g. the authored source file)."
    if judge is None:
        builder = bind_standard(env.builder_heavy(), _LlmState, doc=doc)
        state_input: FlowInput = _LlmInput(input=[])
    else:
        builder = bind_standard(env.builder_heavy(), _JudgedAuthorState, doc=doc, validator=_review_gate)
        tools = [*tools, _RequestReview.bind(judge).as_tool("request_review")]
        system = (system or _DEFAULT_SYS_PROMPT) + _REVIEW_PROTOCOL
        state_input = _LlmInput(input=[])
    graph: Any = (
        builder
        .with_input(_LlmInput)
        .with_sys_prompt(system or _DEFAULT_SYS_PROMPT)
        .with_initial_prompt(instruction)
        .with_tools(tools)
        .compile_async()
    )
    res = await run_to_completion(
        graph,
        state_input,
        thread_id=uniq_thread_id(f"{backend_name}-llm"),
        recursion_limit=recursion_limit,
        description=f"{backend_name} {turn_label} turn",
    )
    result = res.get("result")
    return result if isinstance(result, str) else json.dumps(result)


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
    """Interpret a judge reply as (accept, feedback). Accepts a JSON ``{accept, feedback}`` (what
    the Crucible judge emits) or a plain reply led by ``ACCEPT`` / ``REJECT``."""
    try:
        obj = json.loads(review)
        if isinstance(obj, dict):
            return bool(obj.get("accept")), str(obj.get("feedback", ""))
    except (json.JSONDecodeError, ValueError):
        pass
    return (not review.strip().upper().startswith("REJECT")), review


async def _author_turn(
    module: Any, input_json: str, failure: dict | None, *, env: Any, recursion_limit: int,
    backend_name: str, judge: "_JudgeHook | None" = None, memory_tool: Any = None,
) -> str:
    """One authoring turn: render the backend's prompt (with any prior failure as revise
    context), run the tool-enabled LLM agent, and strip a code fence off the result. When
    ``judge`` is given, the author reviews and self-revises in-session (docs/crucible-judge-in-loop.md)."""
    prompt = json.loads(
        module.author_prompt(input_json, json.dumps(failure) if failure is not None else None)
    )
    reply = await run_llm_agent(
        env, prompt, recursion_limit=recursion_limit, backend_name=backend_name,
        judge=judge, memory_tool=memory_tool,
    )
    return _strip_fence(reply)


# The review sub-agent gets the program API + fixture in its prompt and shares the run memory, so
# it doesn't need the expensive `code_explorer` exploration sub-agent — direct file reads
# (`get_file`/`grep`) cover its spot-checks. Dropping it is the bulk of the review cost
# (docs/crucible-judge-cost.md §3): each `code_explorer` call is itself a multi-call sub-agent.
_JUDGE_EXCLUDE_TOOLS = frozenset({"code_explorer"})


async def _judge_turn(
    module: Any, input_json: str, spec: str, *, env: Any, recursion_limit: int, backend_name: str,
    emit: Callable[[str, dict], None] | None = None, memory_tool: Any = None,
) -> tuple[bool, str]:
    """Optional LLM review of a spec: ``(accept, feedback)``. ``(True, "")`` when the backend
    declares no judge (``judge_prompt`` → ``None``, the default). When a review actually runs,
    emit a ``judge`` event carrying the verdict so the frontend surfaces accept/reject."""
    jp = module.judge_prompt(input_json, spec)
    if not jp:
        return True, ""
    review = await run_llm_agent(
        env, json.loads(jp), recursion_limit=recursion_limit,
        backend_name=backend_name, turn_label="judge", memory_tool=memory_tool,
        exclude_tools=_JUDGE_EXCLUDE_TOOLS,
    )
    ok, feedback = _parse_judge(review)
    if emit is not None:
        emit("judge", {
            "line": "reviewer accepted the tests" if ok
            else f"reviewer rejected — revising: {_first_line(feedback)}",
            "outcome": "GOOD" if ok else "BAD",
        })
    return ok, feedback


def _make_judge_hook(
    module: Any, input_json: str, *, env: Any, recursion_limit: int, backend_name: str,
    emit: Callable[[str, dict], None] | None, memory_tool: Any,
) -> "_JudgeHook":
    """Wrap the wheel's judge as a ``(draft) -> (accepted, feedback)`` callable for the in-loop
    ``request_review`` tool. Reuses :func:`_judge_turn` so the verdict event still fires."""
    async def judge(draft: str) -> tuple[bool, str]:
        return await _judge_turn(
            module, input_json, draft, env=env, recursion_limit=recursion_limit,
            backend_name=backend_name, emit=emit, memory_tool=memory_tool,
        )
    return judge


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
    that have no report units to validate — e.g. Crucible's shared setup fixture (a compile-only
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
            module, input_json, spec, env=env, recursion_limit=recursion_limit,
            backend_name=backend_name, emit=emit,
        )
        if not ok:
            failure = {"draft": spec, "errors": feedback, "kind": "judge"}
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


def _confined_target(root: Path, rel: str) -> Path:
    """Join a wheel-supplied relative path under ``root``, rejecting absolute paths / ``..``
    traversal — mirrors the Rust ``confined_join`` so host-written deliverable/prep files stay
    inside the project (the wheel is trusted, but defense-in-depth is cheap)."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe file path {rel!r}: absolute or traverses outside the workdir")
    return root / p


async def run_workspace_prep(
    module: Any,
    input_dict: dict,
    *,
    workdir: Path,
    sandbox: SandboxConfig | None,
    command_timeout_s: int,
) -> None:
    """Execute the wheel's pure ``workspace_prep`` plan (``docs/rust-pure-app.md`` §4): write the
    declared files (path-confined), then — only when a sandbox is enabled, so a later
    confined+offline build finds its deps warm — ``cargo fetch`` each ``warm_dirs`` and build the
    named program via the shared Solana build capability.

    Network stays Python-owned and the posture is unchanged: fetches run *unconfined* (a fetch
    executes no untrusted code), the code-executing build runs *confined + offline*
    (``build_program`` handles both). The wheel supplies only file contents + which dirs/program —
    never a command line."""
    plan = json.loads(module.workspace_prep(json.dumps(input_dict)))
    for rel, contents in (plan.get("files") or {}).items():
        target = _confined_target(workdir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    warm_dirs = plan.get("warm_dirs") or []
    build_prog = plan.get("build_program")
    if not warm_dirs and not build_prog:
        return

    from composer.spec.solana.build import build_program, warm_cargo_cache

    if warm_dirs and sandbox is not None and sandbox.enabled:
        # Warm into the SAME private CARGO_HOME the confined offline build will read.
        from composer.sandbox.recipes import sandbox_cargo_home

        cargo_home = sandbox_cargo_home(str(workdir))
        for d in warm_dirs:
            await warm_cargo_cache(
                _confined_target(workdir, d), cargo_home=cargo_home, timeout_s=command_timeout_s
            )
    if build_prog:
        await build_program(str(workdir), build_prog, timeout_s=command_timeout_s, sandbox=sandbox)


# ---------------------------------------------------------------------------
# The formalizer.
# ---------------------------------------------------------------------------

class RustFormalizer(Formalizer[RustFormalResult, FeatureUnit]):
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
        command_sem: asyncio.Semaphore | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        context_extra: dict | None = None,
        setup_result: str | None = None,
    ):
        super().__init__(RustFormalResult, as_report_backend(descriptor.backend_tag))
        self._module = module
        self._descriptor = descriptor
        self._sandbox = sandbox
        self._command_timeout_s = command_timeout_s
        self._command_sem = command_sem
        self._max_attempts = max_attempts
        # Injected into every component's ``AuthorInput.context`` (declared-arg values + the
        # compiled setup artifact under its ``context_key``); the prepared system assembles it.
        self._context_extra = context_extra or {}
        # The compiled setup spec (Crucible's fixture), forwarded to ``finalize`` so a
        # callout-mode wheel can render the whole deliverable.
        self._setup_result = setup_result

    # -- hooks an application backend may override -------------------------

    def _context(self, run: PipelineRun) -> dict:
        """The ``AuthorInput.context`` blob for a component. The program plus whatever the
        prepared system injected (declared args + the setup artifact under its context key)."""
        return {"program": str(run.source.contract_name), **self._context_extra}

    def _before_formalize(self, feat: FeatureUnit, slugs: list[str]) -> None:
        """Place any crate scaffolding before compile/validate. Base: nothing (the wheel
        materializes its crate per confined run via the ``files`` map)."""
        return None

    async def _sandbox_spec(self, workdir: Path) -> dict:
        if self._sandbox is None or not self._sandbox.enabled:
            return {"argv_prefix": [], "timeout_s": self._command_timeout_s}
        return await self._sandbox.backend_spec(workdir, timeout_s=self._command_timeout_s)

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
        sandbox_dict = await self._sandbox_spec(workdir)
        sandbox_json = json.dumps(sandbox_dict)
        emit = make_emitter()
        units = json.loads(self._module.units(input_json))

        # When the wheel supplies a judge for this input, it runs in-loop: a `request_review` tool
        # inside the author session, which self-revises against feedback and can only finalize an
        # accepted draft (docs/crucible-judge-in-loop.md). The author and judge share the run
        # memory across components. Probe the pure callout — `judge_prompt` returns None exactly
        # when there is no judge for this kind, so no review machinery is bound then.
        has_judge = self._module.judge_prompt(input_json, "") is not None
        memory_tool = ctx.get_memory_tool() if has_judge else None
        judge_hook = _make_judge_hook(
            self._module, input_json, env=run.env, recursion_limit=ctx.recursion_limit,
            backend_name=self._descriptor.name, emit=emit, memory_tool=memory_tool,
        ) if has_judge else None

        # Fused author → validate loop: validate's build IS the compile gate (no separate dry-run
        # per component — that ~2×'d the e2e). The units share one build, so a BuildFailed from any
        # unit re-authors the whole spec.
        failure: dict | None = None
        for _ in range(self._max_attempts):
            spec = await _author_turn(
                self._module, input_json, failure, env=run.env,
                recursion_limit=ctx.recursion_limit, backend_name=self._descriptor.name,
                judge=judge_hook, memory_tool=memory_tool,
            )

            # Each report unit declares the *target* that validates it (its own name by default;
            # e.g. Crucible shares one `c_invariants` target across all its units). Run each
            # DISTINCT target once; the backend returns a verdict per unit it covers — it owns
            # attribution (how a failure maps to units), the host records verbatim.
            targets = list(dict.fromkeys(u.get("target") or u["unit"] for u in units))
            prop_of = {u["unit"]: u["property"] for u in units}

            verdicts: dict[str, dict] = {}
            property_units: list[tuple[str, list[str]]] = []
            build_failed: str | None = None
            for target in targets:
                res = json.loads(
                    await _run_blocking(
                        lambda target=target, spec=spec: self._module.validate(
                            input_json, spec, target, str(workdir), sandbox_json
                        ),
                        self._command_sem,
                    )
                )
                if res.get("kind") == "build_failed":
                    build_failed = res.get("errors", "")
                    break
                for unit, verdict in res["verdicts"]:
                    verdicts[unit] = verdict
                    prop = prop_of.get(unit, unit)
                    property_units.append((prop, [unit]))
                    detail = verdict.get("detail")
                    line = f'{prop}: {verdict.get("outcome")}'
                    emit(
                        "verdict",
                        {"outcome": verdict.get("outcome"), "name": prop,
                         "line": f"{line} — {detail}" if detail else line},
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
                message=v.get("detail"),
            )
            for unit, v in formalized.result.verdicts.items()
        }

    @override
    async def finalize(self, outcomes, run: PipelineRun) -> None:
        from composer.pipeline.core import Delivered

        components = []
        for o in outcomes:
            res = o.result
            entry: dict = {"name": o.feat.display_name, "delivered": isinstance(res, Delivered)}
            if isinstance(res, Delivered):
                # A callout-mode wheel renders the whole deliverable from these (Crucible: folds
                # each section into the shared crate, keyed by its property_units feature).
                entry["unit_file"] = res.unit_file
                entry["run_link"] = res.run_link
                entry["artifact_text"] = res.result.artifact_text
                entry["property_units"] = res.result.property_units()
            components.append(entry)
        payload = {
            "program": str(run.source.contract_name),
            "components": components,
            "setup": self._setup_result,
        }
        raw = await asyncio.to_thread(self._module.finalize, json.dumps(payload))
        if not raw:
            return
        files: dict[str, str] = json.loads(raw)
        root = Path(run.source.project_root)
        for rel, contents in files.items():
            target = _confined_target(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)


@dataclass
class RustPreparedSystem(PreparedSystem[RustFormalResult, FeatureUnit]):
    """Generic prepared system, descriptor-driven: run the wheel's workspace prep, author the
    optional shared ``setup`` artifact, and build a formalizer carrying the injected context.

    Fully expresses what Crucible used to need a subclass for (``docs/rust-pure-app.md``): the
    shared fixture, the harness warm + ``.so`` build, per-run serialization, and the
    context-thread of the fixture + declared args."""

    backend: "RustBackend"
    analyzed: BaseApplication | None = None

    @override
    async def prepare_formalization(self, run: PipelineRun) -> Formalizer[RustFormalResult, FeatureUnit]:
        b = self.backend
        descriptor = b.descriptor
        workdir = Path(run.source.project_root)
        program = str(run.source.contract_name)
        # One shared crate / target dir → serialize the toolchain runs (declared by the wheel).
        command_sem = asyncio.Semaphore(1) if descriptor.serialize_toolchain else None

        analyzed_json = self.analyzed.model_dump(mode="json") if self.analyzed is not None else {}
        prep_input = {
            "kind": "setup", "program": program, "component": analyzed_json,
            "props": [], "context": {},
        }

        # 1. Workspace prep: write the wheel's manifest, warm deps, build the program.
        await run_workspace_prep(
            b.module, prep_input, workdir=workdir,
            sandbox=b.sandbox, command_timeout_s=b.command_timeout_s,
        )

        # 2. Every component's context = declared args + (optionally) the compiled setup artifact.
        context_extra: dict = dict(b.declared_args)
        setup_result: str | None = None
        if descriptor.setup is not None:
            sandbox_dict = (
                await b.sandbox.backend_spec(workdir, timeout_s=b.command_timeout_s)
                if (b.sandbox is not None and b.sandbox.enabled)
                else {"argv_prefix": [], "timeout_s": b.command_timeout_s}
            )
            emit = make_emitter()
            fixture = await run.runner(
                TaskInfo(
                    f"{descriptor.name}-setup", descriptor.setup.label,
                    cast(Any, b._phase)[descriptor.setup.phase_key],
                ),
                lambda: author_and_compile(
                    b.module, prep_input, env=run.env, sandbox_dict=sandbox_dict,
                    workdir=workdir, recursion_limit=run.ctx.recursion_limit,
                    backend_name=descriptor.name, emit=emit, command_sem=command_sem,
                ),
            )
            if isinstance(fixture, GaveUp):
                raise RuntimeError(f"{descriptor.name} setup gave up: {fixture.reason}")
            setup_result = fixture
            context_extra[descriptor.setup.context_key] = fixture

        return RustFormalizer(
            b.module, b.descriptor, sandbox=b.sandbox,
            command_timeout_s=b.command_timeout_s,
            command_sem=command_sem, context_extra=context_extra, setup_result=setup_result,
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
    # Wall-clock ceiling for a single compile/validate (a first build can be minutes).
    command_timeout_s: int = DEFAULT_TIMEOUT_S
    # How to confine every toolchain run (docs/command-sandbox.md). None → unsandboxed.
    sandbox: SandboxConfig | None = None
    # Parsed values of the descriptor's declared CLI args, injected into every component's
    # ``AuthorInput.context`` (e.g. Crucible's ``fuzz_timeout``). Set by the entry point.
    declared_args: dict[str, Any] = field(default_factory=dict)

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
    ) -> PreparedSystem[RustFormalResult, FeatureUnit]:
        return RustPreparedSystem(
            self.ecosystem.locate_main(analyzed, run.source), self, analyzed
        )

    def to_artifact_id(self, c: FeatureUnit) -> RustArtifact:
        return RustArtifact(
            c.slug,
            self.descriptor.artifact_layout.artifact_prefix,
            self.descriptor.artifact_layout.artifact_extension,
        )
