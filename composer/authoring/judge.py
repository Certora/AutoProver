"""The feedback judge an authoring session invokes on its own draft.

A judge is a sub-agent, not a scoring function: it gets the session's tool belt, a rough-draft
scratchpad, a memory namespace of its own, and a read-back of the spec under review, and it must
call ``result`` with a structured :class:`PropertyFeedback`.

Two properties are enforced rather than requested. It must read the draft back through the tool
(``did_read``) instead of reviewing the copy pasted into its prompt, and the author may file
:class:`RebuttalBase` entries against prior-round feedback, which are rendered into the judge's
input so a point already answered with evidence is answered rather than repeated.
"""

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, NotRequired, Protocol, Sequence

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from graphcore.graph import Builder, FlowInput

from composer.authoring.buffer import SpecBufferSet
from composer.authoring.state import SkippedProperty
from composer.spec.context import WorkflowContext
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.service_host import ServiceHost, Sort
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

#: A :type:`FeedbackThunk` whose every invocation additionally carries a caller-defined context —
#: the editing pipeline's snapshot of the author's working copy — lifted into the judge's input by
#: the ``input_lift`` the judge was built with.
type ContextualFeedbackThunk[R: RebuttalBase, Ctx] = Callable[
    [Ctx, str, Sequence[SkippedProperty], Sequence[R], str],
    Awaitable[PropertyFeedbackProtocol],
]


class JudgeToolHost(Protocol):
    """The judge's construction surface: a builder, the workflow ``sort``, and the tool suite the
    judge runs with. Callers vary the FS-read strategy through ``judge_tools`` — frozen fs tools
    over the project root, or vfs-aware tools reading the author's working copy — without the judge
    machinery knowing which. ``ServiceHost`` consumers adapt via :func:`judge_host_of`."""

    def builder_heavy(self) -> Builder[None, None, None]: ...

    @property
    def sort(self) -> Sort: ...

    @property
    def judge_tools(self) -> tuple[BaseTool, ...]: ...


@dataclass(frozen=True)
class _ServiceHostJudge:
    """The vanilla adapter: judge runs with the host's full tool surface."""
    env: ServiceHost

    def builder_heavy(self) -> Builder[None, None, None]:
        return self.env.builder_heavy()

    @property
    def sort(self) -> Sort:
        return self.env.sort

    @property
    def judge_tools(self) -> tuple[BaseTool, ...]:
        return self.env.all_tools


def judge_host_of(env: ServiceHost) -> JudgeToolHost:
    return _ServiceHostJudge(env)


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


#: The judge's builder as its prompt hooks see it: state and input are the judge's own, and no
#: context type is bound.
type JudgeBuilder = Builder[JudgeState, None, JudgeInput]

#: Applies a prompt to the judge's builder. A backend that renders templates binds its params and
#: calls ``with_*_prompt_template``; one whose prompts are plain strings (a Rust wheel's) calls
#: ``with_sys_prompt`` / ``with_initial_prompt``. Either way the judge itself holds no opinion about
#: where prompt text comes from.
type ApplySystem = Callable[[JudgeBuilder], JudgeBuilder]
type ApplyPrompt[R: RebuttalBase] = Callable[
    [JudgeBuilder, str, Sequence[SkippedProperty], Sequence[R]], JudgeBuilder
]


def build_feedback_judge_generic[R: RebuttalBase, S: JudgeState, I: JudgeInput, Ctx](
    *,
    st: type[S],
    inp: type[I],
    ctx: WorkflowContext[Any],
    host: JudgeToolHost,
    apply_system: Callable[[Builder[S, None, I]], Builder[S, None, I]],
    apply_prompt: Callable[
        [Builder[S, None, I], str, Sequence[SkippedProperty], Sequence[R]], Builder[S, None, I]
    ],
    input_parts: Callable[
        [str, Sequence[SkippedProperty], Sequence[R]],
        list[str | dict] | Awaitable[list[str | dict]],
    ],
    readback: BaseTool,
    description: str,
    thread_prefix: str,
    input_lift: Callable[[JudgeInput, Ctx], I],
    extra_tools: Sequence[BaseTool] = (),
) -> ContextualFeedbackThunk[R, Ctx]:
    """Compile the judge sub-graph and return the thunk a feedback tool invokes.

    ``apply_prompt`` and ``input_parts`` are the two places a backend decides how the review is
    framed: whatever belongs in the prompt itself goes through the first, and whatever is better
    said as plain input text goes through the second. Both are called per review and both see the
    draft, the skips and the rebuttals, so a prompt that varies with the round can (``input_parts``
    may be async for the same reason — input that reflects state changed between rounds).

    ``st``/``inp`` are the judge's state and input types — :class:`JudgeState`/:class:`JudgeInput`
    themselves, or a backend's extensions of them (the editing pipeline's vfs-aware pair). Whatever
    the extension adds is seeded per invocation by ``input_lift``, which receives the base input and
    the invocation's ``Ctx`` and produces the full ``inp``.

    ``readback`` is the backend's own read-back tool over ``curr_spec``, built with
    :func:`composer.authoring.buffer.get_spec_tool` against ``st`` — it keeps the tool
    name the backend's prompts already refer to.

    ``ctx`` must be the judge's own context — every backend derives a ``child`` with a ``"judge"``
    key rather than passing the author's. The memory tool is namespaced by the context, and a judge
    that shares the author's namespace reviews with the author's notes in hand.
    """
    staged = bind_standard(
        host.builder_heavy().with_tools(host.judge_tools),
        st,
        validator=_did_rough_draft_read,
    ).with_input(
        inp
    ).inject(
        apply_system
    ).with_tools(
        [*get_rough_draft_tools(st), ctx.get_memory_tool(), readback, *extra_tools]
    )

    async def judge(
        exec_ctx: Ctx,
        spec: str,
        skipped: Sequence[SkippedProperty],
        rebuttals: Sequence[R],
        within_tool: str,
    ) -> PropertyFeedbackProtocol:
        workflow = staged.inject(
            lambda b: apply_prompt(b, spec, skipped, rebuttals)
        ).compile_async()
        produced = input_parts(spec, skipped, rebuttals)
        parts = await produced if inspect.isawaitable(produced) else produced
        res = await run_to_completion(
            workflow,
            input_lift(
                JudgeInput(input=parts, curr_spec=spec, memory=None, did_read=False),
                exec_ctx,
            ),
            thread_id=uniq_thread_id(thread_prefix),
            recursion_limit=ctx.recursion_limit,
            description=description,
            within_tool=within_tool,
        )
        assert "result" in res
        return res["result"]

    return judge


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
    """:func:`build_feedback_judge_generic` for the common case: the host's full tool surface, the
    base judge state, and no per-invocation context."""
    def lift(i: JudgeInput, _: None) -> JudgeInput:
        return i

    judge = build_feedback_judge_generic(
        st=JudgeState,
        inp=JudgeInput,
        ctx=ctx,
        host=judge_host_of(env),
        apply_system=apply_system,
        apply_prompt=apply_prompt,
        input_parts=input_parts,
        readback=readback,
        description=description,
        thread_prefix=thread_prefix,
        input_lift=lift,
        extra_tools=extra_tools,
    )

    async def plain(
        spec: str,
        skipped: Sequence[SkippedProperty],
        rebuttals: Sequence[R],
        within_tool: str,
    ) -> PropertyFeedbackProtocol:
        return await judge(None, spec, skipped, rebuttals, within_tool)

    return plain
