"""The CVLR authoring loop: one batch of properties in, one harness module out.

``docs/cvlr-backend-plan.md`` §7.5. Structurally this is the foundry author with a different checker
and a different language, and that is deliberate — the loop is not a hand-written retry sequence but
a compiled agent graph whose *gate* is a digest stamp
(:mod:`composer.authoring.state`). "Author → compile → build → prover → analyze → revise" is what the
agent does with the tools, not what this module's control flow does; what this module guarantees is
that ``result`` is refused until the prover has accepted the draft **as it now stands** and the judge
has too. An edit after a green run silently invalidates that run, because the digest stops matching.

Four things are CVLR's own:

* **Two checker tiers** (:mod:`composer.spec.cvlr.verify`). ``cargo_check`` is free and ungated;
  ``verify_rules`` is minutes and money and is the stamp. A prover run builds first and returns the
  compiler's message without submitting when the build fails, so there is one gate for the two facts.
* **The mapping's ground truth is the draft.** Both CVLR declaration forms name their rules
  deterministically (:mod:`composer.spec.cvlr.rules`), so the publish gate gets the both-direction
  check forge gets — no property claiming a rule that does not exist, no rule left untied to a
  property — without waiting for a submission.
* **A failing rule may be the finding.** ``expect_rule_failure`` records that claim; the gate then
  stops treating the violation as unfinished work. Without it the incentive is to weaken the rule
  until the bug disappears.
* **The CVLR crate source is mounted and authoritative** (§5.5). The prompt says so, because the
  hallucination this backend is most exposed to is an invented macro, and the answer is on disk in
  the version this build resolves.
"""

import asyncio
import dataclasses
from pathlib import Path
from typing import Literal, Sequence, TypedDict, override

from langchain_core.tools import BaseTool
from pydantic import Field

from graphcore.graph import CacheMarker, RawPromptInput, SummaryConfig, tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithImplementation,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.buffer import apply_spec_update, get_spec_tool
from composer.authoring.judge import (
    ContextualFeedbackThunk,
    JudgeBuilder,
    JudgeInput,
    JudgeState,
    RebuttalBase,
    build_feedback_judge_generic,
    judge_host_of,
)
from composer.authoring.state import (
    SkippedProperty,
    ValidationStamper,
    make_validation_stamper,
)
from composer.authoring.tools import give_up_tool, skip_tools
from composer.diagnostics.budget import (
    BudgetExceeded,
    budget_monitor,
    budget_pressure,
    constraint_sort_to_noun,
    raise_budget_exceeded,
)
from composer.spec.context import CvlrGeneration, CvlrJudge, WorkflowContext
from composer.spec.cvlr.editor import editor_tools
from composer.spec.cvlr.harness import GeneratedHarness
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.state import (
    CVLR_JUDGE_KEY,
    FEEDBACK,
    PROVER_VALIDATION_KEY,
    CvlrGenerationInput,
    CvlrGenerationState,
    HarnessAssumptions,
    PropertyRuleMapping,
    RuleSubject,
    check_cvlr_completion,
    harness_assumptions,
    tuning_history,
    validate_property_rules,
    validate_rule_subjects,
)
from composer.spec.cvlr.verify import (
    ExpectRuleFailure,
    ExpectRulePassage,
    HarnessTarget,
    VerifyDeps,
    gate_tools,
)
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import run_to_completion
from composer.spec.service_host import ServiceHost
from composer.spec.solana.model import SolanaComponentInstance
from composer.spec.system_model import component_context
from composer.spec.types import Curtailed, PropertyFormulation, PropertyTitle
from composer.pipeline.ptypes import GaveUp
from composer.ui.tool_display import ToolDisplay, suppress_ack, tool_display

type BatchHarnessResult = GeneratedHarness | Curtailed[GeneratedHarness] | GaveUp


# ---------------------------------------------------------------------------
# Buffer tools
# ---------------------------------------------------------------------------


