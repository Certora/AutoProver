
from typing import Callable, Sequence
from typing_extensions import TypedDict
from composer.spec.service_host import Sort, ServiceHost

from composer.authoring.judge import JudgeState, build_feedback_judge
from composer.authoring.state import SkippedProperty
from composer.spec.context import (
    WorkflowContext, CVLJudge
)
from composer.spec.types import PropertyFormulation
from composer.cvl.tools import get_cvl
from composer.spec.gen_types import TemplateInstantiation, TypedTemplate, ITypedTemplate, PartialTemplate
from composer.spec.cvl_generation import FeedbackToolContext, Rebuttal
from composer.spec.system_model import ContractComponentInstance, component_context

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
    """The CVL property judge, as the context the author's ``feedback_tool`` reads.

    The batch's properties, skips and rebuttals are all rendered into the judge's *prompt* here
    (``property_judge_prompt.j2``); only the spec under review and the caller's ``extra_inputs``
    ride in as input text."""

    if system_prompt is None:
        system_prompt = FeedbackSystemTemplate.bind({"sort": env.sort})

    def render_prompt(
        skipped: Sequence[SkippedProperty], rebuttals: Sequence[Rebuttal]
    ) -> TemplateInstantiation:
        return prompt.bind({
            "properties": props,
            "rebuttals": rebuttals,
            "skipped": skipped,
        })

    def input_parts(
        cvl: str, _skipped: Sequence[SkippedProperty], _rebuttals: Sequence[Rebuttal]
    ) -> list[str | dict]:
        parts: list[str | dict] = []
        if extra_inputs:
            if isinstance(extra_inputs, list):
                parts.extend(extra_inputs)
            else:
                parts.extend(extra_inputs())
        parts.append("The proposed CVL file is")
        parts.append(cvl)
        return parts

    return FeedbackToolContext(
        feedback_thunk=build_feedback_judge(
            ctx=ctx,
            env=env,
            system_prompt=system_prompt,
            render_prompt=render_prompt,
            input_parts=input_parts,
            readback=get_cvl(JudgeState),
            description="Property feedback judge",
            thread_prefix="feedback",
        ),
        titles=[p.title for p in props],
    )
