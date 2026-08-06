"""The Rust backend's authoring session — the shared workflow of :mod:`composer.authoring`, with a
wheel's callouts as its gate and its prompts.

Two session kinds, differing only in what gates them and what they publish:

* a **component** session authors one component's spec, gated by ``validate_spec`` (the wheel's
  ``validate``, run per target), and publishes a property→checks mapping plus the verdicts the
  gating run produced;
* a **setup** session authors the one shared artifact every component builds on, gated by
  ``compile_spec`` (the wheel's ``compile``). It formalizes no properties of its own, so it declares
  no mapping and has nothing to skip.

The wheel is still a passive service: it supplies prompts and answers the two blocking callouts.
What changed from a Python-driven retry loop is who holds the state — the agent does, in a buffer it
edits, and the gate is a tool it calls rather than a loop wrapped around it.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, NotRequired, Sequence, override

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from langgraph.types import Command
from pydantic import BaseModel, Field, create_model

from graphcore.graph import FlowInput, tool_state_update
from graphcore.tools.schemas import (
    WithAsyncDependencies, WithAsyncImplementation, WithInjectedId, WithInjectedState,
)

from composer.authoring.buffer import (
    apply_spec_update, edit_spec_tool, get_spec_tool,
)
from composer.authoring.judge import (
    FeedbackThunk, JudgeState, PropertyFeedbackProtocol, RebuttalBase, build_feedback_judge,
)
from composer.authoring.state import (
    AuthoringExtra, MappingVocab, SkippedProperty, check_completion, make_validation_stamper,
    merge_expected_failures, validate_check_mapping,
)
from composer.authoring.tools import RecordSkip, Unskip, give_up_tool
from composer.pipeline.core import GaveUp, PipelineRun
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.wire import (
    AuthorInput, CompileOk, Prompt, RustAppModule, Target, Check, ValidateBuildFailed,
    parse_compile, parse_prompt, parse_validate,
)
from composer.rustapp.wire import Verdict as WireVerdict
from composer.sandbox.config import BackendSpec
from composer.spec.context import WorkflowContext
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import run_to_completion
from composer.spec.service_host import ServiceHost
from composer.templates.loader import load_jinja_template
from composer.spec.source.report.schema import Outcome
from composer.ui.tool_display import ToolDisplay, suppress_ack, tool_display, tool_display_of
from typing_extensions import TypedDict

_log = logging.getLogger(__name__)

VALIDATE_KEY = "validate"
COMPILE_KEY = "compile"
FEEDBACK_KEY = "feedback"


@dataclass(frozen=True)
class CheckVocab:
    """What this wheel calls one check when it talks to the model.

    Declared by the wheel (``AppDescriptor.check_noun``) because an author writes better when the
    prompt speaks its own domain's language — Crucible's are harness functions, another backend's
    are invariants. Only prose moves: the tools keep their ``check``-worded *names* so the protocol
    can name them literally, and the wire keeps calling a check a check."""

    one: str
    many: str

    @classmethod
    def of(cls, descriptor: AppDescriptor) -> "CheckVocab":
        return cls(one=descriptor.check_label(), many=descriptor.check_label(plural=True))

    def fill(self, text: str) -> str:
        """``text`` with ``{check}`` / ``{checks}`` rendered. Only ever applied to strings declared
        in this module, so a stray brace is a bug here rather than untrusted input."""
        return text.format(check=self.one, checks=self.many)


class ProtocolParams(TypedDict):
    """Render variables for the host-owned half of an author's system prompt."""
    gate_tool: str
    has_judge: bool
    has_checks: bool
    check_noun: str


ProtocolTemplate = TypedTemplate[ProtocolParams]("authoring_protocol.j2")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class PropertyCheckMapping(BaseModel):
    """Maps one property from the batch to the {checks} that carry it."""
    property_title: str = Field(
        description="The unique snake_case title of the property (from the batch listing) that "
        "these {checks} verify"
    )
    checks: list[str] = Field(
        description="The names of the {checks} in your spec that verify this property"
    )


