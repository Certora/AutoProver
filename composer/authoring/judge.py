"""The feedback judge an authoring session invokes on its own draft.

A judge is a sub-agent, not a scoring function: it gets the session's tool belt, a rough-draft
scratchpad, the run memory, and a read-back of the spec under review, and it must call ``result``
with a structured :class:`PropertyFeedback`. The verdict is a field the judge had to set, never
something recovered from prose — an unparseable review is not a thing that can happen here.

Two properties are enforced rather than requested. It must read the draft back through the tool
(``did_read``) instead of reviewing the copy pasted into its prompt, and the author may file
:class:`RebuttalBase` entries against prior-round feedback, which are rendered into the judge's
input so a point already answered with evidence is answered rather than repeated.
"""

from typing import Any, Awaitable, Callable, NotRequired, Protocol, Sequence

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from graphcore.graph import FlowInput

from composer.authoring.buffer import SpecBufferSet
from composer.authoring.state import SkippedProperty
from composer.spec.context import WorkflowContext
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.service_host import ServiceHost
from composer.spec.util import uniq_thread_id
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools


class PropertyFeedback(BaseModel):
    """
    The feedback on the properties
    """
    good: bool = Field(description="Whether the properties are good as is, or if there is room for improvement")
    feedback: str = Field(description="The feedback on the rule if work is needed. Can be empty if there is no feedback")


class PropertyFeedbackProtocol(Protocol):
    """What a caller of the judge needs from its verdict. A protocol so a backend can hand its
    author a richer feedback type without the stamping logic caring."""

    @property
    def good(self) -> bool: ...

    @property
    def feedback(self) -> str: ...


class RebuttalBase(BaseModel):
    prior_feedback_reference: str = Field(
        description=(
            "A brief quote from, or clear pointer to, the piece of prior-round feedback "
            "this rebuttal addresses. Just enough for the judge to identify which prior "
            "suggestion you are responding to — not a full transcript."
        )
    )
    evidence: str = Field(
        description=(
            "The concrete artifact backing the rebuttal: typecheck error text, a "
            "counterexample summary, a manual quote with location, or a brief reasoned "
            "argument. Keep it short and specific — the judge reads this verbatim."
        )
    )


#: ``(spec, skipped, rebuttals, within_tool) -> feedback``. ``within_tool`` is the calling feedback
#: tool's ``tool_call_id``, plumbed through to the judge's ``run_to_completion`` so its UI panel
#: anchors under the parent tool widget.
type FeedbackThunk[R: RebuttalBase] = Callable[
    [str, Sequence[SkippedProperty], Sequence[R], str],
    Awaitable[PropertyFeedbackProtocol],
]


class _JudgeExtra(RoughDraftState, SpecBufferSet):
    pass


class JudgeState(MessagesState, _JudgeExtra):
    result: NotRequired[PropertyFeedback]


class JudgeInput(FlowInput, _JudgeExtra):
    pass


def _did_rough_draft_read(s: JudgeState, _: Any) -> str | None:
    if not s["did_read"]:
        return "Completion REJECTED: never read rough draft for review"
    return None


#: Applies a prompt to the judge's builder. A backend that renders templates binds its params and
#: calls ``with_*_prompt_template``; one whose prompts are plain strings (a Rust wheel's) calls
#: ``with_sys_prompt`` / ``with_initial_prompt``. Either way the judge itself holds no opinion about
#: where prompt text comes from.
type ApplySystem = Callable[[Any], Any]
type ApplyPrompt[R: RebuttalBase] = Callable[
    [Any, str, Sequence[SkippedProperty], Sequence[R]], Any
]


def build_feedback_judge[R: RebuttalBase](
    *,
    ctx: WorkflowContext[Any],
    env: ServiceHost,
    apply_system: ApplySystem,
    apply_prompt: ApplyPrompt[R],
    input_parts: Callable[[str, Sequence[SkippedProperty], Sequence[R]], list[str | dict]],
    readback: BaseTool,
    description: str,
    thread_prefix: str,
    extra_tools: Sequence[BaseTool] = (),
) -> FeedbackThunk[R]:
    """Compile the judge sub-graph and return the thunk a feedback tool invokes.

    ``apply_prompt`` and ``input_parts`` are the two places a backend decides how the review is
    framed: whatever belongs in the prompt itself goes through the first, and whatever is better
    said as plain input text goes through the second. Both are called per review and both see the
    draft, the skips and the rebuttals, so a prompt that varies with the round can.

    ``readback`` is the backend's own read-back tool over ``curr_spec``, built with
    :func:`composer.authoring.buffer.get_spec_tool` against :class:`JudgeState` — it keeps the tool
    name the backend's prompts already refer to.
    """
    staged = bind_standard(
        env.builder_heavy().with_tools(env.all_tools),
        JudgeState,
        validator=_did_rough_draft_read,
    ).with_input(
        JudgeInput
    ).inject(
        apply_system
    ).with_tools(
        [*get_rough_draft_tools(JudgeState), ctx.get_memory_tool(), readback, *extra_tools]
    )

    async def judge(
        spec: str,
        skipped: Sequence[SkippedProperty],
        rebuttals: Sequence[R],
        within_tool: str,
    ) -> PropertyFeedbackProtocol:
        workflow: Any = staged.inject(
            lambda b: apply_prompt(b, spec, skipped, rebuttals)
        ).compile_async()
        res = await run_to_completion(
            workflow,
            JudgeInput(
                input=input_parts(spec, skipped, rebuttals),
                curr_spec=spec,
                memory=None,
                did_read=False,
            ),
            thread_id=uniq_thread_id(thread_prefix),
            recursion_limit=ctx.recursion_limit,
            description=description,
            within_tool=within_tool,
        )
        assert "result" in res
        return res["result"]

    return judge
