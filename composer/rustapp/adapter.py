"""Adapter: wrap a Rust wheel (a :class:`~autoprover_sdk.Backend`) as a
:class:`~composer.pipeline.core.PipelineBackend`.

The Rust wheel is a **passive service** (``docs/rust-applications.md``): Python owns the
author→compile→judge→validate loop and every LLM turn, and calls the wheel's pure callouts
(``descriptor`` / ``units`` / ``author_prompt`` / ``judge_prompt`` / ``finalize``) plus the two
blocking ones (``compile`` / ``validate``) that run the toolchain via ``run-confined``. There is
no IoC ``resume`` loop and no ``Effects`` protocol.

Three phase objects mirror the CVL / foundry backends:

* :class:`RustBackend`        — ``PipelineBackend`` (guidance, phases, store, ``preflight`` /
  ``prepare_system``).
* :class:`RustPreparedSystem` — builds the formalizer (thin; no app-specific setup).
* :class:`RustFormalizer`     — ``formalize`` runs the loop; ``fetch_verdicts`` reads the verdicts
  ``validate`` baked into the result.

App-specific orchestration (a shared setup artifact, workspace prep + its gate, crate assembly) is
descriptor-driven here — no per-application Python package (``docs/rust-applications.md``): the wheel
declares ``preflight`` / ``setup`` / ``workspace_prep`` / ``deliverable_mode=callout`` / ``finalize``
and the generic host runs them.

The two build-shaped steps are overlapped with the LLM steps that don't need them:
:meth:`RustBackend.preflight` (prepare the workspace, then *gate* it — a wheel-authored skeleton
built by the real toolchain) runs alongside system analysis, and the shared setup artifact is
authored after extraction, when the properties it must make checkable finally exist.
"""

import asyncio
import enum
import json
import logging
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, NotRequired, override

from langchain_core.tools import BaseTool
from pydantic import Field
from graphcore.graph import FlowInput, tool_state_update
from graphcore.tools.schemas import WithAsyncDependencies, WithInjectedId
from langgraph.graph import MessagesState
from langgraph.types import Command

from composer.diagnostics.timing import get_current_task_id
from composer.io.context import push_custom_update
from composer.io.multi_job import TaskInfo
from composer.pipeline.core import (
    BackendJob,
    ComponentOutcome,
    CorePhases,
    Delivered,
    Formalizer,
    GaveUp,
    PipelineBackend,
    PipelineRun,
    PreparedSystem,
    StagedFormalizer,
    SystemAnalysisSpec,
)
from composer.pipeline.ecosystem import ChainTag, Ecosystem
from composer.sandbox.command import DEFAULT_TIMEOUT_S
from composer.sandbox.config import BackendSpec, SandboxConfig
from composer.rustapp.descriptor import AppDescriptor, PhaseRole, PhaseSpec
from composer.rustapp.result import RustArtifact, RustFormalResult, RustSetupArtifact
from composer.rustapp.toolchain import project_toolchain, source_unit
from composer.rustapp.wire import (
    AuthorInput,
    CompileOk,
    ComponentGaveUp,
    ComponentInput,
    Failure,
    FailureKind,
    FinalizeComponent,
    FinalizeInput,
    PreflightInput,
    Prompt,
    Property,
    RustAppModule,
    SetupInput,
    Target,
    ValidateBuildFailed,
    parse_compile,
    parse_files,
    parse_prompt,
    parse_units,
    parse_validate,
    parse_workspace_prep,
)
# The wheel's per-unit verdict and the *report's* per-unit verdict are different types with the same
# name (``fetch_verdicts`` maps one to the other), so the wire one is aliased here. Likewise
# ``Delivered``: the pipeline's component outcome and the wire's payload for one.
from composer.rustapp.wire import Delivered as WireDelivered
from composer.rustapp.wire import Verdict as WireVerdict
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import CacheKey, SourceFields, WorkflowContext
from composer.spec.service_host import ServiceHost
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.ui.tool_display import tool_display
from composer.spec.source.report.schema import Outcome, RuleName
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import PropertyFormulation
from composer.spec.util import slugify_filename, string_hash, uniq_thread_id

_log = logging.getLogger(__name__)

# Author→compile revise attempts per artifact.
DEFAULT_MAX_ATTEMPTS = 7

# ---------------------------------------------------------------------------
# The LLM authoring turn — the "author" step of the author→compile→judge→validate loop, and
# the peer of composer/foundry/author.py. Python runs it; the backend only supplies the
# prompt, through its author_prompt/judge_prompt callouts.
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


# WARNING: `bind_standard` introspects this state class's ``__annotations__`` at runtime to
# unwrap ``result: NotRequired[T]``, so the annotation must stay a real object — a stringized
# one breaks the unwrap.
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


@dataclass(frozen=True)
class Accepted:
    """The reviewer accepted the draft. ``feedback`` is whatever it said while accepting — often
    empty, and never something the author has to act on."""

    feedback: str = ""


@dataclass(frozen=True)
class Rejected:
    """The reviewer rejected the draft. ``feedback`` is what to revise against, and is the only
    thing the next authoring turn gets told about the rejection — so it is never empty."""

    feedback: str


#: One review's verdict. A wheel that declares no judge produces ``None`` instead — "nothing to
#: review", which is not a verdict and must not be read as a favourable one.
type Review = Accepted | Rejected

# Reviews a candidate artifact. The host builds it (see :func:`_make_judge_hook`) around the wheel's
# ``judge_prompt``; it only exists when the wheel declared a judge for that input, so unlike
# :func:`_judge_turn` it always produces a verdict.
type _JudgeHook = Callable[[str], Awaitable[Review]]

# Authors the shared setup artifact for a run, given the properties it must make checkable. Built
# by :class:`RustPreparedSystem` and called from :meth:`RustStagedFormalizer.begin` — see there for
# why it runs between extraction and the per-unit fan-out rather than during prep or on first use.
type SetupAuthor = Callable[[list[PropertyFormulation], PipelineRun], Awaitable[str]]