class RustSpecExtra(AuthoringExtra):
    property_checks: list[PropertyCheckMapping]
    expected_failures: Annotated[dict[str, str], merge_expected_failures]
    #: The verdicts the last full gating run produced, check name → the wheel's verdict. Recorded
    #: verbatim: attribution is the wheel's, and the host does no verdict logic of its own.
    verdicts: dict[str, WireVerdict]
    failed: bool | None


class RustSessionInput(RustSpecExtra, FlowInput):
    pass


class RustSessionState(RustSpecExtra, MessagesState):
    result: NotRequired[str]


_MAPPING = MappingVocab(
    check_noun="check",
    field_name="property_checks",
    ran_source="the stamping validate_spec run",
)


def live_checks(checks: Sequence[Check], skipped: Sequence[SkippedProperty]) -> list[Check]:
    """The checks still expected to be checked — every declared one whose property was not skipped.

    The wheel declares its checks before authoring starts and they do not depend on what the model
    wrote; skipping is the only thing that removes one, which is why a skip has to carry a reason."""
    dropped = {s.property_title for s in skipped}
    return [c for c in checks if c.property not in dropped]


# ---------------------------------------------------------------------------
# The gate tools
# ---------------------------------------------------------------------------

@dataclass
class GateDeps:
    """What both gate tools need to reach the wheel's blocking callouts."""

    module: RustAppModule
    input_json: str
    workdir: Path
    sandbox_json: str
    emit: Callable[[str, dict], None]
    #: What to call a check when talking to the author.
    vocab: CheckVocab = CheckVocab("check", "checks")
    #: Serializes the blocking callouts when the wheel shares one build dir across components.
    command_sem: asyncio.Semaphore | None = None
    #: The checks this session must produce, from the wheel's ``checks``. Empty for a setup
    #: session, which formalizes none.
    checks: list[Check] = field(default_factory=list)


async def _blocking(thunk: Callable[[], str], sem: asyncio.Semaphore | None) -> str:
    """Run a wheel callout that spawns ``run-confined`` off the event loop, serialized by ``sem``
    when the wheel shares one workdir across concurrent components."""
    if sem is None:
        return await asyncio.to_thread(thunk)
    async with sem:
        return await asyncio.to_thread(thunk)


def _first_line(s: str) -> str:
    return next((ln for ln in s.splitlines() if ln.strip()), "").strip()


@tool_display("Building spec", "Build result")
class CompileSpec(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncDependencies[Command | str, GateDeps],
):
    """
    Build your current spec with the real toolchain.

    A clean build stamps the publish gate for the buffer exactly as it now stands; any later
    `put_spec` or `edit_spec` invalidates that stamp and you must build again. A failed build
    returns the compiler's own diagnostics.
    """

    @override
    async def run(self) -> Command | str:
        spec = self.state["curr_spec"]
        if spec is None:
            return "No spec written yet — use `put_spec` first."
        with self.tool_deps() as deps:
            result = parse_compile(
                await _blocking(
                    lambda: deps.module.compile(
                        deps.input_json, spec, str(deps.workdir), deps.sandbox_json
                    ),
                    deps.command_sem,
                )
            )
            if not isinstance(result, CompileOk):
                deps.emit("build_output", {"line": _first_line(result.errors) or "build failed"})
                return f"The build FAILED.\n\n{result.errors}"
            return tool_state_update(
                self.tool_call_id,
                "The build succeeded.",
                validations=make_validation_stamper(COMPILE_KEY)(self.state),
            )


