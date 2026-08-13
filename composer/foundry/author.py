"""Foundry test author — generates ``.t.sol`` tests for property formulations.

The single home for the foundry authoring workflow: the author's tools, the
feedback judge, the publish gate, and the batch entry point
(``batch_foundry_test_generation``). The ``forge test`` runner lives in
``composer.foundry.runner``; state types and the publish-gate checks live in
``composer.foundry.state``.

Workflow shape:

* Single ``curr_spec`` buffer per batch (one ``.t.sol`` file), written
  via ``put_test_raw``. No put-time compile check; ``forge_test`` is the gate.
* A feedback judge (``feedback_tool``) reviews the draft against the batch's
  properties. Publish requires both a green unseeded ``forge_test`` run AND a
  judge acceptance stamped on the *current* buffer.
* The publish-time property→test mapping is validated against the test names
  forge actually ran (from its JSON output), in both directions: every
  non-skipped property is demonstrated by a test that ran, and every test
  that ran is tied back to a property.
* Per-test expected-failure marking via ``expect_test_failure``.
* No prover-config editor — foundry projects are assumed pre-configured.
"""

from dataclasses import dataclass
import asyncio
from typing import (
    Callable, Literal, Sequence, overload, override
)
from typing_extensions import TypedDict

from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from graphcore.graph import tool_state_update
from graphcore.summary import SummaryConfig
from graphcore.tools.schemas import (
    WithAsyncDependencies, WithAsyncImplementation, WithImplementation,
    WithInjectedId, WithInjectedState,
)
from composer.authoring.buffer import (
    SpecBuffer, SpecBufferSet, apply_spec_update, get_spec_tool,
)
from composer.authoring.judge import (
    FeedbackThunk, JudgeBuilder, JudgeState, RebuttalBase, build_feedback_judge,
)
from composer.authoring.state import SkippedProperty
from composer.authoring.tools import give_up_tool
from composer.pipeline.core import Curtailed, GaveUp
from composer.spec.context import FoundryGeneration, FoundryJudge, WorkflowContext
from composer.diagnostics.budget import (
    BudgetExceeded, budget_monitor, budget_pressure,
    raise_budget_exceeded, constraint_sort_to_noun,
)
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import run_to_completion
from composer.spec.types import CheckName, PropertyFormulation, PropertyTitle
from composer.spec.system_model import ContractComponentInstance, component_context
from composer.spec.service_host import ServiceHost
from composer.ui.tool_display import (
    ToolDisplay, suppress_ack, tool_display,
)