@tool_display(
    label=lambda p: f"Putting harness draft ({len(p.get('harness', ''))} chars)",
    result=suppress_ack("Put harness result", ("Accepted",)),
)
class PutHarness(WithImplementation[Command | str], WithInjectedId):
    """Put a CVLR harness module into the working buffer, replacing it entirely.

    There is no put-time compile check — call ``cargo_check`` for that; it is fast and free. Any put
    invalidates a previous ``verify_rules`` stamp, so run the prover *after* you have finished
    iterating, not before.
    """

    harness: str = Field(
        description=(
            "The full source of the harness module: one Rust file, the contents of a module inside "
            "the crate under verification. It may `use crate::...` to reach the program's own items, "
            "and `use cvlr::prelude::*;` for the specification language. Declare rules as `#[rule]` "
            "functions, or with `cvlr_rules!` when one property applies across several handlers."
        )
    )

    @override
    def run(self) -> Command | str:
        return apply_spec_update(tool_call_id=self.tool_call_id, text=self.harness)


_GET_HARNESS_DESCRIPTION = """
    Retrieve the current harness module source.
    """


def get_harness_tool(ty: type) -> BaseTool:
    """The read-back tool over the harness buffer, named ``get_harness`` for author and judge alike.

    The judge is *required* to read the draft back through this rather than review the copy in its
    prompt, which is why both get the same tool under the same name."""
    return get_spec_tool(
        ty,
        name="get_harness",
        description=_GET_HARNESS_DESCRIPTION,
        missing="No harness draft written",
        display=ToolDisplay("Reading current harness draft", None),
    )


_SKIP_DESCRIPTION = """
    Declare that you are skipping a property from the batch.

    You must give the property's title and a justification. Skipping excludes the property from the
    publish-time property→rule mapping check; use it only after a genuine attempt to formalize, and
    say what specifically blocked it.
    """

_SKIP_REASON = (
    "Justification for why this property cannot be expressed as a CVLR rule — a missing account "
    "model, a construct the prover cannot reason about, an unbounded loop, and so on"
)

_GIVE_UP_DESCRIPTION = """
    Last-resort exit when you have exhausted other mechanisms to complete the task. The batch will
    be reported as failed with your ``reason``. Prefer skipping individual properties.
    """


# ---------------------------------------------------------------------------
# Feedback judge
# ---------------------------------------------------------------------------


class Rebuttal(RebuttalBase):
    """A rebuttal to a specific piece of prior-round feedback, backed by evidence.

    File one when a suggestion was tried and provably does not work — the construction does not
    compile, the crate does not have the helper the judge named, the prover timed out on it. Do not
    file rebuttals for feedback you merely disagree with; address those by revising the harness.
    """

    evidence_type: Literal[
        "compilation_failure",
        "prover_output",
        "crate_source",
        "manual_citation",
        "reasoned",
    ] = Field(
        description=(
            "What backs this rebuttal. Use 'compilation_failure' for rustc output from trying the "
            "suggestion, 'prover_output' for a verdict or counterexample showing what the "
            "suggestion actually does, 'crate_source' when the CVLR source you read contradicts it "
            "(cite the versioned path), 'manual_citation' for a documentation citation, and "
            "'reasoned' for an argument backed by neither."
        )
    )


@dataclasses.dataclass
class FeedbackDependencies:
    thunk: ContextualFeedbackThunk[Rebuttal, HarnessAssumptions]
    stamper: ValidationStamper
    #: The developer's project, so the judge is shown the munge *diff* rather than a description of
    #: it (``docs/who-edits-the-program.md`` §4, move B).
    pristine: Path