class ValidateSpec(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncDependencies[Command | str, GateDeps],
):
    """
    Build your current spec and run the verifier over it.

    Each {check} runs under its validation target; several {checks} may share one, and each
    distinct target runs once. The report records the verdicts verbatim.

    The publish gate is stamped only by a run that covers EVERY live {check} and comes back
    clean — where a {check} you marked with `expect_check_failure` counts as clean. Naming
    `checks` runs only those, which is for iterating on one problem; it never stamps. Any edit
    after a stamping run invalidates the stamp.
    """
    checks: list[str] | None = Field(
        default=None,
        description="Run only the targets covering these {check} names. Omit to run everything, "
        "which is what the publish gate requires.",
    )

    @override
    async def run(self) -> Command | str:
        spec = self.state["curr_spec"]
        if spec is None:
            return "No spec written yet — use `put_spec` first."
        with self.tool_deps() as deps:
            vocab = deps.vocab
            wanted = live_checks(deps.checks, self.state["skipped"])
            if self.checks is not None:
                asked = set(self.checks)
                unknown = asked - {c.name for c in wanted}
                if unknown:
                    return (
                        f"Unknown {deps.vocab.one} name(s): {', '.join(sorted(unknown))}. The "
                        f"{deps.vocab.many} of this spec are: "
                        f"{', '.join(c.name for c in wanted)}."
                    )
                wanted = [c for c in wanted if c.name in asked]
            if not wanted:
                return (
                    "Nothing to validate: every property is currently skipped. Un-skip one, or "
                    "`give_up` if none of them can be formalized."
                )
            covered = targets_of(wanted)
            verdicts: dict[str, WireVerdict] = {}
            for target in covered:
                res = parse_validate(
                    await _blocking(
                        lambda t=target: deps.module.validate(
                            deps.input_json, spec, t.model_dump_json(),
                            str(deps.workdir), deps.sandbox_json,
                        ),
                        deps.command_sem,
                    )
                )
                if isinstance(res, ValidateBuildFailed):
                    deps.emit(
                        "build_output",
                        {"line": _first_line(res.errors) or "build failed"},
                    )
                    return f"The build FAILED, so nothing was checked.\n\n{res.errors}"
                for name, verdict in res.verdicts:
                    verdicts[name] = verdict
                    _emit_verdict(deps, wanted, name, verdict)

        report = _verdict_report(verdicts, self.state["expected_failures"])
        partial = self.checks is not None
        unexplained = _unexplained(verdicts, self.state["expected_failures"])
        if partial:
            return f"{report}\n\nThis was a partial run, so it does not satisfy the publish gate."
        if unexplained:
            return (
                f"{report}\n\nThe publish gate is NOT satisfied: {', '.join(sorted(unexplained))} "
                f"did not pass. Fix the spec, or — if the failure is the finding — mark the "
                f"{vocab.one} with `expect_check_failure` and a reason."
            )
        return tool_state_update(
            self.tool_call_id,
            f"{report}\n\nEvery live {vocab.one} is accounted for; the publish gate is satisfied.",
            verdicts=verdicts,
            validations=make_validation_stamper(VALIDATE_KEY)(self.state),
        )


def targets_of(checks: Sequence[Check]) -> list[Target]:
    """``checks`` partitioned into the checker invocations that cover them — one :class:`Target` per
    distinct target name, in first-seen order, each carrying its own checks.

    This is the whole of the run-vs-report split: several checks sharing a target means one build
    and one run for all of them, while each still gets its own verdict. The host owns the grouping —
    it decides what runs and in what order — so it hands the answer to the wheel rather than leaving
    it to re-derive one."""
    names = list(dict.fromkeys(c.target_or_name() for c in checks))
    return [
        Target(name=name, checks=[c for c in checks if c.target_or_name() == name])
        for name in names
    ]


def _emit_verdict(
    deps: GateDeps, checks: Sequence[Check], name: str, verdict: WireVerdict
) -> None:
    prop = next((c.property for c in checks if c.name == name), name)
    line = f"{prop}: {verdict.outcome.value}"
    deps.emit(
        "verdict",
        {
            "outcome": verdict.outcome.value,
            "name": prop,
            "line": f"{line} — {verdict.detail}" if verdict.detail else line,
        },
    )


def _unexplained(
    verdicts: dict[str, WireVerdict], expected_failures: dict[str, str]
) -> set[str]:
    """The checks that did not pass and were not marked as expected to fail — what stands between
    the run and the publish gate."""
    return {
        name for name, v in verdicts.items()
        if v.outcome is not Outcome.GOOD and name not in expected_failures
    }