from composer.foundry.runner import get_forge_test_tool
from composer.authoring.state import make_validation_stamper
from composer.foundry.state import (
    FEEDBACK,
    FORGE_TEST_VALIDATION_KEY,
    FOUNDRY_JUDGE_KEY,
    FoundryGenerationInput,
    FoundryGenerationState,
    PropertyTestMapping,
    check_foundry_completion,
    validate_property_tests,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class GeneratedFoundryTest(BaseModel):
    """Successful output of the foundry author for a batch."""
    commentary: str
    test_source: str
    skipped: list[SkippedProperty] = Field(default_factory=list)
    property_tests: list[PropertyTestMapping] = Field(default_factory=list)
    # Forge ground truth at publish time: the tests that actually ran in the
    # gating unseeded run, and the author's expected-failure markings (test
    # name -> reason). Together they give every test a pass / expected-failure
    # status without trusting the model's own transcription.
    expected_failures: dict[CheckName, str] = Field(default_factory=dict)
    ran_tests: list[CheckName] = Field(default_factory=list)

    def property_checks(self) -> list[tuple[PropertyTitle, list[CheckName]]]:
        """Property title -> the foundry test names that demonstrate it (the report's
        `ReportableResult` adapter; pairs with the structurally-shared ``skipped`` field)."""
        return [(m.property_title, m.tests) for m in self.property_tests]
    
    @property
    def artifact_text(self) -> str:
        return self.test_source

    @property
    def output_link(self) -> str | None:
        return None  # foundry has no external run service


type BatchFoundryResult = GeneratedFoundryTest | Curtailed[GeneratedFoundryTest] | GaveUp


# ---------------------------------------------------------------------------
# Author tools
# ---------------------------------------------------------------------------


@tool_display(
    label=lambda p: f"Putting test draft ({len(p.get('test_source', ''))} chars)",
    result=suppress_ack("Put test result", ("Accepted",)),
)
class PutTestRaw(WithImplementation[Command | str], WithInjectedId):
    """
    Put a foundry test file into the working buffer.

    The provided source replaces the entire test buffer. There is no
    put-time compile check — call ``forge_test`` to verify the draft actually
    builds and passes. ``forge_test``'s green stamp is invalidated by any
    subsequent ``put_test_raw``, so call ``forge_test`` *after* you're done
    iterating.
    """
    test_source: str = Field(
        description=(
            "The full source of the foundry test file (a single ``.t.sol`` "
            "file's contents). Must declare a contract that extends "
            "``forge-std/Test.sol``'s ``Test`` and contain ``test_*`` "
            "functions for the properties being verified."
        )
    )

    @override
    def run(self) -> Command | str:
        return apply_spec_update(tool_call_id=self.tool_call_id, text=self.test_source)


_GET_TEST_DESCRIPTION = """
    Retrieve the textual representation of the current foundry test.
    """


@overload
def get_test_tool[S: SpecBufferSet](ty: type[S]) -> BaseTool: ...


@overload
def get_test_tool[S: SpecBuffer](ty: type[S]) -> BaseTool: ...


def get_test_tool(ty: type) -> BaseTool:
    """The read-back tool over the test buffer, named ``get_test`` for both the author and its
    judge."""
    return get_spec_tool(
        ty,
        name="get_test",
        description=_GET_TEST_DESCRIPTION,
        missing="No test draft written",
        display=ToolDisplay("Reading current test draft", None),
        title="GetTestTool",
    )

@tool_display(
    lambda p: f"Skipping property `{p.get('property_title', '?')}`",
    suppress_ack("Skip result", ("Recorded skip",)),
)
class _RecordSkipSchema(
    WithInjectedId,
    # deps: the batch's property titles
    WithAsyncDependencies[Command, list[PropertyTitle]],
):
    """
    Declare that you are skipping a property from the batch.

    You must provide the property's title and a justification. Skipping
    excludes the property from the publish-time property→test mapping
    check; only use after a genuine attempt to formalize.
    """
    property_title: PropertyTitle = Field(
        description="The snake_case title of the property from the batch listing"
    )
    reason: str = Field(
        description="Justification for why this property cannot be formalized as a foundry test"
    )

    @override
    async def run(self) -> Command:
        with self.tool_deps() as titles:
            if self.property_title not in titles:
                return tool_state_update(
                    self.tool_call_id,
                    f"Unknown property title {self.property_title!r}. Must be one "
                    f"of: {', '.join(titles)}.",
                )
        if not self.reason.strip():
            return tool_state_update(
                self.tool_call_id,
                "A non-empty justification is required when skipping a property.",
            )
        skip = SkippedProperty(
            property_title=self.property_title,
            reason=self.reason,
        )
        return tool_state_update(
            self.tool_call_id,
            f"Recorded skip for property {self.property_title}.",
            skipped=[skip],
        )


@tool_display(
    lambda p: f"Un-skipping property `{p.get('property_title', '?')}`",
    suppress_ack("Unskip result", ("Removed skip",)),
)
class _UnskipSchema(
    WithInjectedId,
    # deps: the batch's property titles
    WithAsyncDependencies[Command, list[PropertyTitle]],
):
    """
    Remove a previously declared skip for a property. Use this if you later
    find a way to formalize a property you previously skipped.
    """
    property_title: PropertyTitle = Field(
        description="The snake_case title of the property to un-skip"
    )

    @override
    async def run(self) -> Command:
        with self.tool_deps() as titles:
            if self.property_title not in titles:
                return tool_state_update(
                    self.tool_call_id,
                    f"Unknown property title {self.property_title!r}. Must be one "
                    f"of: {', '.join(titles)}.",
                )
        # Sentinel reason "" — merge_skips drops empty-reason entries.
        skip = SkippedProperty(property_title=self.property_title, reason="")
        return tool_state_update(
            self.tool_call_id,
            f"Removed skip for property {self.property_title}.",
            skipped=[skip],
        )


@tool_display(lambda p: f"Expecting test `{p['test_name']}` to fail", None)
class ExpectTestFailure(WithAsyncImplementation[Command], WithInjectedId):
    """
    Mark a foundry test as expected to fail.

    The ``forge_test`` runner excludes expected-fail tests from the
    all-green check, so a failing test marked here will not block the
    publish gate. Use only when the failure is the *demonstration* of
    a property (e.g., a regression test that proves a negation).
    """
    test_name: str = Field(
        description="The name of the test function (e.g., `test_RevertWhen_Unauthorized`)"
    )
    reason: str = Field(description="Why this test is expected to fail")

    @override
    async def run(self) -> Command:
        # The merge treats an empty reason as "remove the marking" (see
        # _merge_expected_failures), so an empty reason must not get through.
        if not self.reason.strip():
            return tool_state_update(
                self.tool_call_id,
                "A non-empty reason is required when marking a test as expected to fail.",
            )
        return tool_state_update(
            tool_call_id=self.tool_call_id,
            content="Success",
            expected_failures={self.test_name: self.reason},
        )


@tool_display(lambda p: f"Expecting test `{p['test_name']}` to pass", None)
class ExpectTestPassage(WithAsyncImplementation[Command], WithInjectedId):
    """
    Unmark a test previously marked expected-to-fail.

    By default every test is expected to pass; only call this to revert a
    prior ``expect_test_failure``.
    """
    test_name: str = Field(
        description="The name of the test function previously marked expected-to-fail"
    )

    @override
    async def run(self) -> Command:
        # Empty reason = remove the marking (see _merge_expected_failures).
        return tool_state_update(
            tool_call_id=self.tool_call_id,
            content="Success",
            expected_failures={self.test_name: ""},
        )


# ---------------------------------------------------------------------------
# Feedback judge
# ---------------------------------------------------------------------------


class Rebuttal(RebuttalBase):
    """A rebuttal to a specific piece of feedback from a prior round, backed
    by evidence.

    File a rebuttal when a prior-round suggestion was tried and provably does
    not work — the suggested construction does not compile, the suggested
    test demonstrably does not exercise the property, etc. Do NOT file
    rebuttals for feedback you merely disagree with; address those by
    revising the tests.
    """
    evidence_type: Literal[
        "compilation_failure",
        "test_run_output",
        "execution_trace",
        "manual_citation",
        "reasoned",
    ] = Field(
        description=(
            "What backs this rebuttal. Use 'compilation_failure' for forge/solc "
            "build errors hit when trying the suggestion, 'test_run_output' for "
            "forge test output demonstrating the suggestion's actual behavior, "
            "'execution_trace' for a trace showing what a run actually did, "
            "'manual_citation' for a foundry / forge-std documentation citation, "
            "and 'reasoned' for an argument not backed by tool output or "
            "documentation."
        )
    )


@dataclass
class FeedbackDependencies:
    thunk: FeedbackThunk[Rebuttal]
    stamper: Callable[[FoundryGenerationState], dict[str, str]]


@tool_display("Getting feedback", "Feedback")
class FeedbackTool(
    WithInjectedId, WithInjectedState[FoundryGenerationState],
    WithAsyncDependencies[Command | str, FeedbackDependencies],
):
    """
    Receive feedback on your foundry tests and any skip declarations.
    The judge will evaluate whether the tests meaningfully demonstrate the
    batch's properties, coverage (all properties accounted for), and the
    validity of any skip justifications.

    If a prior-round suggestion from the judge was tried and provably does not
    work, file it in `rebuttals` with concrete evidence (compile error text,
    test run output, an execution trace, a documentation citation). Do NOT
    file rebuttals for feedback you merely disagree with — address those by
    revising the tests. An empty rebuttal list is the expected default; only
    populate it when you have ground-truth evidence against a prior point.
    """
    rebuttals: list[Rebuttal] = Field(
        default_factory=list,
        description=(
            "Optional rebuttals to specific pieces of prior-round feedback. Each "
            "entry identifies the prior point being rebutted, classifies the "
            "evidence (`compilation_failure` / `test_run_output` / "
            "`execution_trace` / `manual_citation` / `reasoned`), and supplies "
            "the concrete evidence text. Empirical types outweigh reasoned ones "
            "with the judge. Leave empty if you have nothing to rebut."
        ),
    )

    @override
    async def run(self) -> Command | str:
        if self.state["curr_spec"] is None:
            return "No test written"
        if budget_pressure():
            # Don't launch a judge that would be terminated on its first
            # monitor tick; the author's budget warning already tells it
            # feedback approval is no longer required.
            return (
                "Good? False\nFeedback:\nThe feedback judge was not run due to "
                "budget constraints. See the system alert: feedback approval is "
                "no longer required for this task."
            )
        with self.tool_deps() as deps:
            res = await deps.thunk(
                self.state["curr_spec"],
                self.state["skipped"],
                self.rebuttals,
                self.tool_call_id,
            )
            result = f"Good? {res.good}\nFeedback:\n{res.feedback}"
            if res.good:
                return tool_state_update(
                    content=result,
                    tool_call_id=self.tool_call_id,
                    validations=deps.stamper(self.state),
                )
            return result


@component_context
class _FoundryJudgeParams(TypedDict):
    """Render variables for ``foundry_feedback_prompt.j2``.

    ``sort`` is what the shared application-context partial gates its "pre-existing codebase being
    extended" wording on. Foundry only ever verifies an existing project, so it is fixed here —
    naming it also keeps the template renderable under ``COMPOSER_STRICT_TEMPLATES``, which is what
    the fuzzer runs it as now that the template is declared."""
    properties: list[PropertyFormulation]
    context: ContractComponentInstance | None
    sort: Literal["existing"]


_FoundryJudgeTemplate = TypedTemplate[_FoundryJudgeParams]("foundry_feedback_prompt.j2")
class _NoParams(TypedDict):
    pass


_FoundryJudgeSystemTemplate = TypedTemplate[_NoParams]("foundry_property_judge_system_prompt.j2")


def _build_feedback_thunk(
    judge_ctx: WorkflowContext[FoundryJudge],
    env: ServiceHost,
    props: list[PropertyFormulation],
    component: ContractComponentInstance | None,
) -> FeedbackThunk[Rebuttal]:
    """The foundry feedback judge. The shared judge supplies the review protocol (rough draft +
    persistent memory + enforced read-back of the file under review); what is foundry's own is the
    prompt pair and the fact that the skips and rebuttals are stated as input text rather than
    rendered into the prompt template."""

    def apply_prompt(
        builder: JudgeBuilder, _spec: str,
        _skipped: Sequence[SkippedProperty], _rebuttals: Sequence[Rebuttal],
    ) -> JudgeBuilder:
        return _FoundryJudgeTemplate.bind({
            "properties": props, "context": component, "sort": "existing",
        }).render_to(builder.with_initial_prompt_template)

    def input_parts(
        test_source: str, skipped: Sequence[SkippedProperty], rebuttals: Sequence[Rebuttal]
    ) -> list[str | dict]:
        parts: list[str | dict] = [
            "The proposed foundry test file is",
            test_source,
        ]
        if skipped:
            parts.append("The following properties were explicitly skipped by the author:")
            for s in skipped:
                parts.append(f"  Property {s.property_title}: {s.reason}")
        if rebuttals:
            parts.append(
                "The author has filed the following rebuttals against feedback "
                "from prior rounds. Evaluate each per the rebuttal rules in your "
                "instructions. Empirical evidence types (`compilation_failure`, "
                "`test_run_output`, `execution_trace`, `manual_citation`) carry "
                "near-binding weight; `reasoned` rebuttals are a conversation, "
                "not a veto."
            )
            for i, r in enumerate(rebuttals, 1):
                parts.append(
                    f"  Rebuttal {i} [{r.evidence_type}]\n"
                    f"    Addressing: {r.prior_feedback_reference}\n"
                    f"    Evidence: {r.evidence}"
                )
        return parts

    return build_feedback_judge(
        ctx=judge_ctx,
        env=env,
        apply_system=lambda b: _FoundryJudgeSystemTemplate.bind({}).render_to(
            b.with_sys_prompt_template
        ),
        apply_prompt=apply_prompt,
        input_parts=input_parts,
        readback=get_test_tool(JudgeState),
        description="Foundry test feedback judge",
        thread_prefix="foundry-feedback",
    )


# ---------------------------------------------------------------------------
# Publish / give-up tools
# ---------------------------------------------------------------------------


@tool_display(label="Publishing foundry test", result=None)
class PublishResultTool(
    WithInjectedState[FoundryGenerationState],
    WithInjectedId,
    # deps: the batch's property titles
    WithAsyncDependencies[Command | str, list[PropertyTitle]],
):
    """
    Call to signal completion. The publish is gated on the required
    validations: ``forge_test`` must have reported a clean unseeded run AFTER
    your latest ``put_test_raw``, and the feedback judge must have accepted
    the current draft.

    ``property_tests`` is checked against the tests forge actually ran:
    every non-skipped property from the batch must be demonstrated by at
    least one test that ran, and every test that ran must be tied back to
    one of the batch's properties.
    """
    commentary: str = Field(
        description="Human-readable commentary on the generated test file"
    )
    property_tests: list[PropertyTestMapping] = Field(
        description=(
            "The property→tests mapping. For every property you did NOT skip "
            "(referenced by its unique snake_case title from the batch listing), "
            "list the name(s) of the foundry test function(s) in your draft "
            "that demonstrate it (e.g., ``test_RevertWhen_Unauthorized``)."
        )
    )

    @override
    async def run(self) -> Command | str:
        if (err := check_foundry_completion(self.state)) is not None:
            return err
        if not budget_pressure():
            # The forge-ground-truth cross-check requires a recorded run; in
            # the budget wrap-up window (where the agent is told to delete
            # failing tests and publish without re-running forge) the declared
            # mapping is accepted as-is.
            ran = self.state["last_test_names"]
            if ran is None:
                # Unreachable in practice — the forge_test stamp required above
                # implies a run recorded its test names — but defend anyway.
                return "Completion REJECTED: no forge_test run has been recorded."
            with self.tool_deps() as titles:
                err = validate_property_tests(
                    self.property_tests, self.state["skipped"], titles, ran,
                )
            if err is not None:
                return err
        return tool_state_update(
            self.tool_call_id,
            "Accepted",
            result=self.commentary,
            property_tests=self.property_tests,
            failed=False,
        )


_GIVE_UP_DESCRIPTION = """
    Last-resort exit when you've exhausted other mechanisms to complete
    the task. The batch will be reported as failed with your ``reason``.
    """


# ---------------------------------------------------------------------------
# Summary config (context compaction)
# ---------------------------------------------------------------------------


class FoundryGenerationSummaryConfig(SummaryConfig[FoundryGenerationState]):
    """Summarization prompts for the foundry author when the context window
    fills up. Same role as ``PropertyGenerationConfig`` in the CVL author,
    reworded for the foundry workflow (a test file, not a CVL spec)."""

    @override
    def get_summarization_prompt(self, state: FoundryGenerationState) -> str:
        return """
You are approaching the context limit for your task. After this point your
context will be cleared and the task restarted from the initial prompt.

To enable you to continue effectively, summarize the current state of your
task. In particular, summarize:
1. The current state of your test draft (high-level structure, which
   properties you have formalized, which you have skipped and why).
2. Which test functions verify which properties (the property→test mapping
   you intend to declare at publish).
3. Any tests you have marked as expected-to-fail and why.
4. Any unresolved feedback — from the last ``forge_test`` run (compile
   errors, failing tests, etc.) or from the feedback judge — that you still
   need to address.
5. Foundry cheatcode patterns / idioms you discovered during this batch
   so the next iteration does not re-research them.

If your current task itself began with a summary, include the salient parts
of that summary in your new summary.
"""

    @override
    def get_resume_prompt(self, state: FoundryGenerationState, summary: str) -> str:
        return f"""
You are resuming this task already in progress. The current version of your
test draft (if any) is available via the ``get_test`` tool.

A summary of your work up to this point:

BEGIN SUMMARY:
{summary}
END SUMMARY

**IMPORTANT**: Nothing has changed since the summary was produced. You do
NOT need to re-research foundry cheatcode patterns already captured in the
summary. If you have outstanding ``forge_test`` failures or judge feedback to
address, proceed directly with addressing them.
"""


# ---------------------------------------------------------------------------
# Top-level batch entry
# ---------------------------------------------------------------------------

@component_context
class FoundryPropertyGenParams(TypedDict):
    """Per-batch render variables for ``foundry_property_generation_prompt.j2``.
    Mirror of ``PropertyGenParams`` in the CVL author, minus ``resources``
    (no CVL-importable resource concept here)."""
    context: ContractComponentInstance | None
    properties: list[PropertyFormulation]
    contract_name: str
    sort: Literal["existing"]


_FoundryPropertyGenTemplate = TypedTemplate[FoundryPropertyGenParams](
    "foundry_property_generation_prompt.j2"
)

_BUDGET_WRAPUP_MESSAGE = """
<system-alert>
You have almost exceeded the {resource} budget for this task. Wrap up IMMEDIATELY;
a partial test file is better than going over budget. Concretely:

- The forge-test and feedback validation requirements on publishing have been lifted. You
  no longer need approval from the feedback judge — ignore any pending or future feedback,
  including a judge response saying it was terminated.
- Do NOT start new forge runs or research.
- Delete any tests that do not currently compile or pass.
- Skip (`record_skip`) every property you have not gotten to work, citing budget exhaustion.
- Then publish what remains via the `result` tool.
</system-alert>
"""


async def batch_foundry_test_generation(
    ctx: WorkflowContext[FoundryGeneration],
    *,
    project_root: str,
    contract_name: str,
    props: list[PropertyFormulation],
    component: ContractComponentInstance | None,
    env: ServiceHost,
    description: str,
    forge_binary: str = "forge",
    forge_timeout_s: int = 600,
    forge_sem : asyncio.Semaphore
) -> BatchFoundryResult:
    """Author one batch of foundry tests covering ``props``.

    The graph terminates when the agent calls ``result`` (publish) or
    ``give_up``. Both ``forge_test`` and the feedback judge must have stamped
    the *current* buffer for ``result`` to be accepted.

    Caller responsibilities:

    * ``project_root`` is a fully-configured foundry project (has
      ``foundry.toml`` and any required deps under ``lib/``). The author
      stages its draft into ``<project_root>/test/`` and deletes the
      staged file after each ``forge test`` invocation.
    * ``env`` carries the foundry RAG (``rag_tools``) + project source
      tools (``source_tools``). Typically built via
      ``composer.foundry.env.build_foundry_env``.
    * ``contract_name`` / ``component`` / ``props`` are bound into the
      initial prompt (``foundry_property_generation_prompt.j2``).

    ``ctx`` is marked ``FoundryGeneration`` so its cache namespace stays
    distinct from a co-located CVL run's.
    """
    forge_test_tool = get_forge_test_tool(
        project_root, forge_binary=forge_binary, timeout_s=forge_timeout_s, forge_sem=forge_sem
    )

    bound_template = _FoundryPropertyGenTemplate.bind({
        "context": component,
        "properties": props,
        "contract_name": contract_name,
        "sort": "existing"
    })

    titles = [p.title for p in props]
    judge_ctx = ctx.child(FOUNDRY_JUDGE_KEY)
    feedback_deps = FeedbackDependencies(
        thunk=_build_feedback_thunk(judge_ctx, env, props, component),
        stamper=make_validation_stamper(FEEDBACK),
    )

    builder = (
        env.builder_heavy()
        .with_state(FoundryGenerationState)
        .with_input(FoundryGenerationInput)
        .with_output_key("result")
        .with_tools(env.source_tools)
        .with_tools(env.rag_tools)
        .with_tools([
            PutTestRaw.as_tool("put_test_raw"),
            get_test_tool(FoundryGenerationState),
            _RecordSkipSchema.bind(titles).as_tool("record_skip"),
            _UnskipSchema.bind(titles).as_tool("unskip_property"),
            ExpectTestFailure.as_tool("expect_test_failure"),
            ExpectTestPassage.as_tool("expect_test_passage"),
            forge_test_tool,
            FeedbackTool.bind(feedback_deps).as_tool("feedback_tool"),
            PublishResultTool.bind(titles).as_tool("result"),
            give_up_tool(
                name="give_up", description=_GIVE_UP_DESCRIPTION,
                label="foundry-test generation",
                reason_description="Why you are giving up on this batch",
            ),
            ctx.get_memory_tool(),
        ])
        .with_sys_prompt_template("foundry_property_generation_system_prompt.j2")
        .inject(lambda b: bound_template.render_to(b.with_initial_prompt_template))
        .with_summary_config(FoundryGenerationSummaryConfig())
        .with_monitor(budget_monitor(
            warning_message=lambda _s, c: _BUDGET_WRAPUP_MESSAGE.format(resource=constraint_sort_to_noun(c)),
            state_transformer=lambda _s, _c: {"required_validations": [], "budget_curtailed": True},
            on_overbudget=raise_budget_exceeded,
        ))
    )
    graph = builder.compile_async()

    init_state = FoundryGenerationInput(
        curr_spec=None,
        input=[],
        required_validations=[FORGE_TEST_VALIDATION_KEY, FEEDBACK],
        skipped=[],
        property_tests=[],
        validations={},
        expected_failures={},
        last_test_names=None,
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
    except BudgetExceeded as e:
        return Curtailed(None, detail=str(e))

    assert "result" in res_state
    assert res_state["failed"] is not None
    if res_state["failed"]:
        if res_state["budget_curtailed"]:
            # A give-up issued after the wrap-up order isn't a considered "this batch is
            # unformalizable" judgment — it's the budget talking. Keep the agent's account.
            return Curtailed(None, detail=res_state["result"])
        return GaveUp(reason=res_state["result"])
    draft = res_state["curr_spec"]
    assert draft is not None
    generated = GeneratedFoundryTest(
        commentary=res_state["result"],
        test_source=draft,
        skipped=res_state["skipped"],
        property_tests=res_state["property_tests"],
        expected_failures=res_state["expected_failures"],
        ran_tests=res_state["last_test_names"] or [],
    )
    if res_state["budget_curtailed"]:
        # Published under lifted gates: hand it back as an explicitly unreliable partial.
        return Curtailed(generated)
    return generated