@tool_display("Getting feedback", "Feedback")
class FeedbackTool(
    WithInjectedId,
    WithInjectedState[CvlrGenerationState],
    WithAsyncDependencies[Command | str, FeedbackDependencies],
):
    """Get feedback on your harness and any skip declarations.

    The judge evaluates whether the rules meaningfully demonstrate the batch's properties, whether
    every property is accounted for, and whether the skip justifications hold. It reads your draft
    back itself rather than trusting a copy.

    If a prior-round suggestion was tried and provably does not work, put it in ``rebuttals`` with
    concrete evidence. An empty list is the expected default.
    """

    rebuttals: list[Rebuttal] = Field(
        default_factory=list,
        description="Optional rebuttals to specific pieces of prior-round feedback.",
    )

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        if budget_pressure():
            return (
                "Good? False\nFeedback The feedback judge was not run due to budget constraints. "
                "See the system alert: feedback approval is no longer required for this task."
            )
        skipped = self.state["skipped"]
        with self.tool_deps() as deps:
            verdict = await deps.thunk(
                harness_assumptions(self.state, deps.pristine),
                draft,
                skipped,
                self.rebuttals,
                self.tool_call_id,
            )
            message = f"Good? {verdict.good}\nFeedback {verdict.feedback}"
            if not verdict.good:
                return message
            stamp = deps.stamper(self.state, tuning_history(self.state))
        return tool_state_update(self.tool_call_id, message, validations=stamp)


@component_context
class _CvlrJudgeParams(TypedDict):
    """Render variables for the judge's initial prompt.

    ``sort`` is fixed: this backend only ever verifies an existing program, and naming it keeps the
    template renderable under ``COMPOSER_STRICT_TEMPLATES``, which is what the template fuzzer runs
    it as. ``context`` names the concrete unit type so the fuzzer can draw one coherent with the
    sort — the ``FeatureUnit`` protocol is not something it can construct."""

    properties: list[PropertyFormulation]
    context: SolanaComponentInstance | None
    sort: Literal["existing"]
    program: str


class CvlrMountParams(TypedDict):
    """What both system prompts need to state about the mounted CVLR source.

    The versions belong in the *system* prompt rather than only in the task: which release is on
    disk is a standing fact about the session, and an agent that reads it once per turn is less
    likely to reach for a helper from a different line."""

    cvlr_versions: str


class CvlrAuthorSystemParams(CvlrMountParams):
    module: str


_JudgeTemplate = TypedTemplate[_CvlrJudgeParams]("cvlr_feedback_prompt.j2")
_JudgeSystemTemplate = TypedTemplate[CvlrMountParams]("cvlr_property_judge_system_prompt.j2")
_PropertyGenSysTemplate = TypedTemplate[CvlrAuthorSystemParams](
    "cvlr_property_generation_system_prompt.j2"
)


def with_assumptions(base: JudgeInput, assumptions: HarnessAssumptions) -> JudgeInput:
    """The judge's ``input_lift``: append what the draft cannot show to what ``input_parts`` built.

    Appended rather than prepended because the order is a claim about what the review opens on —
    the artifact first, then the caveats attached to it.
    """
    return {**base, "input": [*base["input"], *assumptions.briefing()]}