def _verdict_report(verdicts: dict[str, WireVerdict], expected_failures: dict[str, str]) -> str:
    lines = []
    for name, v in verdicts.items():
        mark = " (expected to fail)" if name in expected_failures else ""
        detail = f" — {v.detail}" if v.detail else ""
        lines.append(f"  {name}: {v.outcome.value}{mark}{detail}")
    return "Validation results:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Expected-failure marking
# ---------------------------------------------------------------------------

class ExpectCheckFailure(WithAsyncImplementation[Command | str], WithInjectedId):
    """
    Mark a {check} as expected to fail.

    Use this only when the failure IS the finding — the checker found a real counterexample, or the
    property does not hold of the program under test. A marked {check} no longer blocks the publish
    gate, and your reason is recorded with the result. Do not use it to get past your own bug.
    """
    check_name: str = Field(description="The name of the {check} expected to fail")
    reason: str = Field(
        description="Why this {check} is expected to fail — what the failure demonstrates"
    )

    @override
    async def run(self) -> Command | str:
        # An empty reason is the sentinel ``merge_expected_failures`` reads as an unmarking, so it
        # must not get through here.
        if not self.reason.strip():
            return "A non-empty reason is required when marking it as expected to fail."
        return tool_state_update(
            self.tool_call_id, "Recorded.", expected_failures={self.check_name: self.reason},
        )


class ExpectCheckPassage(WithAsyncImplementation[Command], WithInjectedId):
    """
    Unmark a {check} previously marked expected-to-fail. Every {check} is expected to pass by
    default, so this only reverts a prior `expect_check_failure`.
    """
    check_name: str = Field(description="The name of the {check} now expected to pass after all")

    @override
    async def run(self) -> Command:
        return tool_state_update(
            self.tool_call_id, "Recorded.", expected_failures={self.check_name: ""},
        )


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def rebuttal_model(evidence_kinds: Sequence[str]) -> type[RebuttalBase]:
    """The rebuttal type for a wheel, over the evidence it declared it can produce.

    Built per wheel rather than fixed because what counts as evidence is a property of the backend:
    a fuzzing wheel can show a counterexample, a typechecking one only what its checker said."""
    kinds = tuple(evidence_kinds) or ("reasoned",)
    return create_model(
        "Rebuttal",
        __base__=RebuttalBase,
        __doc__=RebuttalBase.__doc__,
        evidence_type=(
            Literal[kinds],  # type: ignore[valid-type]
            Field(
                description="What backs this rebuttal. Evidence from a tool outweighs an argument "
                f"with the judge. One of: {', '.join(kinds)}."
            ),
        ),
    )


#: Appended to the wheel's judge system prompt. The wheel says what to review; the host says how the
#: verdict is returned, because the host is what reads it.
_JUDGE_PROTOCOL = (
    "\n\nRead the spec back with `get_spec` before judging it — reviewing the copy in this prompt "
    "is not reviewing what was written. When you are done, call the `result` tool with `good` set "
    "to whether the spec is acceptable as it stands, and `feedback` saying what to change (empty "
    "when there is nothing to change). Your verdict is what the author's publish gate reads."
)

# The reviewer gets the spec and the program's API in its prompt and shares the run memory, so it
# does not need the exploration sub-agent — direct reads (`get_file`/`grep`) cover its spot-checks,
# and each `code_explorer` call is itself a multi-call sub-agent.
JUDGE_EXCLUDE_TOOLS = frozenset({"code_explorer"})


def _judge_prompts(module: RustAppModule, input_json: str, spec: str) -> Prompt | None:
    raw = module.judge_prompt(input_json, spec)
    return parse_prompt(raw) if raw else None


