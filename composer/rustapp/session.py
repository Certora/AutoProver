"""The Rust backend's authoring session — the shared workflow of :mod:`composer.authoring`, with a
wheel's callouts as its gate and its prompts.

Two session kinds, differing only in what gates them and what they publish:

* a **component** session authors one component's spec, gated by ``validate_spec`` (the wheel's
  ``validate``, run per target), and publishes a property→checks mapping plus the verdicts the
  gating run produced;
* a **setup** session authors the one shared spec every component builds on, gated by
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
    TemplatedTool, ToolFamilyParams, WithAsyncDependencies, WithAsyncImplementation,
    WithInjectedId, WithInjectedState, tool_family,
)

from composer.authoring.buffer import (
    apply_spec_update, edit_spec_tool, get_spec_tool,
)
from composer.authoring.judge import (
    FeedbackThunk, JudgeBuilder, JudgeState, PropertyFeedbackProtocol, RebuttalBase,
    build_feedback_judge,
)
from composer.authoring.state import (
    AuthoringExtra, MappingVocab, SkippedProperty, check_completion, make_validation_stamper,
    merge_expected_failures, validate_check_mapping,
)
from composer.authoring.tools import give_up_tool, skip_tools
from composer.pipeline.core import GaveUp, PipelineRun
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.result import RustFormalResult, RustSetupSpec
from composer.rustapp.wire import (
    AuthorInput, CompileOk, Prompt, RustAppModule, Target, Check, ValidateBuildFailed,
    parse_compile, parse_judge, parse_prompt, parse_validate,
)
from composer.rustapp.wire import Verdict as WireVerdict
from composer.sandbox.config import BackendSpec
from composer.spec.context import CacheKey, WorkflowContext
from composer.spec.types import CheckName, PropertyTitle
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import run_to_completion
from composer.spec.service_host import ServiceHost
from composer.templates.loader import load_jinja_template
from composer.spec.source.report.schema import Outcome
from composer.ui.tool_display import (
    ToolDisplay, suppress_ack, tool_display, tool_display_of, tool_family_display,
)
from typing_extensions import TypedDict

_log = logging.getLogger(__name__)

VALIDATE_KEY = "validate"
COMPILE_KEY = "compile"
FEEDBACK_KEY = "feedback"

_SKIP_DESCRIPTION = """
    Declare that you are skipping a property from the batch.

    You must provide the property's title and a justification. Skipping
    excludes the property from the publish-time mapping check; only use
    after a genuine attempt to formalize.
    """

_SKIP_REASON = "Justification for why this property cannot be formalized"


@dataclass(frozen=True)
class CheckVocab:
    """What this wheel calls one check when it talks to the model.

    Declared by the wheel (``AppDescriptor.check_noun``) because an author writes better when the
    prompt speaks its own domain's language — Crucible's are harness functions, another backend's
    are invariants. Only prose moves: the tools keep their ``check``-worded *names* so the protocol
    can name them literally, and the wire keeps calling a check a check. Schema text is instantiated
    via :class:`CheckNouns`; this object supplies the values and the leftover runtime strings."""

    one: str
    many: str

    @classmethod
    def of(cls, descriptor: AppDescriptor) -> "CheckVocab":
        return cls(one=descriptor.check_label(), many=descriptor.check_label(plural=True))

    def fill(self, text: str) -> str:
        """``text`` with ``{check}`` / ``{checks}`` rendered. For *runtime* strings (tool results,
        the initial prompt). LLM-facing schemas go through :func:`tool_family` instead."""
        return text.format(check=self.one, checks=self.many)


class CheckNouns(ToolFamilyParams):
    """The nouns :func:`tool_family` substitutes into the session tools' schemas."""

    check: str
    checks: str


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
    """Maps one property from the batch to the {checks} that carry it.

    Many-to-many: a property may need several {checks}, and one {check} may carry several
    properties (a single rule discharging three related invariants)."""
    property_title: PropertyTitle = Field(
        description="The unique snake_case title of the property (from the batch listing) that "
        "these {checks} verify"
    )
    checks: list[CheckName] = Field(
        description="The names of the {checks} in your spec that verify this property"
    )


