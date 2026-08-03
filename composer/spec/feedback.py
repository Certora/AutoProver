
from typing import Callable, NotRequired, Sequence
from typing_extensions import TypedDict
from composer.spec.service_host import Sort, ServiceHost

from pydantic import BaseModel, Field


from langgraph.graph import MessagesState

from graphcore.graph import FlowInput

from composer.spec.context import (
    WorkflowContext, CVLJudge
)
from composer.spec.types import PropertyFormulation
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.cvl.tools import get_cvl
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools
from composer.spec.gen_types import TemplateInstantiation, TypedTemplate, ITypedTemplate, PartialTemplate
from composer.spec.cvl_generation import FeedbackToolContext, Rebuttal, SkippedProperty
from composer.spec.system_model import ContractComponentInstance, component_context
from composer.spec.util import uniq_thread_id

class PropertyFeedback(BaseModel):
    """
    The feedback on the properties
    """
    good: bool = Field(description="Whether the properties are good as is, or if there is room for improvement")
    feedback: str = Field(description="The feedback on the rule if work is needed. Can be empty if there is no feedback")

class Properties(TypedDict):
    properties: list[PropertyFormulation]

class FeedbackInputs(Properties):
    rebuttals: Sequence[Rebuttal]
    skipped: Sequence[SkippedProperty]

@component_context
class FeedbackInherentParams(TypedDict):
    context: ContractComponentInstance | None
    # Matches the tri-state on the env-level ``sort``:
    #   ``greenfield`` — no pre-existing Solidity anywhere; everything is stubs.
    #   ``update``     — pre-existing codebase being extended; target is a
    #                    new-contract stub, others are stable source.
    #   ``existing``   — pre-existing codebase being verified as-is; target
    #                    has real immutable source.
    sort: Sort

FeedbackTemplate = PartialTemplate[FeedbackInherentParams, FeedbackInputs]("property_judge_prompt.j2")

class JudgeSystemParams(TypedDict):
    sort: Sort

# Judge system prompt, shared between the natspec and source-mode flows. The fs
# primitives are always documented; ``sort`` drives the rest (the template
# compiles out the code_explorer / code_document_ref guidance unless
# ``sort == "existing"``, the only mode that wires those tools).
FeedbackSystemTemplate = TypedTemplate[JudgeSystemParams]("property_judge_system_prompt.j2")

def property_feedback_judge(
    ctx: WorkflowContext[CVLJudge],
    env: ServiceHost,
    prompt: ITypedTemplate[FeedbackInputs],
    props: list[PropertyFormulation],
    *,
    extra_inputs: list[str | dict] | Callable[[], list[str | dict]] | None = None,
    system_prompt: TemplateInstantiation | None = None,
) -> FeedbackToolContext:

    if system_prompt is None:
        system_prompt = FeedbackSystemTemplate.bind({"sort": env.sort})

    builder = env.builder_heavy().with_tools(
        env.all_tools
    )

    class JudgeExtra(RoughDraftState):
        curr_spec: str

    class ST(MessagesState, JudgeExtra):
        result: NotRequired[PropertyFeedback]

    class SpecJudgeInput(FlowInput, JudgeExtra):
        pass

    rough_draft_tools = get_rough_draft_tools(ST)

    def did_rough_draft_read(s: ST, _) -> str | None:
        if not s["did_read"]:
            return "Completion REJECTED: never read rough draft for review"
        return None

    mem = ctx.get_memory_tool()

    staged_workflow = bind_standard(
        builder, ST, validator=did_rough_draft_read
    ).with_input(
        SpecJudgeInput
    ).inject(
        lambda g: system_prompt.render_to(g.with_sys_prompt_template)
    ).with_tools([*rough_draft_tools, mem, get_cvl(ST), ])

    async def the_tool(
        cvl: str,
        skipped: Sequence[SkippedProperty],
        rebuttals: Sequence[Rebuttal],
        within_tool: str,
    ) -> PropertyFeedback:
        workflow = staged_workflow.inject(
            lambda b: prompt.bind({
                "properties": props,
                "rebuttals": rebuttals,
                "skipped": skipped
            }).render_to(b.with_initial_prompt_template)
        ).compile_async()

        input_parts: list[str | dict] = []
        if extra_inputs:
            if isinstance(extra_inputs, list):
                input_parts.extend(extra_inputs)
            else:
                input_parts.extend(extra_inputs())

        input_parts.append("The proposed CVL file is")
        input_parts.append(cvl)
        res = await run_to_completion(
            workflow,
            SpecJudgeInput(input=input_parts, curr_spec=cvl, memory=None, did_read=False),
            thread_id=uniq_thread_id("feedback"),
            recursion_limit=ctx.recursion_limit,
            description="Property feedback judge",
            within_tool=within_tool,
        )
        assert "result" in res
        return res["result"]

    return FeedbackToolContext(feedback_thunk=the_tool, titles=[p.title for p in props])