def build_judge(
    module: RustAppModule,
    input_json: str,
    *,
    ctx: WorkflowContext[Any],
    env: ServiceHost,
    backend_name: str,
) -> FeedbackThunk[Any] | None:
    """The wheel's judge as the shared feedback thunk, or ``None`` when it declares none.

    Probed with an empty spec, exactly as the wheel's ``judge_prompt`` contract allows: it returns
    ``None`` for a kind it does not review, and that answer does not depend on the draft."""
    probe = _judge_prompts(module, input_json, "")
    if probe is None:
        return None

    # The role half is asked for once — it says what this reviewer is, which cannot depend on the
    # draft. The instruction is asked for per review, because it is *about* the draft.
    system = (probe.system or "You are reviewing a formal specification.") + _JUDGE_PROTOCOL

    def apply_system(builder):
        return builder.with_sys_prompt(system)

    def apply_prompt(builder, spec: str, skipped, rebuttals):
        prompt = _judge_prompts(module, input_json, spec)
        assert prompt is not None, "the wheel declared a judge for this input"
        return builder.with_initial_prompt(prompt.instruction)

    def input_parts(spec: str, skipped, rebuttals) -> list[str | dict]:
        parts: list[str | dict] = ["The proposed spec is", spec]
        if skipped:
            parts.append("The author declined to formalize these properties:")
            for s in skipped:
                parts.append(f"  Property {s.property_title}: {s.reason}")
        if rebuttals:
            parts.append(
                "The author has filed the following rebuttals against feedback from prior "
                "rounds. Evidence produced by a tool carries near-binding weight; a reasoned "
                "rebuttal is a conversation, not a veto."
            )
            for i, r in enumerate(rebuttals, 1):
                parts.append(
                    f"  Rebuttal {i} [{getattr(r, 'evidence_type', 'reasoned')}]\n"
                    f"    Addressing: {r.prior_feedback_reference}\n"
                    f"    Evidence: {r.evidence}"
                )
        return parts

    return build_feedback_judge(
        ctx=ctx,
        env=env,
        apply_system=apply_system,
        apply_prompt=apply_prompt,
        input_parts=input_parts,
        readback=_readback(JudgeState),
        description=f"{backend_name} spec review",
        thread_prefix=f"{backend_name}-judge",
    )


@dataclass
class FeedbackDeps:
    thunk: FeedbackThunk[Any]


class FeedbackTool(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncDependencies[Command | str, FeedbackDeps],
):
    """
    Send your current spec to the reviewer.

    The reviewer evaluates whether the spec really checks the properties it claims to, and whether
    any skip is justified. An acceptance is recorded against the buffer exactly as it now stands; a
    later edit invalidates it.

    If a prior-round suggestion was tried and provably does not work, file it in `rebuttals` with
    the concrete evidence. Do not file rebuttals for feedback you merely disagree with — address
    those by revising the spec.
    """
    rebuttals: list[Any] = Field(
        default_factory=list,
        description="Rebuttals to specific prior-round feedback, each identifying the point being "
        "rebutted, classifying the evidence, and supplying it. Empty is the expected default.",
    )

    @override
    async def run(self) -> Command | str:
        spec = self.state["curr_spec"]
        if spec is None:
            return "No spec written yet — there is nothing to review."
        with self.tool_deps() as deps:
            res: PropertyFeedbackProtocol = await deps.thunk(
                spec, self.state["skipped"], self.rebuttals, self.tool_call_id,
            )
        body = f"Accepted? {res.good}\nFeedback:\n{res.feedback}"
        if not res.good:
            return body
        return tool_state_update(
            self.tool_call_id, body,
            validations=make_validation_stamper(FEEDBACK_KEY)(self.state),
        )


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

@dataclass
class PublishDeps:
    titles: list[str]
    checks: list[Check]


class PublishSpec(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncDependencies[Command | str, PublishDeps],
):
    """
    Publish your spec. Refused unless every required gate has accepted the buffer as it now stands,
    and the declared mapping accounts for every property that was not skipped.
    """
    commentary: str = Field(description="Human-readable commentary on the spec you authored")
    property_checks: list[PropertyCheckMapping] = Field(
        description="For every property you did NOT skip, the {checks} in your spec that verify it."
    )

    @override
    async def run(self) -> Command | str:
        if (err := check_completion(self.state)) is not None:
            return err
        with self.tool_deps() as deps:
            expected = live_checks(deps.checks, self.state["skipped"])
            err = validate_check_mapping(
                [(m.property_title, m.checks) for m in self.property_checks],
                self.state["skipped"],
                deps.titles,
                _MAPPING,
                ran=[c.name for c in expected],
            )
        if err is not None:
            return err
        return tool_state_update(
            self.tool_call_id, "Accepted",
            result=self.commentary,
            property_checks=self.property_checks,
            failed=False,
        )