# How many times one authoring session may be reviewed before its draft is taken as-is. The loop
# has to be bounded because it can be *unwinnable*: when the reviewer's objection is something the
# author has no power to change, "revise and re-review" never converges — it just re-spends a
# whole (and steadily growing) context per round until the graph's recursion limit. Three rounds
# buys the revision the feedback is for; the compile gate and the fuzzer still judge the result.
MAX_REVIEW_ROUNDS = 3


@dataclass
class _ReviewBudget:
    """The review rounds left in one authoring session, shared by the judge hook and the gate."""

    limit: int = MAX_REVIEW_ROUNDS
    used: int = 0

    @property
    def spent(self) -> bool:
        return self.used >= self.limit

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)


# Appended to the system prompt when the judge runs in-loop, so the author knows the review
# protocol and the finalize gate. Kept generic (no backend/domain specifics).
_REVIEW_PROTOCOL = (
    "\n\nBefore you finalize, a reviewer must accept your work. Call the `request_review` tool "
    "with the exact artifact text you intend to submit; if the review is REJECTED, revise and "
    "call `request_review` again. Only call `result` with a draft the reviewer ACCEPTED — the "
    f"`result` call is rejected otherwise. You get at most {MAX_REVIEW_ROUNDS} reviews: if a "
    "concern is one you cannot address (it needs something outside what this task lets you "
    "change), say so in a comment in the artifact rather than asserting something weaker to "
    "satisfy it — a note the next stage can act on beats a check that only looks like one."
)


def _review_gate(
    state: _JudgedAuthorState, result_value: str, budget: _ReviewBudget
) -> str | None:
    """Block ``result`` until the judge has accepted this exact draft — or the budget is spent.

    Spending the budget releases the gate deliberately: the alternative is an author that cannot
    finalize and cannot improve, cycling review→result→review at full context each time."""
    if budget.spent or (state.get("review_ok") and state.get("reviewed_text") == result_value):
        return None
    return (
        "Not accepted yet: call `request_review` with this exact draft and get an ACCEPTED "
        f"review before calling `result` ({budget.left} review(s) left; revise first — after the "
        "last one your draft is submitted as-is)."
    )


def _budgeted(judge: "_JudgeHook", budget: _ReviewBudget) -> "_JudgeHook":
    """``judge``, counted against ``budget`` — and on the last round, relenting.

    Relenting is a real :class:`Accepted`, not a rejection dressed up as one: the author's gate opens
    and the draft goes forward. What it carries is the honest reason — the open concerns, labelled as
    unresolved — which is what the author reads and what lands in the transcript for a human."""

    async def reviewed(draft: str) -> Review:
        budget.used += 1
        review = await judge(draft)
        if isinstance(review, Accepted) or not budget.spent:
            return review
        _log.warning(
            "review budget spent (%d rounds) with the draft still rejected; submitting as-is",
            budget.used,
        )
        return Accepted(
            f"{review.feedback}\n\nNo review rounds left ({budget.used} used) — this draft is being "
            "submitted with the concerns above unresolved. Call `result` with it now."
        )

    return reviewed


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
            review = await judge(self.draft)
        accepted = isinstance(review, Accepted)
        verdict = "ACCEPTED" if accepted else "REJECTED"
        content = (
            f"Review {verdict}.\n\n{review.feedback}" if review.feedback else f"Review {verdict}."
        )
        return tool_state_update(
            tool_call_id=self.tool_call_id, content=content,
            reviewed_text=self.draft, review_ok=accepted,
        )