def _build_feedback_thunk(
    judge_ctx: WorkflowContext[CvlrJudge],
    env: ServiceHost,
    props: list[PropertyFormulation],
    component: SolanaComponentInstance | None,
    program: str,
    cvlr_versions: str,
    extra_tools: Sequence[BaseTool],
) -> ContextualFeedbackThunk[Rebuttal, HarnessAssumptions]:
    """The CVLR feedback judge.

    ``extra_tools`` is where the CVLR crate source reaches the judge. It matters more here than for
    the author: the judge's most useful and most dangerous move is "use helper X instead", and a
    judge that cannot check whether X exists in the resolved version sends the author after a name
    the compiler will reject.

    Contextual rather than plain because the draft is not the whole artifact under review: the
    summaries and munges the author has accumulated ride in per invocation, since they change what
    a green rule means and the judge cannot see either in the source it reads.
    """

    def apply_prompt(
        builder: JudgeBuilder,
        _draft: str,
        _skipped: Sequence[SkippedProperty],
        _rebuttals: Sequence[Rebuttal],
    ) -> JudgeBuilder:
        return _JudgeTemplate.bind(
            {
                "properties": props,
                "context": component,
                "sort": "existing",
                "program": program,
            }
        ).render_to(builder.with_initial_prompt_template)

    def input_parts(
        draft: str, skipped: Sequence[SkippedProperty], rebuttals: Sequence[Rebuttal]
    ) -> list[str | dict]:
        """Everything the review needs that the draft yields. What it does not — the author's
        summaries and munges — arrives through :func:`with_assumptions`, which is the only
        callback that sees the invocation's context."""
        parts: list[str | dict] = ["The proposed CVLR harness module is", draft]
        declared = rule_names(draft)
        parts.append(
            "It declares these rules: " + (", ".join(declared) if declared else "none")
        )
        if skipped:
            parts.append("The author explicitly skipped these properties:")
            parts += [f"  Property {s.property_title}: {s.reason}" for s in skipped]
        if rebuttals:
            parts.append(
                "The author has filed the following rebuttals against prior-round feedback. "
                "Empirical evidence ('compilation_failure', 'prover_output', 'crate_source', "
                "'manual_citation') carries near-binding weight; 'reasoned' rebuttals are a "
                "conversation, not a veto."
            )
            for index, rebuttal in enumerate(rebuttals, 1):
                parts.append(
                    f"  Rebuttal {index} [{rebuttal.evidence_type}]\n"
                    f"    Addressing: {rebuttal.prior_feedback_reference}\n"
                    f"    Evidence: {rebuttal.evidence}"
                )
        return parts

    return build_feedback_judge_generic(
        st=JudgeState,
        inp=JudgeInput,
        ctx=judge_ctx,
        host=judge_host_of(env),
        apply_system=lambda b: _JudgeSystemTemplate.bind(
            {"cvlr_versions": cvlr_versions}
        ).render_to(b.with_sys_prompt_template),
        apply_prompt=apply_prompt,
        input_parts=input_parts,
        readback=get_harness_tool(JudgeState),
        description="CVLR harness feedback judge",
        thread_prefix="cvlr-feedback",
        input_lift=with_assumptions,
        extra_tools=extra_tools,
    )


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


@tool_display(label="Publishing CVLR harness", result=None)
class PublishResultTool(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, list[PropertyTitle]],
):
    """Signal completion.

    Gated on both required validations: ``verify_rules`` must have accepted the draft *as it now
    stands*, and so must the feedback judge. Any put or skip since either stamp invalidates it.

    ``property_rules`` is checked against the rules your draft actually declares — every non-skipped
    property must be demonstrated by at least one, and every rule you wrote must be tied back to a
    property.
    """

    commentary: str = Field(
        description="Human-readable commentary on the harness: what it models, what it assumes, "
        "and anything a reader of the report needs in order to trust the verdicts"
    )
    property_rules: list[PropertyRuleMapping] = Field(
        description="The property→rules mapping, for every property you did not skip."
    )
    rule_subjects: list[RuleSubject] = Field(
        description="One entry per rule your draft declares, saying what that rule drives: the "
        "program function it calls, or — if it drives a stand-in you wrote in the harness — which "
        "program function that stands in for and why you could not call the real one."
    )

    @override
    async def run(self) -> Command | str:
        if (err := check_cvlr_completion(self.state)) is not None:
            return err
        draft = self.state["curr_spec"]
        assert draft is not None, "check_cvlr_completion rejects an unwritten draft"
        if not budget_pressure():
            # In the wrap-up window the gates are lifted and the declared mapping is accepted as
            # given; outside it, the draft is the ground truth.
            with self.tool_deps() as titles:
                err = validate_property_rules(
                    self.property_rules, self.state["skipped"], titles, draft
                )
            if err is None:
                err = validate_rule_subjects(self.rule_subjects, draft)
            if err is not None:
                return err
        return tool_state_update(
            self.tool_call_id,
            "Accepted",
            result=self.commentary,
            property_rules=self.property_rules,
            rule_subjects=self.rule_subjects,
            failed=False,
        )


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------