@tool_display("Publishing spec", None)
class PublishSetup(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncImplementation[Command | str],
):
    """
    Publish the shared artifact. Refused unless every required check has accepted the buffer as it
    now stands.
    """
    commentary: str = Field(description="Human-readable commentary on the artifact you authored")

    @override
    async def run(self) -> Command | str:
        if (err := check_completion(self.state)) is not None:
            return err
        return tool_state_update(
            self.tool_call_id, "Accepted", result=self.commentary, failed=False,
        )


_GIVE_UP_DESCRIPTION = """
    Give up on authoring this spec.

    A last resort, once you have exhausted the other tools. The reason is recorded and reported —
    it is a better outcome than publishing something that only looks checked.
    """


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

_GET_SPEC_DESCRIPTION = """
    Read back the current contents of your spec buffer.
    """

_EDIT_SPEC_DESCRIPTION = """
    Make a surgical edit to the current spec instead of re-emitting the whole file.

    Provide `old_string` — an exact span copied from the current spec — and `new_string` to replace
    it with. `old_string` must occur exactly once; include enough surrounding context to make it
    unique. Any edit invalidates the stamps a gate tool or the reviewer put on the previous draft.
    """


def _readback(ty: type) -> BaseTool:
    return get_spec_tool(
        ty,
        name="get_spec",
        description=_GET_SPEC_DESCRIPTION,
        missing="No spec written yet",
        display=ToolDisplay("Reading spec", None),
        title="GetSpec",
    )


#: The wheel's put-time check, as the buffer's validator.
type SyntaxCheck = Callable[[str], str | None]


@tool_display(
    label=lambda p: f"Putting spec ({len(p.get('spec', ''))} chars)",
    result=suppress_ack("Spec write result", ("Accepted",)),
)
class PutSpec(WithAsyncDependencies[Command | str, SyntaxCheck], WithInjectedId):
    """
    Replace the entire spec buffer with the source you provide.

    Prefer `edit_spec` once a draft exists — it is dramatically cheaper than re-sending the whole
    file. Any write invalidates the stamps a gate tool or the reviewer put on the previous draft.
    """
    spec: str = Field(description="The complete source of the spec file")

    @override
    async def run(self) -> Command | str:
        with self.tool_deps() as check:
            return apply_spec_update(
                tool_call_id=self.tool_call_id, text=self.spec, validator=check,
            )


def _syntax_check(module: RustAppModule, input_json: str) -> SyntaxCheck:
    def check(spec: str) -> str | None:
        return module.check_syntax(input_json, spec)
    return check


@dataclass(frozen=True)
class SessionResult:
    """What an authoring session produced."""

    commentary: str
    spec: str
    skipped: list[SkippedProperty]
    property_checks: list[tuple[str, list[str]]]
    verdicts: dict[str, WireVerdict]
    expected_failures: dict[str, str]


