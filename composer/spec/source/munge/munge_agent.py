from dataclasses import dataclass
from typing import NotRequired, Any, Callable, Awaitable, override, Protocol, Iterable
from typing_extensions import TypedDict
from pydantic import BaseModel, Field


from langgraph.graph import MessagesState
from langgraph.types import Command

from langchain_core.tools import BaseTool
from langchain_core.messages import ToolMessage

from .edit_store import EditStore
from .vfs_diff import summarize_changes
from .compile_check import check_edits_compile, BuildFailed, EditsNotCompiled

from graphcore.graph import FlowInput
from graphcore.tools.vfs import VFSState, VFSAccessor, VFSInput
from graphcore.tools.schemas import (
    WithAsyncDependencies, WithImplementation, WithInjectedState, WithInjectedId,
)
from composer.spec.context import WorkflowContext, CVLGeneration, CacheKey, EditorAgent, EditorJudge
from composer.spec.service_host import ServiceHost
from composer.spec.graph_builder import run_to_completion, bind_standard
from composer.spec.util import uniq_thread_id
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools

class MungerStateExtra(VFSState):
    did_read: bool
    memory: str | None
    orig_vfs: dict[str, str]
    compile_conf: dict
    # The author's problem statement, piped through to the reviewer so it can
    # judge whether the edits stay on script.
    request: str
    # Hash of the VFS the reviewer approved. submit_edit only fires when this
    # matches the current VFS hash, so any edit after approval silently voids it —
    # the editor can't get a review and then sneak in further changes.
    reviewed_digest: str | None

class CommonMungeDescription(
    BaseModel
):
    """A holistic description of your changes"""
    executive_summary: str = Field(description="An executive summary of your changes, fully covering each part of the diff with the existing source code")

    how_to_apply: str | None = Field(description="What changes might need to be made by the upstream verification author to make effective use of your changes")

    why_sound: str = Field(description="A precise, reasoned argument why your changes are *either* sound, OR an acceptable over-approximation")

class MungeDescription(
    CommonMungeDescription
):
    """A holistic description of your changes & any extra entry points you added"""
    added_files: list[str] = Field(description="A manifest of files you added that should be added to the compilation steps. Empty if no files were added")

class MungeRefusal(BaseModel):
    """
    A structured description of why you are refusing to make the requested edits
    """
    explanation: str = Field(description="A concise description of why you are refusing to make the edit or why such an edit is not possible.")

class MungerAgentInput(MungerStateExtra, VFSInput):
    ...

class MungerAgentState(MungerStateExtra, MessagesState):
    result: NotRequired[MungeDescription | MungeRefusal]

@dataclass
class MungeToolDeps:
    graph_runner: Callable[[MungerAgentInput, str], Awaitable[MungerAgentState]]
    edit_store: EditStore
    accessor: VFSAccessor[VFSState]

class MungeStateDeps(TypedDict):
    config: dict
    vfs: dict[str, str]

class EditMungeTool(WithAsyncDependencies[str, MungeToolDeps], WithInjectedId, WithInjectedState[MungeStateDeps]):
    """
    Call this tool to request for a dedicated agent to make small,
    targeted changes to the source code under verification. You should invoke this
    tool only after exhausting other plausible strategies.

    You are *not* responsible for telling the agent what edits to make.
    Rather you must describe the problem you are hoping to solve by edits made
    by the agent.

    Good request: "The inline assembly access in `readStoreData()` is crashing the prover. Can you rewrite it to use standard Solidity?"
    Good request: "The iterative computation of sqrt inlined in `computePriceCurve()` needs to be summarized to avoid a timeout, can you refactor it into 
    a standalone function"
    Bad request: "Add the following line to the beginning of `transferAdmin()`: `require(msg.sender == address(this))`"
    Bad request: "Delete the body of `compoundInterest()`, it's too difficult for the prover."
    """
    request: str = Field(description="A short, concise, natural language request for an edit; it must include the problem" \
    "you're trying to solve, and the intended 'shape' of the solution.")

    @override
    async def run(self) -> str:
        with self.tool_deps() as deps:
            agent_input = MungerAgentInput(
                input=[self.request],
                request=self.request,
                orig_vfs=self.state["vfs"].copy(),
                did_read=False,
                memory=None,
                compile_conf=self.state["config"],
                vfs=self.state["vfs"].copy(),
                reviewed_digest=None,
            )
            res = await deps.graph_runner(
                agent_input, self.tool_call_id
            )
            assert "result" in res
            d = res["result"]
            if isinstance(d, MungeRefusal):
                return f"The editor refused your request with the following reason:\n\n{d.explanation}"
            
            application_key = await deps.edit_store.commit(res["vfs"])
            diff = summarize_changes(
                res, deps.accessor, self.state["vfs"]
            )
            result_msg = f"""
The editor finished responding to your request.

**Executive Summary**:
{d.executive_summary}

**Soundness Argument**:
{d.why_sound}

**Integration notes**:
{"(None provided)" if not d.how_to_apply else d.how_to_apply}

You can apply this edit to your working source by calling `commit_edit({application_key})`

-----

The diff of the edit is as follows:

{diff}
"""
            return result_msg