class RustSpecExtra(AuthoringExtra):
    #: What the author says verifies what, declared with ``map_checks`` and revisable. The distinct
    #: check names in it are the set a gate run executes, and the whole of it is what the publish
    #: gate validates. Empty for a setup session, which formalizes no properties of its own.
    property_checks: list[PropertyCheckMapping]
    expected_failures: Annotated[dict[CheckName, str], merge_expected_failures]
    #: The verdicts the last full gating run produced, check name → the wheel's verdict. Recorded
    #: verbatim: attribution is the wheel's, and the host does no verdict logic of its own.
    verdicts: dict[CheckName, WireVerdict]
    #: The targets that run covered, each carrying the checks it covered — the ground truth the
    #: publish gate holds the mapping to, and what the result reports as this component's coverage.
    ran: list[Target]
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


def properties_of(mapping: Sequence[PropertyCheckMapping], check: CheckName) -> list[PropertyTitle]:
    """The property titles the mapping says ``check`` verifies — several when one check discharges
    several properties, none while the author has not said."""
    return [m.property_title for m in mapping if check in m.checks]


def declared_names(mapping: Sequence[PropertyCheckMapping]) -> list[CheckName]:
    """The check names the author's mapping references, in first-seen order and without repeats.

    This is the set that runs. A name may be claimed by several properties (one rule discharging
    three invariants), so this is not the mapping flattened — it is its distinct names."""
    return list(dict.fromkeys(CheckName(n.strip()) for m in mapping for n in m.checks if n.strip()))


def declared_checks(
    module: RustAppModule, input_json: str, mapping: Sequence[PropertyCheckMapping]
) -> list[Check]:
    """:func:`declared_names` as :class:`Check`\\ s, each grouped by the wheel's ``target_for``.

    The parts come from the two parties that can know them: the *name* and what it verifies from the
    author (the artifact is the author's, and so is the claim), the *grouping* from the wheel (which
    invocation of the checker covers it, a backend convention)."""
    return [
        Check(
            name=name,
            properties=properties_of(mapping, name),
            target=module.target_for(input_json, name),
        )
        for name in declared_names(mapping)
    ]


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