async def run_session(
    *,
    module: RustAppModule,
    input: AuthorInput,
    kind: Literal["component", "setup"],
    checks: list[Check],
    titles: list[str],
    env: ServiceHost,
    ctx: WorkflowContext[Any],
    run: PipelineRun,
    workdir: Path,
    sandbox_dict: BackendSpec,
    descriptor: AppDescriptor,
    emit: Callable[[str, dict], None],
    command_sem: asyncio.Semaphore | None = None,
    description: str,
) -> SessionResult | GaveUp:
    """Run one authoring session to completion and return what it published, or :class:`GaveUp`.

    Everything the wheel gets to say about how the session speaks and what it may cite comes from
    ``descriptor``, so those declarations are read in one place rather than threaded as loose
    strings."""
    input_json = input.model_dump_json()
    backend_name = descriptor.name
    vocab = CheckVocab.of(descriptor)
    gate_deps = GateDeps(
        module=module,
        input_json=input_json,
        workdir=workdir,
        sandbox_json=json.dumps(sandbox_dict),
        emit=emit,
        command_sem=command_sem,
        checks=checks,
        vocab=vocab,
    )
    component = kind == "component"
    judge = build_judge(module, input_json, ctx=ctx, env=env, backend_name=backend_name)

    gate_tool = "validate_spec" if component else "compile_spec"
    required = [VALIDATE_KEY if component else COMPILE_KEY]
    if judge is not None:
        required.append(FEEDBACK_KEY)

    tools: list[BaseTool] = [
        *env.all_tools,
        PutSpec.bind(_syntax_check(module, input_json)).as_tool("put_spec"),
        _readback(RustSessionState),
        edit_spec_tool(
            RustSessionState,
            name="edit_spec",
            description=_EDIT_SPEC_DESCRIPTION,
            missing="No spec written yet — use `put_spec` first.",
            display=ToolDisplay("Editing spec", suppress_ack("Spec edit result")),
            title="EditSpec",
            validator=_syntax_check(module, input_json),
            reset_read=None,
        ),
        give_up_tool(
            name="give_up", description=_GIVE_UP_DESCRIPTION, label=f"{backend_name} authoring",
        ),
        ctx.get_memory_tool(),
    ]
    if component:
        tools += [
            _validate_tool(gate_deps, vocab),
            RecordSkip.bind(lambda: titles).as_tool("record_skip"),
            Unskip.bind(lambda: titles).as_tool("unskip_property"),
            *_expect_tools(vocab),
            _publish_tool(PublishDeps(titles=titles, checks=checks), vocab),
        ]
    else:
        tools += [
            CompileSpec.bind(gate_deps).as_tool("compile_spec"),
            PublishSetup.as_tool("result"),
        ]
    if judge is not None:
        rebuttal = rebuttal_model(descriptor.evidence_kinds)
        tools.append(_feedback_tool(judge, rebuttal))

    prompt = parse_prompt(module.author_prompt(input_json))
    # The host owns the protocol half of the system prompt and the wheel the domain half, so a
    # wheel never restates the tool contract — and cannot drift from it.
    protocol = ProtocolTemplate.bind({
        "gate_tool": gate_tool,
        "has_judge": judge is not None,
        "has_checks": component,
        "check_noun": vocab.one,
    }).render_to(load_jinja_template)
    system = f"{protocol}\n\n{prompt.system}" if prompt.system else protocol

    graph: Any = (
        env.builder_heavy()
        .with_state(RustSessionState)
        .with_input(RustSessionInput)
        .with_output_key("result")
        .with_tools(tools)
        .with_sys_prompt(system)
        .with_initial_prompt(_initial_prompt(prompt, checks, component, vocab))
        .compile_async()
    )

    tid, mnem = await ctx.thread_and_mnemonic()
    state = await run_to_completion(
        graph,
        RustSessionInput(
            input=[],
            curr_spec=None,
            skipped=[],
            validations={},
            required_validations=required,
            property_checks=[],
            expected_failures={},
            verdicts={},
            failed=None,
        ),
        thread_id=tid,
        recursion_limit=ctx.recursion_limit,
        description=f"{description} ({mnem})",
    )

    assert "result" in state
    assert state["failed"] is not None
    if state["failed"]:
        return GaveUp(reason=state["result"])
    spec = state["curr_spec"]
    assert spec is not None
    return SessionResult(
        commentary=state["result"],
        spec=spec,
        skipped=state["skipped"],
        property_checks=[(m.property_title, m.checks) for m in state["property_checks"]],
        verdicts=state["verdicts"],
        expected_failures=state["expected_failures"],
    )