async def run_llm_agent(
    env: ServiceHost, prompt: Prompt, *, recursion_limit: int, backend_name: str = "rust",
    turn_label: str = "authoring", judge: "_JudgeHook | None" = None,
    memory_tool: BaseTool | None = None, exclude_tools: frozenset[str] = frozenset(),
) -> str | None:
    """Run one bounded, tool-enabled turn and return its final text, or ``None`` if the turn ended
    without one. ``turn_label`` names the turn's role ("authoring" / "judge") for the UI/log panel.

    Binds the env's tool belt (source navigation + RAG search over the backend's
    knowledge base) and a result tool, and runs an agent to completion — so the
    prompt can pull in framework docs / read the program. Must run inside a
    ``with_handler`` scope (the caller wraps it in ``run.runner``).

    When ``judge`` is given, the turn becomes an in-loop-review author (docs/crucible-judge-in-loop.md (PR3)):
    a ``request_review`` tool runs the judge in-session and ``result`` is gated on an accepted draft,
    so the author self-revises against feedback. ``memory_tool`` (when given) is added to the belt so
    facts persist across turns/components. ``exclude_tools`` drops named tools from the belt (used to
    clamp the review sub-agent's exploration — docs/crucible-judge-cost.md (PR3) §3)."""
    tools = [t for t in env.all_tools if t.name not in exclude_tools]
    if memory_tool is not None:
        tools.append(memory_tool)
    system = prompt.system
    doc = "Your complete final answer as a single string (e.g. the authored source file)."
    if judge is None:
        builder = bind_standard(env.builder_heavy(), _LlmState, doc=doc)
        state_input: FlowInput = _LlmInput(input=[])
    else:
        # One budget per session, shared by the hook (which counts and finally relents) and the
        # gate (which stops blocking once it is spent) — so the two can't disagree about when the
        # author is allowed to finalize.
        budget = _ReviewBudget()

        def gate(state: _JudgedAuthorState, value: str) -> str | None:
            return _review_gate(state, value, budget)

        builder = bind_standard(env.builder_heavy(), _JudgedAuthorState, doc=doc, validator=gate)
        tools = [*tools, _RequestReview.bind(_budgeted(judge, budget)).as_tool("request_review")]
        system = (system or _DEFAULT_SYS_PROMPT) + _REVIEW_PROTOCOL
        state_input = _LlmInput(input=[])
    graph: Any = (
        builder
        .with_input(_LlmInput)
        .with_sys_prompt(system or _DEFAULT_SYS_PROMPT)
        .with_initial_prompt(prompt.instruction)
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
    # ``result`` is ``NotRequired``: the agent may end its turn without ever calling the result tool
    # (it ran out of recursion, or just stopped). ``None`` says so, rather than handing the caller a
    # placeholder the toolchain would spend an attempt building.
    return result if isinstance(result, str) else None


# ---------------------------------------------------------------------------
# Shared loop helpers (used by RustFormalizer.formalize and app setup artifacts).
# ---------------------------------------------------------------------------

def make_emitter() -> Callable[[str, dict], None]:
    """A ``emit(kind, payload)`` that streams a domain event to the current task's panel.
    Routes out-of-graph (the loop isn't inside a LangGraph run) via ``push_custom_update``,
    keyed by the active ``run_task`` id."""

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


def _properties(props: list[PropertyFormulation], slugs: list[str]) -> list[Property]:
    """The wire form of the properties one artifact must make checkable, each with the host-assigned
    slug that names its unit (see :func:`unique_slugs`)."""
    return [
        Property(title=p.title, sort=p.sort, description=p.description, slug=s)
        for p, s in zip(props, slugs)
    ]


def _first_line(s: str) -> str:
    return next((ln for ln in s.splitlines() if ln.strip()), "").strip()


#: What a rejection with nothing to revise against is reported as. A judge that says "no" and gives
#: no reason would otherwise send the author into a revise round with an empty instruction; saying so
#: at least tells it (and a human reading the transcript) what happened.
_UNEXPLAINED_REJECTION = (
    "The reviewer rejected the draft without giving a reason. Re-read the task's criteria and "
    "strengthen whatever you are least sure of."
)


def _parse_judge(reply: str) -> Review:
    """Interpret a judge's reply as a verdict. A JSON ``{accept, feedback}`` (what the Crucible judge
    emits) is authoritative; otherwise the reply is read as prose led by ``ACCEPT`` / ``REJECT``.

    NOTE: prose that leads with neither is taken as an **acceptance** — the reviewer is an advisory
    gate in front of the compile/validate gates that actually decide, so an unparseable reply lets
    the draft through rather than burning a revise round on a verdict nobody stated. Flipping that
    default is a policy decision, not a cleanup."""
    try:
        obj = json.loads(reply)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        feedback = str(obj.get("feedback", ""))
        return Accepted(feedback) if obj.get("accept") else Rejected(feedback or _UNEXPLAINED_REJECTION)
    return Rejected(reply) if reply.strip().upper().startswith("REJECT") else Accepted(reply)


#: What the next authoring turn is told when the previous one produced no artifact at all. It is a
#: failure like any other — it costs an attempt — but the toolchain never sees it, because there is
#: nothing to build.
_NO_ARTIFACT = (
    "The previous attempt ended without calling the `result` tool, so it produced no artifact. "
    "Explore less and finalize: call `result` with your complete artifact source."
)


async def _author_turn(
    module: RustAppModule, input_json: str, failure: Failure | None, *, env: ServiceHost,
    recursion_limit: int, backend_name: str, judge: "_JudgeHook | None" = None,
    memory_tool: BaseTool | None = None,
) -> str | None:
    """One authoring turn: render the backend's prompt (with any prior failure as revise
    context), run the tool-enabled LLM agent, and strip a code fence off the result. ``None`` when
    the agent ended its turn without producing one. When ``judge`` is given, the author reviews and
    self-revises in-session (docs/crucible-judge-in-loop.md (PR3))."""
    prompt = parse_prompt(
        module.author_prompt(input_json, failure.model_dump_json() if failure is not None else None)
    )
    reply = await run_llm_agent(
        env, prompt, recursion_limit=recursion_limit, backend_name=backend_name,
        judge=judge, memory_tool=memory_tool,
    )
    return _strip_fence(reply) if reply is not None else None


# The review sub-agent gets the program API + fixture in its prompt and shares the run memory, so
# it doesn't need the expensive `code_explorer` exploration sub-agent — direct file reads
# (`get_file`/`grep`) cover its spot-checks. Dropping it is the bulk of the review cost
# (docs/crucible-judge-cost.md (PR3) §3): each `code_explorer` call is itself a multi-call sub-agent.
_JUDGE_EXCLUDE_TOOLS = frozenset({"code_explorer"})


async def _judge_turn(
    module: RustAppModule, input_json: str, spec: str, *, env: ServiceHost, recursion_limit: int,
    backend_name: str, emit: Callable[[str, dict], None] | None = None,
    memory_tool: BaseTool | None = None,
) -> Review | None:
    """One optional LLM review of a spec. ``None`` — *no verdict*, not a favourable one — when the
    backend declares no judge for this input (``judge_prompt`` → ``None``, the default).

    When a review does run, emit a ``judge`` event carrying the verdict so the frontend surfaces
    accept/reject."""
    jp = module.judge_prompt(input_json, spec)
    if not jp:
        return None
    reply = await run_llm_agent(
        env, parse_prompt(jp), recursion_limit=recursion_limit,
        backend_name=backend_name, turn_label="judge", memory_tool=memory_tool,
        exclude_tools=_JUDGE_EXCLUDE_TOOLS,
    )
    if reply is None:
        # A reviewer that never stated a verdict has not rejected anything. Same reasoning as an
        # unparseable reply (see :func:`_parse_judge`): it is an advisory gate, so it fails open.
        _log.warning("%s: judge turn ended without a verdict; treating as accepted", backend_name)
        return Accepted()
    review = _parse_judge(reply)
    accepted = isinstance(review, Accepted)
    if emit is not None:
        emit("judge", {
            "line": "reviewer accepted the tests" if accepted
            else f"reviewer rejected — revising: {_first_line(review.feedback)}",
            "outcome": (Outcome.GOOD if accepted else Outcome.BAD).value,
        })
    return review


def _make_judge_hook(
    module: RustAppModule, input_json: str, *, env: ServiceHost, recursion_limit: int,
    backend_name: str, emit: Callable[[str, dict], None] | None, memory_tool: BaseTool | None,
) -> "_JudgeHook":
    """Wrap the wheel's judge as a ``(draft) -> Review`` callable for the in-loop ``request_review``
    tool. Reuses :func:`_judge_turn` so the verdict event still fires."""
    async def judge(draft: str) -> Review:
        review = await _judge_turn(
            module, input_json, draft, env=env, recursion_limit=recursion_limit,
            backend_name=backend_name, emit=emit, memory_tool=memory_tool,
        )
        # This hook is only built for an input the wheel *did* declare a judge for, and
        # ``judge_prompt`` is pure — so "no verdict" can't happen here. If it somehow did, a review
        # that isn't running must not be what keeps the author from finalizing.
        return review if review is not None else Accepted()
    return judge


async def author_and_compile(
    module: RustAppModule,
    input: AuthorInput,
    *,
    env: ServiceHost,
    sandbox_dict: BackendSpec,
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
    input_json = input.model_dump_json()
    sandbox_json = json.dumps(sandbox_dict)
    failure: Failure | None = None
    for _ in range(max_attempts):
        spec = await _author_turn(
            module, input_json, failure, env=env, recursion_limit=recursion_limit, backend_name=backend_name
        )
        if spec is None:
            _log.warning("%s: authoring turn produced no artifact; retrying", backend_name)
            failure = Failure(draft="", errors=_NO_ARTIFACT)
            continue
        result = parse_compile(
            await _run_blocking(
                # ``spec=spec`` binds this attempt's draft (the name is rebound each round), the
                # same capture the per-target ``validate`` loop makes.
                lambda spec=spec: module.compile(input_json, spec, str(workdir), sandbox_json),
                command_sem,
            )
        )
        if not isinstance(result, CompileOk):
            failure = Failure(draft=spec, errors=result.errors)
            emit("build_output", {"line": _first_line(result.errors) or "build failed; revising"})
            continue
        review = await _judge_turn(
            module, input_json, spec, env=env, recursion_limit=recursion_limit,
            backend_name=backend_name, emit=emit,
        )
        # No judge (``None``) and an accepting one both mean "nothing left to fix" — only an actual
        # rejection sends this back for a revise, and it is flagged as a *judge* failure so the next
        # prompt frames it as review feedback rather than as compiler errors.
        if isinstance(review, Rejected):
            failure = Failure(draft=spec, errors=review.feedback, kind=FailureKind.JUDGE)
            continue
        return spec
    return GaveUp(reason=f"{backend_name}: did not pass compile/judge in {max_attempts} attempts")


async def _run_blocking(thunk: Callable[[], str], sem: asyncio.Semaphore | None) -> str:
    """Run a blocking wheel call (``compile``/``validate`` — they spawn ``run-confined`` and
    release the GIL) off the event loop, serialized by ``sem`` when the backend shares one
    workdir/crate across concurrent units."""
    guard = sem if sem is not None else nullcontext()
    async with guard:
        return await asyncio.to_thread(thunk)


def confined_target(root: Path, rel: str) -> Path:
    """Join a wheel-supplied relative path under ``root``, rejecting absolute paths / ``..``
    traversal — mirrors the Rust ``confined_join`` so host-written deliverable/prep files stay
    inside the project (the wheel is trusted, but defense-in-depth is cheap).

    Public because the toolchain half of a workspace prep lives outside this module
    (:class:`~composer.rustapp.toolchain.ProjectToolchain`) and whatever it writes must be confined
    exactly as the host's own writes are."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe file path {rel!r}: absolute or traverses outside the workdir")
    return root / p


def source_unit_of(
    ecosystem: Ecosystem[Any, Any, Any], source: SourceFields
) -> dict[str, Any]:
    """The ``AuthorInput.source_unit`` field — where the code under analysis lives as a unit of its
    own build system — from the chain's registered toolchain
    (:func:`composer.rustapp.toolchain.source_unit`).

    A wheel that must *depend on* the analyzed code (Crucible's harness path-depends on the program
    under test) reads this instead of deriving a directory or package name from
    ``source.contract_name``, which is only the analysis identifier. Empty when the chain has no
    toolchain, when the language has no such unit (Solidity), or when the layout couldn't be read —
    all three mean the same thing to the wheel, which then applies its own convention.
    """
    return source_unit(ecosystem.name, source)


def _setup_identity(input: SetupInput) -> str:
    """A cache key for the shared setup artifact: a hash of what it is authored *from*.

    Exactly the inputs the wheel renders the artifact from — the program, the project facts (where its
    code lives, and what the prep established, which is what decides where its types come from), the
    analyzed model, and the properties it has to make checkable. Deliberately NOT the whole input:
    ``args`` also carries run knobs (a fuzz budget) that don't change what gets authored, and keying
    on those would throw the artifact away for no reason.
    """
    material = {
        "program": input.program,
        "source_unit": input.source_unit,
        "prep_facts": input.prep_facts,
        "model": input.model,
        "props": [p.model_dump() for p in input.props],
    }
    return string_hash(json.dumps(material, sort_keys=True, default=str))


async def run_workspace_prep(
    module: RustAppModule,
    input: AuthorInput,
    *,
    chain: ChainTag,
    source: SourceFields,
    sandbox: SandboxConfig | None,
    command_timeout_s: int,
) -> dict[str, Any]:
    """Execute the wheel's pure ``workspace_prep`` plan (``docs/rust-applications.md`` §7): write the
    declared files (path-confined) under ``source.project_root``, then hand the plan's
    ``toolchain_request`` to ``chain``'s registered
    :class:`~composer.rustapp.toolchain.ProjectToolchain`. Returns what the prep established, which
    the caller reports back to the wheel as ``AuthorInput.prep_facts`` — empty when the plan only
    placed files.

    The split is the seam: writing files is the same in every ecosystem, while preparing a *project*
    means driving a build system the host does not understand, which only something that knows the
    chain can do (see the toolchain module for why the framework carries no implementation). Either
    way the wheel supplies only file contents + a request its chain's toolchain understands, never a
    command line, so the network posture stays Python-owned.

    The whole ``source`` goes through rather than just its root: an implementation resolves its own
    project facts from it (Solana reads the crate that owns ``relative_path`` to fill in an IDL's
    program id), which is knowledge the framework would otherwise have to hold a shape for."""
    workdir = Path(source.project_root)
    plan = parse_workspace_prep(module.workspace_prep(input.model_dump_json()))
    for rel, contents in plan.files.items():
        target = confined_target(workdir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    if not plan.needs_toolchain:
        return {}
    facts = await project_toolchain(chain).prepare(
        plan, input, source=source, sandbox=sandbox, timeout_s=command_timeout_s
    )
    if facts:
        _log.info("workspace prep established %s", facts)
    return facts


class PreflightFailed(RuntimeError):
    """The prepared workspace does not build, or its skeleton artifact does not run — established
    before any property or authored artifact exists.

    Terminal by construction: what fails here is the *workspace* (a dependency graph that won't
    resolve, a unit that won't link, codegen the generator rejects, a built program that won't load),
    and none of that is something an authoring agent can fix — it doesn't own the project's build
    files. Re-authoring against it only burns the revise budget on errors the model can't address,
    which is exactly what this gate exists to prevent."""


async def run_preflight_gate(
    module: RustAppModule,
    input: AuthorInput,
    *,
    workdir: Path,
    sandbox_dict: BackendSpec,
    emit: Callable[[str, dict], None],
) -> None:
    """Gate the prepared workspace with a ``kind="preflight"`` ``compile`` — the wheel's own
    skeleton artifact, built by the real toolchain under the real sandbox.

    The ``spec`` is empty on purpose: nothing has been authored yet (this runs alongside system
    analysis), so the wheel renders the smallest artifact that still exercises what an authored one
    will depend on. Raises :class:`PreflightFailed` with the compiler diagnostics the wheel
    extracted; there is no retry."""
    result = parse_compile(
        await asyncio.to_thread(
            module.compile, input.model_dump_json(), "", str(workdir), json.dumps(sandbox_dict)
        )
    )
    if isinstance(result, CompileOk):
        return
    emit("build_output", {"line": _first_line(result.errors) or "preflight build failed"})
    raise PreflightFailed(
        "the prepared workspace does not build (or its skeleton does not run), before anything has "
        "been authored — a toolchain, dependency or program-build problem, not something the run "
        f"can author its way around:\n{result.errors}"
    )


# ---------------------------------------------------------------------------
# The formalizer.
# ---------------------------------------------------------------------------

class RustFormalizer(Formalizer[RustFormalResult, FeatureUnit]):
    """Drives a Rust :class:`~autoprover_sdk.Backend` through the author→compile→judge→validate
    loop. Ecosystem-agnostic: the unit is any :class:`FeatureUnit`, marshalled via
    ``feature_json()``."""

    def __init__(
        self,
        module: RustAppModule,
        descriptor: AppDescriptor,
        *,
        sandbox: SandboxConfig | None = None,
        command_timeout_s: int = DEFAULT_TIMEOUT_S,
        command_sem: asyncio.Semaphore | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        declared_args: dict[str, Any] | None = None,
        setup_result: str | None = None,
        project: "ProjectFacts | None" = None,
    ):
        super().__init__(RustFormalResult, descriptor.backend_tag)
        self._module = module
        self._descriptor = descriptor
        self._sandbox = sandbox
        self._command_timeout_s = command_timeout_s
        self._command_sem = command_sem
        self._max_attempts = max_attempts
        # The run's values for the wheel's own declared flags, put on every component's input.
        self._declared_args = declared_args or {}
        # The compiled setup spec (Crucible's fixture): on every component's input, and forwarded
        # to ``finalize`` so a callout-mode wheel can render the whole deliverable. A wheel that
        # declares a ``setup`` step reaches here only through :class:`RustStagedFormalizer`, which
        # authors the artifact before constructing this.
        self._setup_result = setup_result
        # What the preflight established about the project (see :class:`ProjectFacts`), carried on
        # every ``AuthorInput`` and mirrored into ``finalize`` — what ships must name the same
        # dependency the gated builds did.
        self._project = project or ProjectFacts()

    async def _sandbox_spec(self, workdir: Path) -> BackendSpec:
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
        input_json = ComponentInput(
            program=str(run.source.contract_name),
            source_unit=self._project.source_unit,
            unit=feat.feature_json(),
            props=_properties(props, slugs),
            setup=self._setup_result,
            prep_facts=self._project.prep_facts,
            args=self._declared_args,
        ).model_dump_json()
        sandbox_dict = await self._sandbox_spec(workdir)
        sandbox_json = json.dumps(sandbox_dict)
        emit = make_emitter()
        units = parse_units(self._module.units(input_json))

        # When the wheel supplies a judge for this input, it runs in-loop: a `request_review` tool
        # inside the author session, which self-revises against feedback and can only finalize an
        # accepted draft (docs/crucible-judge-in-loop.md (PR3)). The author and judge share the run
        # memory across components. Probe the pure callout — `judge_prompt` returns None exactly
        # when there is no judge for this kind, so no review machinery is bound then.
        has_judge = self._module.judge_prompt(input_json, "") is not None
        memory_tool = ctx.get_memory_tool() if has_judge else None
        judge_hook = _make_judge_hook(
            self._module, input_json, env=run.env, recursion_limit=ctx.recursion_limit,
            backend_name=self._descriptor.name, emit=emit, memory_tool=memory_tool,
        ) if has_judge else None

        # Fused author → validate loop: validate's build IS the compile gate, so a component pays
        # for one build rather than a dry-run plus a check. The units share that build, so a
        # BuildFailed from any unit re-authors the whole spec.
        failure: Failure | None = None
        for _ in range(self._max_attempts):
            spec = await _author_turn(
                self._module, input_json, failure, env=run.env,
                recursion_limit=ctx.recursion_limit, backend_name=self._descriptor.name,
                judge=judge_hook, memory_tool=memory_tool,
            )
            if spec is None:
                _log.warning(
                    "%s: authoring turn for %s produced no artifact; retrying",
                    self._descriptor.name, label,
                )
                failure = Failure(draft="", errors=_NO_ARTIFACT)
                continue

            # Each report unit declares the *target* that validates it (its own name by default;
            # e.g. Crucible shares one `c_<component>` target across that component's units). Run each
            # DISTINCT target once, handing the wheel the rows that target covers; it returns a
            # verdict per row — it owns attribution (how a failure maps to units), the host records
            # verbatim.
            targets = list(dict.fromkeys(u.target_or_unit() for u in units))
            prop_of = {u.unit: u.property for u in units}
            covered = {
                name: Target(name=name, units=[u for u in units if u.target_or_unit() == name])
                for name in targets
            }

            verdicts: dict[str, WireVerdict] = {}
            # Grouped by property, not appended as singletons: two units checking the same property
            # are two unit names under ONE report row — as singletons they would be two rows with
            # the same key, and the store's ``dict()`` of this list would silently keep the last.
            units_by_prop: dict[str, list[str]] = {}
            build_failed: ValidateBuildFailed | None = None
            for target in targets:
                res = parse_validate(
                    await _run_blocking(
                        lambda target=covered[target], spec=spec: self._module.validate(
                            input_json, spec, target.model_dump_json(), str(workdir), sandbox_json
                        ),
                        self._command_sem,
                    )
                )
                if isinstance(res, ValidateBuildFailed):
                    build_failed = res
                    break
                for unit, verdict in res.verdicts:
                    verdicts[unit] = verdict
                    prop = prop_of.get(unit, unit)
                    units_by_prop.setdefault(prop, []).append(unit)
                    line = f"{prop}: {verdict.outcome.value}"
                    emit(
                        "verdict",
                        {"outcome": verdict.outcome.value, "name": prop,
                         "line": f"{line} — {verdict.detail}" if verdict.detail else line},
                    )
            if build_failed is not None:
                failure = Failure(draft=spec, errors=build_failed.errors)
                emit(
                    "build_output",
                    {"line": _first_line(build_failed.errors) or "build failed; revising"},
                )
                continue
            return RustFormalResult(
                artifact_text=spec, units=list(units_by_prop.items()),
                verdicts=verdicts, targets=targets,
            )

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
                outcome=v.outcome,
                line=v.line,
                duration_seconds=v.duration_seconds,
                unit_file=v.unit_file or formalized.unit_file,
                message=v.detail,
            )
            for unit, v in formalized.result.verdicts.items()
        }

    @override
    async def finalize(
        self, outcomes: list[ComponentOutcome[RustFormalResult, FeatureUnit]], run: PipelineRun
    ) -> None:
        components = [
            FinalizeComponent(name=o.feat.display_name, outcome=ComponentGaveUp())
            if not isinstance(o.result, Delivered)
            # A callout-mode wheel renders the whole deliverable from these (Crucible: folds each
            # section into the shared crate, keyed by its property_units feature) — including the
            # targets each row was validated by, which its sections and declared features key on.
            else FinalizeComponent(
                name=o.feat.display_name,
                outcome=WireDelivered(
                    unit_file=o.result.unit_file,
                    run_link=o.result.run_link,
                    artifact_text=o.result.result.artifact_text,
                    property_units=o.result.result.property_units(),
                    targets=list(o.result.result.targets),
                ),
            )
            for o in outcomes
        ]
        payload = FinalizeInput(
            program=str(run.source.contract_name),
            source_unit=self._project.source_unit,
            prep_facts=self._project.prep_facts,
            components=components,
            setup=self._setup_result,
        )
        raw = await asyncio.to_thread(self._module.finalize, payload.model_dump_json())
        if not raw:
            return
        files = parse_files(raw)
        root = Path(run.source.project_root)
        for rel, contents in files.items():
            target = confined_target(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)


@dataclass(frozen=True)
class ProjectFacts:
    """What the host established about the project under analysis — the outcome of
    :meth:`RustBackend.preflight`, handed forward to ``prepare_system`` and carried on every callout
    from there.

    Both fields are inputs every later callout needs, and neither follows from the analyzed model.
    Both are also **chain-shaped**: this is the one part of the seam whose vocabulary belongs to the
    analyzed project's build system rather than to the framework, so the host transports them without
    a schema (see :mod:`composer.rustapp.toolchain`). Carried rather than recomputed so the gated
    preflight build, every authoring turn, and the delivered artifact all agree on what they are
    building against."""

    #: Where the analyzed source lives as a unit of its own build system, from the chain's toolchain
    #: (:func:`source_unit_of`). Empty = nothing resolved, and the wheel applies its own convention.
    source_unit: dict[str, Any] = field(default_factory=dict)
    #: What the workspace prep established (:func:`run_workspace_prep`). Empty = it established
    #: nothing, which is what the wheel reads to decide how it sources the program's types.
    prep_facts: dict[str, Any] = field(default_factory=dict)


class RustStagedFormalizer(StagedFormalizer[RustFormalResult, FeatureUnit]):
    """The formalizer for a wheel that declares a ``setup`` step, before its shared artifact exists.

    ``author`` writes and compiles the artifact from the properties it must make checkable;
    ``build`` turns that artifact into the :class:`RustFormalizer` (see
    :meth:`RustPreparedSystem.prepare_formalization`, which closes over everything else the
    formalizer needs). Splitting it this way means the artifact is never assigned onto a live
    formalizer — the only formalizer that exists already has it."""

    def __init__(self, author: SetupAuthor, build: Callable[[str], RustFormalizer]):
        self._author = author
        self._build = build

    @override
    async def begin(
        self, jobs: Sequence[BackendJob[FeatureUnit]], run: PipelineRun
    ) -> RustFormalizer:
        """Author the shared setup artifact from **every** unit's properties, and hand back the
        formalizer built around it.

        Two constraints fix this point in the run. It cannot happen in ``prepare_formalization``
        (which overlaps property extraction, so no properties exist yet), and it cannot happen
        lazily on first ``formalize`` (whichever unit won the race would decide the artifact the
        rest are then told to work within — see :class:`StagedFormalizer` and
        docs/crucible-component-units.md (PR3) §8.2). The driver calls this exactly between the two.

        Properties are de-duplicated by title, keeping first-seen order: the units are disjoint, but
        two components can legitimately surface the same property, and the artifact's cache identity
        is built from this list."""
        seen: set[str] = set()
        union: list[PropertyFormulation] = []
        for job in jobs:
            for prop in job.props:
                if prop.title not in seen:
                    seen.add(prop.title)
                    union.append(prop)
        return self._build(await self._author(union, run))


@dataclass
class RustPreparedSystem(PreparedSystem[RustFormalResult, FeatureUnit, Any]):
    """Generic prepared system, descriptor-driven: author the optional shared ``setup`` artifact and
    build a formalizer carrying the injected context.

    The workspace itself was prepared and gated *before* analysis, by
    :meth:`RustBackend.preflight` — its outcome arrives here as :attr:`preflight`.

    Descriptor-driven throughout (``docs/rust-applications.md``), so an application needing a shared
    fixture, per-run serialization, or a context-thread of the fixture + declared args declares them
    rather than subclassing."""

    backend: "RustBackend"
    preflight: ProjectFacts
    analyzed: BaseApplication | None = None

    @override
    async def prepare_formalization(
        self, run: PipelineRun
    ) -> Formalizer[RustFormalResult, FeatureUnit] | StagedFormalizer[RustFormalResult, FeatureUnit]:
        """A wheel that declares no ``setup`` step gets its formalizer here. One that does gets a
        :class:`RustStagedFormalizer` instead — this method overlaps property extraction, so the
        properties its artifact must be authored from do not exist yet."""
        b = self.backend
        descriptor = b.descriptor
        workdir = Path(run.source.project_root)
        program = str(run.source.contract_name)
        # One shared workspace / build dir → serialize the toolchain runs (declared by the wheel).
        command_sem = asyncio.Semaphore(1) if descriptor.serialize_toolchain else None

        analyzed_json = self.analyzed.model_dump(mode="json") if self.analyzed is not None else {}
        project = self.preflight

        def build(setup_result: str | None) -> RustFormalizer:
            """The formalizer, around a shared setup artifact that is either already authored or
            not called for."""
            return RustFormalizer(
                b.module, b.descriptor, sandbox=b.sandbox,
                command_timeout_s=b.command_timeout_s,
                command_sem=command_sem, declared_args=b.declared_args,
                setup_result=setup_result, project=project,
            )

        setup = descriptor.step(PhaseRole.SETUP)
        if setup is None:
            return build(None)
        # The base for the setup artifact's own input; ``author_setup`` adds the properties.
        prep_input = SetupInput(
            program=program, source_unit=project.source_unit, model=analyzed_json,
            prep_facts=project.prep_facts, args=b.declared_args,
        )

        async def author_setup(props: list[PropertyFormulation], run: PipelineRun) -> str:
            # The properties are what the artifact must make checkable, so they are part of both
            # the prompt and the cache identity.
            setup_input = prep_input.with_props(_properties(props, unique_slugs(props)))
            # Cached like a formalization result (and skipped entirely on a hit): authoring +
            # compiling this is a full LLM loop, and on a large program the longest single step
            # of a run — so a re-run after a failure downstream must not pay for it twice. Keyed
            # by what it is authored *from*, so a changed model, program crate, type source
            # (crate vs IDL) or property set re-authors it. As with the driver's other caches, a
            # change to the *prompt* does not invalidate — clear the namespace for that.
            setup_ctx: WorkflowContext[RustSetupArtifact] = run.ctx.child(
                CacheKey(f"{descriptor.name}-setup-{_setup_identity(setup_input)}")
            )
            if (hit := await setup_ctx.cache_get(RustSetupArtifact)) is not None:
                return hit.source
            sandbox_dict = await b.sandbox_spec(workdir)
            emit = make_emitter()
            fixture = await run.runner(
                b.task_info(setup),
                lambda: author_and_compile(
                    b.module, setup_input, env=run.env, sandbox_dict=sandbox_dict,
                    workdir=workdir, recursion_limit=run.ctx.recursion_limit,
                    backend_name=descriptor.name, emit=emit, command_sem=command_sem,
                ),
            )
            if isinstance(fixture, GaveUp):
                raise RuntimeError(f"{descriptor.name} setup gave up: {fixture.reason}")
            await setup_ctx.cache_put(RustSetupArtifact(source=fixture))
            return fixture

        return RustStagedFormalizer(author_setup, build)


@dataclass
class RustBackend(
    PipelineBackend[
        enum.Enum, RustFormalResult, Any, RustArtifact, FeatureUnit, Any, BaseApplication,
        ProjectFacts,
    ]
):
    """A :class:`PipelineBackend` backed by a Rust wheel. Ecosystem-agnostic: it locates the main
    and marshals units through the resolved ``ecosystem`` + the ``FeatureUnit`` protocol, so its
    unit / main / app axes stay open (``Any``) where a single-ecosystem backend would pin them.

    Subclass (or replace via ``backend_cls``) when the app needs non-generic prep — e.g.
    Crucible's shared fixture + harness crate."""

    module: RustAppModule
    descriptor: AppDescriptor
    #: The phase enum synthesized from the descriptor — the *same* class object the frontend's
    #: ``phase_labels`` are keyed by, since the lookup is by member identity. Public because the
    #: prepared system and the formalizer need to tag their own tasks with a declared phase; reach
    #: it through :meth:`task_info` rather than indexing it.
    phase: type[enum.Enum]
    #: The four core slots of :attr:`phase`, as the driver's own mapping.
    core_phases: CorePhases
    store: ArtifactStore[Any, RustFormalResult]
    ecosystem: Ecosystem[Any, Any, Any]
    # Wall-clock ceiling for a single compile/validate (a first build can be minutes).
    command_timeout_s: int = DEFAULT_TIMEOUT_S
    # How to confine every toolchain run (docs/command-sandbox.md). None → unsandboxed.
    sandbox: SandboxConfig | None = None
    # Parsed values of the descriptor's declared CLI args, put on every component's
    # ``AuthorInput.args`` (e.g. Crucible's ``fuzz_timeout``). Set by the entry point.
    declared_args: dict[str, Any] = field(default_factory=dict)

    # Both are the wheel's to state, so they are derived from its descriptor rather than passed.
    backend_guidance: str = field(init=False)
    analysis_spec: SystemAnalysisSpec = field(init=False)

    def __post_init__(self) -> None:
        self.backend_guidance = self.descriptor.backend_guidance
        self.analysis_spec = SystemAnalysisSpec(self.descriptor.analysis_key, "rust-properties")

    @property
    @override
    def artifact_store(self) -> ArtifactStore[Any, RustFormalResult]:
        return self.store

    def task_info(self, phase: PhaseSpec) -> TaskInfo[enum.Enum]:
        """The task a step-declaring phase runs as: an id from its role, the wheel's label, and the
        phase *member* itself.

        The one way to turn a declared phase into a member. The enum is synthesized per application,
        so a caller can't name a member statically — and resolving it here keeps the member the
        driver emits identical to the one the frontend's labels are keyed by."""
        return TaskInfo(
            f"{self.descriptor.name}-{phase.role.value}", phase.label, self.phase[phase.key]
        )

    @override
    async def preflight(self, run: PipelineRun) -> ProjectFacts:
        """Prepare the wheel's workspace and gate it — everything buildable before the program has
        been analyzed, run concurrently with system analysis (``docs/rust-applications.md`` §4.2).

        Two steps, both *declared* by the wheel and executed here (``docs/rust-applications.md`` §7):

        1. :func:`run_workspace_prep` — place the wheel's build files, and (through the chain's
           :class:`~composer.rustapp.toolchain.ProjectToolchain`) carry out whatever preparing the
           analyzed project takes. Already the run's slowest non-LLM step.
        2. :func:`run_preflight_gate`, when the descriptor declares a ``preflight`` — build a
           skeleton artifact *the wheel authors itself* through the real toolchain, in the real
           sandbox. This is what turns step 1 from "we placed some build files" into "this workspace
           compiles": warming a dependency cache resolves a graph but compiles nothing, and its
           failures are deliberately non-fatal. Without the gate the first *check* of the workspace
           is the first authored draft's build — after the whole extraction phase, and reported as
           compiler errors an authoring agent cannot fix because it does not own the build files.

        Neither step reads the analyzed model or any property, which is what makes the overlap safe.
        A failure raises (:class:`PreflightFailed` from the gate, or the workspace toolchain's own
        error), and the driver cancels the analysis racing it."""
        descriptor = self.descriptor
        gate = descriptor.step(PhaseRole.PREFLIGHT)
        workdir = Path(run.source.project_root)
        # Resolved once per run and carried on every AuthorInput from here on: the wheel renders its
        # build files from this, so prep, every gated build, and the deliverable agree on what they
        # are building against.
        unit = source_unit_of(self.ecosystem, run.source)
        # Declared args are in scope from the start: prep may need one (Crucible reads
        # ``program_idl`` when deciding how to source the program's types).
        prep_input = PreflightInput(
            program=str(run.source.contract_name),
            source_unit=unit, args=dict(self.declared_args),
        )

        async def prep() -> ProjectFacts:
            prep_facts = await run_workspace_prep(
                self.module, prep_input, chain=self.ecosystem.name, source=run.source,
                sandbox=self.sandbox, command_timeout_s=self.command_timeout_s,
            )
            result = ProjectFacts(source_unit=unit, prep_facts=prep_facts)
            if gate is not None:
                # The gate renders the same workspace the prep just set up — including whatever it
                # established — so it must see the reported facts.
                await run_preflight_gate(
                    self.module,
                    prep_input.with_prep_facts(result.prep_facts),
                    workdir=workdir,
                    sandbox_dict=await self.sandbox_spec(workdir),
                    emit=make_emitter(),
                )
            return result

        if gate is None:
            # Nothing to show a task for: the prep is silent (it always was) and there is no gate.
            return await prep()
        # Unmetered: this is a build, not an agent — it must not spend one of the run's
        # ``--max-concurrent`` agent slots for the whole of system analysis.
        return await run.unmetered_runner(self.task_info(gate), prep)

    async def sandbox_spec(self, workdir: Path) -> BackendSpec:
        """The confinement prefix the wheel's blocking callouts prepend, or the trusted empty one."""
        if self.sandbox is not None and self.sandbox.enabled:
            return await self.sandbox.backend_spec(workdir, timeout_s=self.command_timeout_s)
        return {"argv_prefix": [], "timeout_s": self.command_timeout_s}

    @override
    async def prepare_system(
        self, analyzed: BaseApplication, run: PipelineRun, preflight: ProjectFacts
    ) -> PreparedSystem[RustFormalResult, FeatureUnit, Any]:
        return RustPreparedSystem(
            self.ecosystem.locate_main(analyzed, run.source), self, preflight, analyzed
        )

    @override
    def to_artifact_id(self, c: FeatureUnit) -> RustArtifact:
        return RustArtifact(
            c.slug,
            self.descriptor.artifact_layout.artifact_prefix,
            self.descriptor.artifact_layout.artifact_extension,
        )