@tool_family_display("Validating spec", "Validation result")
@tool_family(CheckNouns)
class ValidateSpec(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncDependencies[Command | str, GateDeps],
):
    """
    Build your current spec and run the verifier over the {checks} you declared.

    What runs is exactly what `map_checks` declared, so declare before validating. Each {check}
    runs under its validation target; several {checks} may share one, and each distinct target runs
    once. The report records the verdicts verbatim — including a {check} the verifier could not find
    or never exercised, which does NOT pass.

    The publish gate is stamped only by a run that covers EVERY declared {check} and comes back
    clean — where a {check} you marked with `expect_check_failure` counts as clean. Naming
    `checks` runs only those, which is for iterating on one problem; it never stamps. Any edit
    after a stamping run invalidates the stamp.
    """
    checks: list[CheckName] | None = Field(
        default=None,
        description="Run only the targets covering these {check} names. Omit to run everything, "
        "which is what the publish gate requires.",
    )

    @override
    async def run(self) -> Command | str:
        spec = self.state["curr_spec"]
        if spec is None:
            return "No spec written yet — use `put_spec` first."
        mapping = self.state["property_checks"]
        with self.tool_deps() as deps:
            vocab = deps.vocab
            wanted = declared_checks(deps.module, deps.input_json, mapping)
            if not wanted:
                return (
                    f"No {vocab.many} declared yet, so there is nothing to run. Use `map_checks` "
                    f"to say which {vocab.many} in your spec verify which property — that "
                    f"declaration is what gets validated."
                )
            if self.checks is not None:
                asked = set(self.checks)
                unknown = asked - {c.name for c in wanted}
                if unknown:
                    return (
                        f"Unknown {vocab.one} name(s): {', '.join(sorted(unknown))}. The declared "
                        f"{vocab.many} are: {', '.join(c.name for c in wanted)}."
                    )
                wanted = [c for c in wanted if c.name in asked]
            covered = targets_of(wanted)
            verdicts: dict[CheckName, WireVerdict] = {}
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
                for check, verdict in res.resolve(target):
                    verdicts[check.name] = verdict
                    _emit_verdict(deps, check, verdict)

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
        # ``ran`` travels with the stamp: the publish gate validates the mapping against the
        # {checks} THIS run covered, and any later edit invalidates the stamp, so a stale set can
        # never be the one publish is held to.
        return tool_state_update(
            self.tool_call_id,
            f"{report}\n\nEvery declared {vocab.one} is accounted for; the publish gate is "
            f"satisfied.",
            verdicts=verdicts,
            ran=covered,
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


def _emit_verdict(deps: GateDeps, check: Check, verdict: WireVerdict) -> None:
    # The property's own words read better than a check name — the check carries the author's claim,
    # so this needs nothing else. Several titles when one check discharges several properties.
    name = ", ".join(check.properties) or check.name
    line = f"{name}: {verdict.outcome.value}"
    deps.emit(
        "verdict",
        {
            "outcome": verdict.outcome.value,
            "name": name,
            "line": f"{line} — {verdict.detail}" if verdict.detail else line,
        },
    )


def _unexplained(
    verdicts: dict[CheckName, WireVerdict], expected_failures: dict[CheckName, str]
) -> set[CheckName]:
    """The checks that did not pass and were not marked as expected to fail — what stands between
    the run and the publish gate."""
    return {
        name for name, v in verdicts.items()
        if v.outcome is not Outcome.GOOD and name not in expected_failures
    }


def _verdict_report(
    verdicts: dict[CheckName, WireVerdict], expected_failures: dict[CheckName, str]
) -> str:
    lines = []
    for name, v in verdicts.items():
        mark = " (expected to fail)" if name in expected_failures else ""
        detail = f" — {v.detail}" if v.detail else ""
        lines.append(f"  {name}: {v.outcome.value}{mark}{detail}")
    return "Validation results:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Expected-failure marking
# ---------------------------------------------------------------------------

def _expect_fail_label(p: dict, *, check: str, checks: str) -> str:
    return f"Expecting {check} `{p['check_name']}` to fail"


@tool_family_display(_expect_fail_label, None)
@tool_family(CheckNouns)
class ExpectCheckFailure(WithAsyncImplementation[Command | str], WithInjectedId):
    """
    Mark a {check} as expected to fail.

    Use this only when the failure IS the finding — the checker found a real counterexample, or the
    property does not hold of the program under test. A marked {check} no longer blocks the publish
    gate, and your reason is recorded with the result. Do not use it to get past your own bug.
    """
    check_name: CheckName = Field(description="The name of the {check} expected to fail")
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


def _expect_pass_label(p: dict, *, check: str, checks: str) -> str:
    return f"Expecting {check} `{p['check_name']}` to pass"


@tool_family_display(_expect_pass_label, None)
@tool_family(CheckNouns)
class ExpectCheckPassage(WithAsyncImplementation[Command], WithInjectedId):
    """
    Unmark a {check} previously marked expected-to-fail. Every {check} is expected to pass by
    default, so this only reverts a prior `expect_check_failure`.
    """
    check_name: CheckName = Field(description="The name of the {check} now expected to pass after all")

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

class RustJudge:
    """Phantom marker for the judge's child context."""


def build_judge[K: (RustFormalResult, RustSetupSpec)](
    module: RustAppModule,
    input_json: str,
    *,
    author_ctx: WorkflowContext[K],
    env: ServiceHost,
    backend_name: str,
) -> FeedbackThunk[RebuttalBase] | None:
    """The wheel's judge as the shared feedback thunk, or ``None`` when it declares none for this
    input — a wheel may review components and not the shared setup spec.

    Asked without a draft because it is asked before there is one: whether this input is reviewed
    and who reviews it are both fixed for the session. What to ask about a particular draft is
    ``judge_instruction``, below, once per round."""
    declared = module.judge(input_json)
    if declared is None:
        return None
    system = (
        parse_judge(declared).system or "You are reviewing a formal specification."
    ) + _JUDGE_PROTOCOL

    def apply_system(builder: JudgeBuilder) -> JudgeBuilder:
        return builder.with_sys_prompt(system)

    def apply_prompt(
        builder: JudgeBuilder, spec: str,
        skipped: Sequence[SkippedProperty], rebuttals: Sequence[RebuttalBase],
    ) -> JudgeBuilder:
        return builder.with_initial_prompt(module.judge_instruction(input_json, spec))

    def input_parts(
        spec: str, skipped: Sequence[SkippedProperty], rebuttals: Sequence[RebuttalBase],
    ) -> list[str | dict]:
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
        # The judge reviews under its own child context so its memory namespace is disjoint from
        # the author's — a reviewer that reads the author's notes is not independent.
        ctx=author_ctx.child(CacheKey[K, RustJudge]("judge")),
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
    thunk: FeedbackThunk[RebuttalBase]


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
    rebuttals: list[RebuttalBase] = Field(
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
    titles: list[PropertyTitle]


@tool_family(CheckNouns)
class MapChecks(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncImplementation[Command | str],
):
    """
    Declare which {checks} in your spec verify which property.

    This declaration is what gets run: the distinct {check} names in it are exactly what
    `validate_spec` executes, and it is what the publish gate is validated against. Declare before
    validating, and call this again to revise — each call replaces the whole declaration.

    A property may need several {checks}, and one {check} may carry several properties: name it
    under each. Do not name a property you skipped.
    """
    property_checks: list[PropertyCheckMapping] = Field(
        description="For every property you did NOT skip, the {checks} in your spec that verify it."
    )

    @override
    async def run(self) -> Command | str:
        names = declared_names(self.property_checks)
        if not names:
            return "That declares no checks at all. Name at least one per property you did not skip."
        return tool_state_update(
            self.tool_call_id,
            f"Recorded: {len(self.property_checks)} propert"
            f"{'y' if len(self.property_checks) == 1 else 'ies'} mapped onto "
            f"{', '.join(names)}. Validating now runs exactly these.",
            property_checks=self.property_checks,
        )


@tool_display("Publishing spec", None)
class PublishSpec(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncDependencies[Command | str, PublishDeps],
):
    """
    Publish your spec. Refused unless every required gate has accepted the buffer as it now stands,
    and the mapping you declared accounts for every property that was not skipped.
    """
    commentary: str = Field(description="Human-readable commentary on the spec you authored")

    @override
    async def run(self) -> Command | str:
        if (err := check_completion(self.state)) is not None:
            return err
        mapping = self.state["property_checks"]
        with self.tool_deps() as deps:
            # Ground truth is what the STAMPING run covered, not what is declared now: a name added
            # since is one that did not run, and one removed is one that ran unclaimed. Both are
            # errors here, which is why a mapping edit needs no stamp of its own.
            err = validate_check_mapping(
                [(m.property_title, m.checks) for m in mapping],
                self.state["skipped"],
                deps.titles,
                _MAPPING,
                ran=[c.name for t in self.state["ran"] for c in t.checks],
            )
        if err is not None:
            return err
        return tool_state_update(
            self.tool_call_id, "Accepted",
            result=self.commentary,
            failed=False,
        )


@tool_display("Publishing spec", None)
class PublishSetup(
    WithInjectedId,
    WithInjectedState[RustSessionState],
    WithAsyncImplementation[Command | str],
):
    """
    Publish the shared spec. Refused unless every required gate has accepted the buffer as it
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
    property_checks: list[tuple[PropertyTitle, list[CheckName]]]
    verdicts: dict[CheckName, WireVerdict]
    #: What the stamping gate run covered — the targets, each with its checks.
    ran: list[Target]
    expected_failures: dict[CheckName, str]


async def run_session[K: (RustFormalResult, RustSetupSpec)](
    *,
    module: RustAppModule,
    input: AuthorInput,
    kind: Literal["component", "setup"],
    titles: list[PropertyTitle],
    env: ServiceHost,
    ctx: WorkflowContext[K],
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
        vocab=vocab,
    )
    component = kind == "component"
    judge = build_judge(module, input_json, author_ctx=ctx, env=env, backend_name=backend_name)

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
            _map_tool(vocab),
            _validate_tool(gate_deps, vocab),
            *skip_tools(
                lambda: titles,
                skip_description=_SKIP_DESCRIPTION,
                skip_reason=_SKIP_REASON,
            ),
            *_expect_tools(vocab),
            _publish_tool(PublishDeps(titles=titles)),
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

    graph = (
        env.builder_heavy()
        .with_state(RustSessionState)
        .with_input(RustSessionInput)
        .with_output_key("result")
        .with_tools(tools)
        .with_sys_prompt(system)
        .with_initial_prompt(_initial_prompt(prompt, component, vocab))
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
            ran=[],
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
        ran=state["ran"],
        expected_failures=state["expected_failures"],
    )


def _validate_tool(deps: GateDeps, vocab: CheckVocab) -> BaseTool:
    return (
        ValidateSpec.with_template(check=vocab.one, checks=vocab.many)
        .bind(deps)
        .as_tool("validate_spec")
    )


def _expect_tools(vocab: CheckVocab) -> list[BaseTool]:
    return [
        ExpectCheckFailure.with_template(check=vocab.one, checks=vocab.many)
        .as_tool("expect_check_failure"),
        ExpectCheckPassage.with_template(check=vocab.one, checks=vocab.many)
        .as_tool("expect_check_passage"),
    ]


def _map_tool(vocab: CheckVocab) -> BaseTool:
    # Formatting is not transitive: MapChecks.with_template rewrites this class's own text, but
    # the nested PropertyCheckMapping would still show ``{checks}``. Template the element first,
    # then splice it in. Display is applied after the splice so ``as_tool`` closes over the
    # spliced schema, not the unspliced templated base.
    mapping = TemplatedTool(PropertyCheckMapping).with_template(
        check=vocab.one, checks=vocab.many,
    )
    templated = MapChecks.with_template(check=vocab.one, checks=vocab.many)
    schema = create_model(
        "MapChecks",
        __doc__=templated.__doc__,
        __base__=templated,
        property_checks=(list[mapping], Field(  # type: ignore[valid-type]
            description=templated.model_fields["property_checks"].description,
        )),
    )
    tool_display_of(ToolDisplay(f"Declaring {vocab.many}", None))(schema)
    return schema.as_tool("map_checks")


def _publish_tool(deps: PublishDeps) -> BaseTool:
    return PublishSpec.bind(deps).as_tool("result")


def _feedback_tool(judge: FeedbackThunk[RebuttalBase], rebuttal: type[RebuttalBase]) -> BaseTool:
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


def _initial_prompt(prompt: Prompt, component: bool, vocab: CheckVocab) -> str:
    """The wheel's instruction, plus the obligation the gate will hold the author to.

    The host states it rather than trusting each wheel's free-form instruction to: the names are the
    author's to choose, but *that* every property is covered and every declared name is really in
    the spec is enforced identically for every backend, so it is worded once here."""
    if not component:
        return prompt.instruction
    return (
        f"{prompt.instruction}\n\nDeclare with `map_checks` which {vocab.many} verify which "
        f"property before you validate: that declaration is what gets run. Every property must be "
        f"verified by at least one {vocab.one} or skipped with a reason, every {vocab.one} you "
        f"declare must really be in your spec, and one {vocab.one} may carry several properties."
    )