def _redescribe[T: BaseModel](base: type[T], vocab: CheckVocab, **fields: Any) -> type[T]:
    """``base`` with its docstring — and the named fields' descriptions — spoken in the wheel's
    vocabulary. A subclass rather than a mutation: the base classes are module-level and shared by
    every session in the process.

    The bases these are built from must NOT carry ``@tool_display``: that decorator rebinds
    ``as_tool``/``bind`` closed over the class it decorated, so a subclass of a decorated class
    silently hands back the *base's* schema. The factories below apply the display instead, which
    is also what lets a label speak the wheel's noun."""
    assert base.__doc__ is not None
    return create_model(
        base.__name__,
        __base__=base,
        __doc__=vocab.fill(base.__doc__),
        **{
            name: (info.annotation, Field(
                default_factory=info.default_factory,  # type: ignore[arg-type]
            ) if info.default_factory is not None else Field(
                default=info.default,
                description=vocab.fill(info.description or ""),
            ))
            for name, info in ((n, base.model_fields[n]) for n in fields)
        },
    )


def _validate_tool(deps: GateDeps, vocab: CheckVocab) -> BaseTool:
    schema = _redescribe(ValidateSpec, vocab, checks=...)
    tool_display_of(ToolDisplay("Validating spec", "Validation result"))(schema)
    return schema.bind(deps).as_tool("validate_spec")


def _expect_tools(vocab: CheckVocab) -> list[BaseTool]:
    fail = _redescribe(ExpectCheckFailure, vocab, check_name=..., reason=...)
    passage = _redescribe(ExpectCheckPassage, vocab, check_name=...)
    tool_display_of(
        ToolDisplay(lambda p: f"Expecting {vocab.one} `{p['check_name']}` to fail", None)
    )(fail)
    tool_display_of(
        ToolDisplay(lambda p: f"Expecting {vocab.one} `{p['check_name']}` to pass", None)
    )(passage)
    return [
        fail.as_tool("expect_check_failure"),
        passage.as_tool("expect_check_passage"),
    ]


def _publish_tool(deps: PublishDeps, vocab: CheckVocab) -> BaseTool:
    # The mapping's own field descriptions are LLM-facing too, so the publish schema is rebuilt
    # around a re-described element type rather than just re-describing the list.
    mapping = _redescribe(PropertyCheckMapping, vocab, property_title=..., checks=...)
    schema = create_model(
        "PublishSpec",
        __base__=PublishSpec,
        __doc__=vocab.fill(PublishSpec.__doc__ or ""),
        property_checks=(list[mapping], Field(  # type: ignore[valid-type]
            description=vocab.fill(
                PublishSpec.model_fields["property_checks"].description or ""
            ),
        )),
    )
    tool_display_of(ToolDisplay("Publishing spec", None))(schema)
    return schema.bind(deps).as_tool("result")


def _feedback_tool(judge: FeedbackThunk[Any], rebuttal: type[RebuttalBase]) -> BaseTool:
    """``feedback_tool``, with the rebuttal type the wheel's declared evidence kinds produced."""
    schema = create_model(
        "FeedbackTool",
        __base__=FeedbackTool,
        __doc__=FeedbackTool.__doc__,
        rebuttals=(list[rebuttal], Field(  # type: ignore[valid-type]
            default_factory=list,
            description=FeedbackTool.model_fields["rebuttals"].description,
        )),
    )
    tool_display_of(ToolDisplay("Getting feedback", "Feedback"))(schema)
    return schema.bind(FeedbackDeps(thunk=judge)).as_tool("feedback_tool")


def _initial_prompt(
    prompt: Prompt, checks: Sequence[Check], component: bool, vocab: CheckVocab
) -> str:
    """The wheel's instruction, plus the exact names the publish gate will check against.

    The host renders the listing rather than trusting each wheel's free-form instruction to spell
    the titles and check names identically — they are compared literally."""
    if not component or not checks:
        return prompt.instruction
    rows = "\n".join(
        f"  - property {c.property!r} → {vocab.one} `{c.name}`" for c in checks
    )
    return (
        f"{prompt.instruction}\n\nYour spec must provide these {vocab.many}, and `result` must map "
        f"each property to the {vocab.many} that verify it, using exactly these names:\n{rows}"
    )