class EditToolsHost(
    Protocol
):
    @property
    def write_tools(self) -> Iterable[BaseTool]: ...

    @property
    def read_tools(self) -> Iterable[BaseTool]: ...

    @property
    def mat(self) -> VFSAccessor[VFSState]: ...

EDITOR_KEY = CacheKey[CVLGeneration, EditorAgent]("editor")
JUDGE_KEY = CacheKey[EditorAgent, EditorJudge]("judge")


# ---------------------------------------------------------------------------
# Feedback judge
# ---------------------------------------------------------------------------

class MungeFeedback(BaseModel):
    """The reviewer's verdict on the proposed edits."""
    good: bool = Field(description="Whether the edits are acceptable as-is, or need more work.")
    feedback: str = Field(description="Actionable feedback if work is needed; may be empty when the edits are good.")


class MungeFeedbackState(RoughDraftState, VFSState, MessagesState):
    result: NotRequired[MungeFeedback]


class MungeFeedbackInput(FlowInput, RoughDraftState, VFSState):
    pass


# (current vfs, review parts, calling tool_call_id) -> verdict
MungeFeedbackThunk = Callable[[dict[str, str], list[str | dict], str], Awaitable[MungeFeedback]]


def munge_feedback_judge(
    ctx: WorkflowContext[EditorAgent],
    env: ServiceHost,
    edit_tools: "EditToolsHost",
) -> MungeFeedbackThunk:
    """Build the editor's feedback judge. Its read-only VFS tools inspect the
    *editor's* current VFS: the caller seeds that VFS into the judge's own state
    at invocation time (see ``the_tool``), so the judge reviews the edited source
    rather than the on-disk baseline."""
    feedback_ctx = ctx.child(JUDGE_KEY)

    rough_draft_tools = get_rough_draft_tools(MungeFeedbackState)

    def did_rough_draft_read(s: MungeFeedbackState, _: MungeFeedback) -> str | None:
        if not s["did_read"]:
            return "Completion REJECTED: never read rough draft for review"
        return None

    workflow = bind_standard(
        env.builder_heavy(), MungeFeedbackState, validator=did_rough_draft_read
    ).with_input(
        MungeFeedbackInput
    ).with_sys_prompt_template(
        "munge_feedback_system.j2"
    ).with_initial_prompt_template(
        "munge_feedback_prompt.j2"
    ).with_tools(
        [*rough_draft_tools, feedback_ctx.get_memory_tool(), *edit_tools.read_tools]
    ).compile_async()

    async def the_tool(
        vfs: dict[str, str],
        review: list[str | dict],
        within_tool: str,
    ) -> MungeFeedback:
        res = await run_to_completion(
            workflow,
            MungeFeedbackInput(input=review, vfs=vfs, memory=None, did_read=False),
            thread_id=uniq_thread_id("munge-feedback"),
            recursion_limit=ctx.recursion_limit,
            description="Editor feedback judge",
            within_tool=within_tool,
        )
        assert "result" in res
        return res["result"]

    return the_tool


# ---------------------------------------------------------------------------
# Completion tools
# ---------------------------------------------------------------------------

class GiveUpTool(WithInjectedId, WithImplementation[Command]):
    """
    Abandon the edit request. Use this only when the requested change is
    impossible or cannot be made soundly — explain why in the caller's terms.
    """
    explanation: str = Field(description="Why the requested edit cannot or should not be made.")

    @override
    def run(self) -> Command:
        return Command(update={
            "result": MungeRefusal(explanation=self.explanation),
            "messages": [ToolMessage(tool_call_id=self.tool_call_id, content="Acknowledged.")],
        })


class ReviewStateSlice(VFSState):
    orig_vfs: dict[str, str]
    request: str


@dataclass
class ReviewDeps:
    accessor: VFSAccessor[VFSState]
    feedback: MungeFeedbackThunk