class CvlrGenerationSummaryConfig(SummaryConfig[CvlrGenerationState]):
    """Summarization prompts for when the author's context window fills up."""

    @override
    def get_summarization_prompt(self, state: CvlrGenerationState) -> str:
        return """
You are approaching the context limit for your task. After this point your context will be cleared
and the task restarted from the initial prompt.

To enable you to continue effectively, summarize the current state of your task. In particular:
1. The current state of your harness draft — its structure, which properties you have formalized,
   which you have skipped and why.
2. Which rules demonstrate which properties (the property→rule mapping you intend to declare).
3. Any rules you have marked expected-to-fail, and why you believe the violation is real.
4. Any unresolved feedback: rustc errors from `cargo_check`, verdicts or counterexamples from
   `verify_rules`, or points from the feedback judge you have not yet addressed.
5. What you learned from reading the CVLR crate source — which helpers and macros exist at the
   version this project resolves, and the exact paths they live under — so the next iteration does
   not have to look them up again.
"""

    @override
    def get_resume_prompt(self, state: CvlrGenerationState, summary: str) -> str:
        return f"""
You are resuming this task already in progress. The current version of your harness draft (if any)
is available via the `get_harness` tool.

A summary of your work up to this point:

BEGIN SUMMARY:
{summary}
END SUMMARY

**IMPORTANT**: Nothing has changed since the summary was produced. You do NOT need to re-read CVLR
source for facts already captured above. If you have outstanding compiler errors, prover verdicts or
judge feedback to address, proceed directly to addressing them.
"""


# ---------------------------------------------------------------------------
# Top-level batch entry
# ---------------------------------------------------------------------------


@component_context
class CvlrPropertyGenParams(TypedDict):
    """Per-batch render variables for the author's initial prompt."""

    context: SolanaComponentInstance | None
    properties: list[PropertyFormulation]
    program: str
    module: str
    cvlr_versions: str
    sort: Literal["existing"]


_PropertyGenTemplate = TypedTemplate[CvlrPropertyGenParams]("cvlr_property_generation_prompt.j2")

_BUDGET_WRAPUP_MESSAGE = """
<system-alert>
You have almost exceeded the {resource} budget for this task. Wrap up IMMEDIATELY; a partial harness
is better than going over budget. Concretely:

- The prover and feedback validation requirements on publishing have been lifted. You no longer
  need approval from the feedback judge — ignore any pending or future feedback, including a judge
  response saying it was terminated.
- Do NOT start new prover runs or research.
- Delete any rules that do not currently compile, and any whose verdict you never saw.
- Skip (`record_skip`) every property you have not gotten to work, citing budget exhaustion.
- Then publish what remains via the `result` tool.
</system-alert>
"""


