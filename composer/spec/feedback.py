
import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, NotRequired, Sequence
from typing_extensions import TypedDict
from composer.spec.service_host import Sort, ServiceHost

from graphcore.graph import Builder
from graphcore.tools.vfs import VFSState

from composer.authoring.judge import (
    JudgeInput, JudgeState, JudgeToolHost, ContextualFeedbackThunk,
    build_feedback_judge_generic, judge_host_of,
)
from composer.authoring.state import SkippedProperty
from composer.spec.context import (
    WorkflowContext, CVLJudge
)
from composer.spec.types import PropertyFormulation
from composer.cvl.tools import get_cvl
from composer.spec.gen_types import TemplateInstantiation, TypedTemplate, ITypedTemplate, PartialTemplate
from composer.spec.cvl_generation import FeedbackServices, Rebuttal
from composer.spec.source.live_explorer import VersionedHistory
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
    # True when the pipeline can modify the source under verification (the
    # editor tool is wired). Swaps the immutable-source skip guidance for the
    # "evaluate against the code as it stands" variant. Absent/False keeps the
    # immutable story, which remains true for pipelines without the editor.
    source_editing: NotRequired[bool]

FeedbackTemplate = PartialTemplate[FeedbackInherentParams, FeedbackInputs]("property_judge_prompt.j2")

class JudgeSystemParams(TypedDict):
    sort: Sort
    source_editing: NotRequired[bool]

# Judge system prompt, shared between the natspec and source-mode flows. The fs
# primitives are always documented; ``sort`` drives the rest (the template
# compiles out the code_explorer / code_document_ref guidance unless
# ``sort == "existing"``, the only mode that wires those tools).
# ``source_editing`` replaces the "No Source Changes" block with the "Source
# Changes" one: the code can differ between rounds, but feedback must stay
# actionable against the code as it stands.
FeedbackSystemTemplate = TypedTemplate[JudgeSystemParams]("property_judge_system_prompt.j2")


# Extra input parts prepended to every judge invocation. A bare list is static;
# a callable is evaluated per invocation (and may be async) so the producer can
# reflect state that changes between review rounds — e.g. a notice that the
# source under verification has changed since the judge last saw it.
type ExtraInputPrompt = (
    list[str | dict]
    | Callable[[], list[str | dict]]
    | Callable[[], Awaitable[list[str | dict]]]
    | None
)

type ContextualFeedbackToolImpl[Ctx] = ContextualFeedbackThunk[Rebuttal, Ctx]


def property_feedback_judge_generic[S: JudgeState, I: JudgeInput, Ctx](
    st: type[S],
    inp: type[I],
    ctx: WorkflowContext[CVLJudge],
    host: JudgeToolHost,
    prompt: ITypedTemplate[FeedbackInputs],
    props: list[PropertyFormulation],
    extra_inputs: ExtraInputPrompt,
    system_prompt: TemplateInstantiation | None,
    input_lift: Callable[[JudgeInput, Ctx], I],
    source_editing: bool = False,
) -> ContextualFeedbackToolImpl[Ctx]:
    """The CVL property judge, generic over its state/input pair and a per-invocation context.

    The batch's properties, skips and rebuttals are all rendered into the judge's *prompt* here
    (``property_judge_prompt.j2``); only the spec under review and the caller's ``extra_inputs``
    ride in as input text."""

    if system_prompt is None:
        system_prompt = FeedbackSystemTemplate.bind(
            {"sort": host.sort, "source_editing": source_editing}
        )
    bound_system = system_prompt

    def apply_prompt(
        builder: Builder[S, None, I], _cvl: str,
        skipped: Sequence[SkippedProperty], rebuttals: Sequence[Rebuttal],
    ) -> Builder[S, None, I]:
        return prompt.bind({
            "properties": props,
            "rebuttals": rebuttals,
            "skipped": skipped,
        }).render_to(builder.with_initial_prompt_template)

    async def input_parts(
        cvl: str, _skipped: Sequence[SkippedProperty], _rebuttals: Sequence[Rebuttal]
    ) -> list[str | dict]:
        parts: list[str | dict] = []
        if extra_inputs:
            if isinstance(extra_inputs, list):
                parts.extend(extra_inputs)
            else:
                produced = extra_inputs()
                if inspect.isawaitable(produced):
                    produced = await produced
                parts.extend(produced)
        parts.append("The proposed CVL file is")
        parts.append(cvl)
        return parts

    return build_feedback_judge_generic(
        st=st,
        inp=inp,
        ctx=ctx,
        host=host,
        apply_system=lambda b: bound_system.render_to(b.with_sys_prompt_template),
        apply_prompt=apply_prompt,
        input_parts=input_parts,
        readback=get_cvl(st),
        description="Property feedback judge",
        thread_prefix="feedback",
        input_lift=input_lift,
    )


def property_feedback_judge(
    ctx: WorkflowContext[CVLJudge],
    env: ServiceHost,
    prompt: ITypedTemplate[FeedbackInputs],
    props: list[PropertyFormulation],
    *,
    extra_inputs: ExtraInputPrompt = None,
    system_prompt: TemplateInstantiation | None = None,
) -> FeedbackServices:
    """The vanilla judge: the full ``ServiceHost`` tool surface (frozen FS
    reads), no source editing. Returns the services bundle the property-
    management tool suite binds against."""
    def lift(i: JudgeInput, _: None) -> JudgeInput:
        return i

    to_wrap = property_feedback_judge_generic(
        st=JudgeState,
        inp=JudgeInput,
        ctx=ctx,
        host=judge_host_of(env),
        extra_inputs=extra_inputs,
        prompt=prompt,
        props=props,
        system_prompt=system_prompt,
        input_lift=lift,
    )

    return FeedbackServices(
        feedback_thunk=lambda spec, skip, rebuttal, tid: to_wrap(None, spec, skip, rebuttal, tid),
        titles=[p.title for p in props]
    )


# ---------------------------------------------------------------------------
# Source-editing judge: the author's working copy rides into the judge's state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceSnapshot:
    """The author's working-copy view at judge-invocation time: the VFS
    overlay and the applied-edit history, seeded into the judge's own state so
    its vfs-aware FS tools (and the versioned explorer) read the edited
    source rather than the on-disk baseline."""
    vfs: dict[str, str]
    version_history: list[str]


class VfsJudgeState(JudgeState, VFSState, VersionedHistory):
    pass


class VfsJudgeInput(JudgeInput, VFSState, VersionedHistory):
    pass


def source_feedback_judge(
    ctx: WorkflowContext[CVLJudge],
    host: JudgeToolHost,
    prompt: ITypedTemplate[FeedbackInputs],
    props: list[PropertyFormulation],
    *,
    extra_inputs: ExtraInputPrompt = None,
    system_prompt: TemplateInstantiation | None = None,
) -> ContextualFeedbackToolImpl[SourceSnapshot]:
    """The editing-aware judge. ``host.judge_tools`` must be the vfs-aware
    read suite (they resolve paths through the state seeded from the
    :class:`SourceSnapshot`); the returned impl takes that snapshot as its
    leading argument on every invocation."""

    def lift(base: JudgeInput, snap: SourceSnapshot) -> VfsJudgeInput:
        return VfsJudgeInput(
            **base,
            vfs=snap.vfs,
            version_history=snap.version_history,
        )

    return property_feedback_judge_generic(
        st=VfsJudgeState,
        inp=VfsJudgeInput,
        ctx=ctx,
        host=host,
        extra_inputs=extra_inputs,
        prompt=prompt,
        props=props,
        system_prompt=system_prompt,
        input_lift=lift,
        source_editing=True,
    )