class RequestReviewTool(
    WithAsyncDependencies[Command, ReviewDeps],
    WithInjectedId,
    WithInjectedState[ReviewStateSlice],
):
    """
    Ask the reviewer to evaluate your current edits. The reviewer inspects the
    edited source and the diff and either approves it or hands back feedback to
    address. You must earn an approving review before you can submit — and the
    approval is tied to the exact edits you have now, so any further change voids
    it and you must request review again.
    """
    summary: CommonMungeDescription = Field(description="A holistic description of the changes you want reviewed.")

    def _review(self, diff: str) -> list[str | dict]:
        parts: list[str | dict] = [
            f"The author's original request to the editor:\n\n{self.state['request']}",
            "The editor proposes the following changes to the source under verification.",
            f"Executive summary:\n{self.summary.executive_summary}",
            f"Soundness argument:\n{self.summary.why_sound}",
        ]
        if self.summary.how_to_apply:
            parts.append(f"Integration notes:\n{self.summary.how_to_apply}")
        parts.append("The diff of the edit:")
        parts.append(diff)
        return parts

    @override
    async def run(self) -> Command:
        with self.tool_deps() as deps:
            current: VFSState = {"vfs": self.state["vfs"]}
            diff = summarize_changes(current, deps.accessor, self.state["orig_vfs"])
            verdict = await deps.feedback(self.state["vfs"], self._review(diff), self.tool_call_id)
            if verdict.good:
                # Stamp the hash of exactly what was approved; submit_edit checks it
                # against the live VFS, so a later edit invalidates the approval.
                digest = EditStore._deterministic_hash(self.state["vfs"])
                body = "The reviewer approved these edits. You may now submit_edit (do not change anything first)."
            else:
                digest = None
                body = f"The reviewer has feedback you must address:\n\n{verdict.feedback}"
            return Command(update={
                "reviewed_digest": digest,
                "messages": [ToolMessage(tool_call_id=self.tool_call_id, content=body)],
            })


class SubmitStateSlice(VFSState):
    compile_conf: dict
    reviewed_digest: str | None


@dataclass
class SubmitDeps:
    accessor: VFSAccessor[VFSState]


class SubmitEditTool(
    WithAsyncDependencies[Command | str, SubmitDeps],
    WithInjectedId,
    WithInjectedState[SubmitStateSlice],
):
    """
    Submit your finished edits. Accepted only if an approving request_review is
    still current for these exact edits AND they compile with every added/edited
    file reached by the build. On failure you get the reason back and should keep
    working; this tool does not end your turn on failure.
    """
    summary: MungeDescription = Field(description="A holistic description of your completed changes.")

    @override
    async def run(self) -> Command | str:
        if self.state["reviewed_digest"] != EditStore._deterministic_hash(self.state["vfs"]):
            return (
                "These edits have not been approved as they stand. Call request_review "
                "on your current edits first — any change since your last review voids "
                "the approval."
            )
        with self.tool_deps() as deps:
            current: VFSState = {"vfs": self.state["vfs"]}

            # The added files must join the build's file set for coverage to pass.
            conf = dict(self.state["compile_conf"])
            conf_files = list(conf.get("files", []))
            for f in self.summary.added_files:
                if f not in conf_files:
                    conf_files.append(f)
            conf["files"] = conf_files

            check = await check_edits_compile(
                current, deps.accessor, conf, self.summary.added_files
            )
            if isinstance(check, BuildFailed):
                return (
                    "Your edits do not build; fix them before submitting.\n\n"
                    f"{check.reason}"
                )
            if isinstance(check, EditsNotCompiled):
                return (
                    "The build succeeded but these edited files were never parsed by "
                    "the compiler, so your changes don't reach the verification. Wire "
                    f"them in (or list them in added_files): {sorted(check.files)}"
                )

            return Command(update={
                "result": self.summary,
                "messages": [ToolMessage(tool_call_id=self.tool_call_id, content="Edits accepted.")],
            })


def editor_tool(
    ctx: WorkflowContext[CVLGeneration],
    edit_store: EditStore,
    edit_tools: EditToolsHost,
    env: ServiceHost
) -> BaseTool:
    editor_ctx = ctx.child(EDITOR_KEY)

    feedback = munge_feedback_judge(editor_ctx, env, edit_tools)
    request_review = RequestReviewTool.bind(ReviewDeps(
        accessor=edit_tools.mat,
        feedback=feedback,
    )).as_tool("request_review")
    submit = SubmitEditTool.bind(SubmitDeps(
        accessor=edit_tools.mat,
    )).as_tool("submit_edit")
    give_up = GiveUpTool.as_tool("give_up")

    b = (
        env
        .builder_heavy()
        .with_input(MungerAgentInput)
        .with_state(MungerAgentState)
        .with_output_key("result")
        .with_sys_prompt_template("munge_editor_system.j2")
        .with_initial_prompt("Respond to the following edit request:")
        .with_tools(edit_tools.write_tools)
        .with_tools([editor_ctx.get_memory_tool(), request_review, submit, give_up])
        .compile_async()
    )

    async def runner(inp: MungerAgentInput, tid: str) -> MungerAgentState:
        return await run_to_completion(
            graph=b,
            context=None,
            description="Code Editor Agent",
            input=inp,
            recursion_limit=ctx.recursion_limit,
            within_tool=tid,
            thread_id=uniq_thread_id("code-editor")
        )
    
    return EditMungeTool.bind(MungeToolDeps(
        accessor=edit_tools.mat,
        edit_store=edit_store,
        graph_runner=runner
    )).as_tool("code_editor")