async def batch_cvlr_generation(
    ctx: WorkflowContext[CvlrGeneration],
    *,
    props: list[PropertyFormulation],
    component: SolanaComponentInstance | None,
    env: ServiceHost,
    description: str,
    program: str,
    module: str,
    cvlr_versions: str,
    target: HarnessTarget,
    verify: VerifyDeps,
    pristine: Path,
    crate_tools: Sequence[BaseTool] = (),
) -> BatchHarnessResult:
    """Author one harness module covering ``props``.

    The graph ends when the agent calls ``result`` (publish) or ``give_up``. Both ``verify_rules``
    and the feedback judge must have stamped the *current* buffer for ``result`` to be accepted.

    ``target`` and ``verify`` are the caller's: they carry the shared cargo session, the run's one
    working tree and where this unit's draft is staged. One tree for every unit, because each unit's
    module is behind its own cargo feature and a module a build does not compile cannot break it
    (``docs/single-working-tree.md``).

    ``pristine`` is the developer's project — the *from* side of every munge diff, and the only thing
    the editor sub-agent needs that is not on ``target``.

    ``crate_tools`` mounts the resolved CVLR source (§5.5). They go to the author *and* the judge;
    the prompt states the source is present and authoritative.
    """
    bound_template = _PropertyGenTemplate.bind(
        {
            "context": component,
            "properties": props,
            "program": program,
            "module": module,
            "cvlr_versions": cvlr_versions,
            "sort": "existing",
        }
    )

    titles = [p.title for p in props]
    judge_ctx = ctx.child(CVLR_JUDGE_KEY)
    feedback_deps = FeedbackDependencies(
        thunk=_build_feedback_thunk(
            judge_ctx, env, props, component, program, cvlr_versions, crate_tools
        ),
        stamper=make_validation_stamper(FEEDBACK),
        pristine=pristine,
    )

    sys_prompt: list[RawPromptInput | type[CacheMarker]] = [
        _PropertyGenSysTemplate.bind(
            {"module": module, "cvlr_versions": cvlr_versions}
        ).render_to
    ]

    builder = (
        # "long" cache: a prover run can take many minutes, and the author's context should still be
        # warm on the other side of one.
        env.builder_heavy()
        .with_state(CvlrGenerationState)
        .with_input(CvlrGenerationInput)
        .with_output_key("result")
        .with_tools(env.source_tools)
        .with_tools(env.rag_tools)
        .with_tools(crate_tools)
        .with_tools(
            [
                PutHarness.as_tool("put_harness"),
                get_harness_tool(CvlrGenerationState),
                *skip_tools(
                    titles, skip_description=_SKIP_DESCRIPTION, skip_reason=_SKIP_REASON
                ),
                ExpectRuleFailure.as_tool("expect_rule_failure"),
                ExpectRulePassage.as_tool("expect_rule_passage"),
                *gate_tools(target, verify),
                *editor_tools(
                    ctx, env, target=target, pristine=pristine, read_tools=env.source_tools
                ),
                FeedbackTool.bind(feedback_deps).as_tool("feedback_tool"),
                PublishResultTool.bind(titles).as_tool("result"),
                give_up_tool(
                    name="give_up",
                    description=_GIVE_UP_DESCRIPTION,
                    label="CVLR harness generation",
                    reason_description="Why you are giving up on this batch",
                ),
                ctx.get_memory_tool(),
            ]
        )
        .with_sys_prompt(sys_prompt)
        .inject(lambda b: bound_template.render_to(b.with_initial_prompt_template))
        .with_summary_config(CvlrGenerationSummaryConfig())
        .with_monitor(
            budget_monitor(
                warning_message=lambda _s, c: _BUDGET_WRAPUP_MESSAGE.format(
                    resource=constraint_sort_to_noun(c)
                ),
                state_transformer=lambda _s, _c: {
                    "required_validations": [],
                    "budget_curtailed": True,
                },
                on_overbudget=raise_budget_exceeded,
            )
        )
    )
    graph = builder.compile_async()

    init_state = CvlrGenerationInput(
        curr_spec=None,
        input=[],
        required_validations=[PROVER_VALIDATION_KEY, FEEDBACK],
        skipped=[],
        property_rules=[],
        rule_subjects=[],
        summaries=[],
        munges=[],
        validations={},
        expected_failures={},
        failed=None,
        budget_curtailed=False,
    )

    tid, mnem = await ctx.thread_and_mnemonic()
    try:
        res_state = await run_to_completion(
            graph,
            init_state,
            thread_id=tid,
            description=f"{description} ({mnem})",
            recursion_limit=ctx.recursion_limit,
        )
    except BudgetExceeded as exc:
        return Curtailed(None, detail=str(exc))

    assert "result" in res_state
    assert res_state["failed"] is not None
    if res_state["failed"]:
        if res_state["budget_curtailed"]:
            # A give-up issued after the wrap-up order is not a considered "this batch is
            # unformalizable" judgment — it is the budget talking. Keep the agent's account.
            return Curtailed(None, detail=res_state["result"])
        return GaveUp(reason=res_state["result"])

    draft = res_state["curr_spec"]
    assert draft is not None
    generated = GeneratedHarness(
        commentary=res_state["result"],
        harness=draft,
        skipped=res_state["skipped"],
        property_rules=res_state["property_rules"],
        rule_subjects=res_state["rule_subjects"],
        summaries=res_state["summaries"],
        munges=res_state["munges"],
        expected_failures=res_state["expected_failures"],
        declared_rules=list(rule_names(draft)),
        final_link=res_state.get("prover_link"),
    )
    if res_state["budget_curtailed"]:
        # Published under lifted gates: hand it back as an explicitly unreliable partial.
        return Curtailed(generated)
    return generated
