from dataclasses import dataclass
from typing import NotRequired, Any, Callable, Awaitable, override, Protocol, Iterable
from typing_extensions import TypedDict
from pydantic import BaseModel, Field


from langgraph.graph import MessagesState

from langchain_core.tools import BaseTool

from .edit_store import EditStore

from .vfs_diff import summarize_changes

from graphcore.tools.vfs import VFSState, VFSAccessor, VFSInput
from graphcore.tools.schemas import WithAsyncDependencies, WithInjectedState, WithInjectedId
from composer.spec.context import WorkflowContext, CVLGeneration, CacheKey, EditorAgent, EditorJudge
from composer.spec.service_host import ServiceHost
from composer.spec.graph_builder import run_to_completion
from composer.spec.util import uniq_thread_id

class MungerStateExtra(VFSState):
    did_read: bool
    memory: str | None
    orig_vfs: dict[str, str]
    compile_conf: dict

class MungeDescription(
    BaseModel
):
    """A holistic description of your changes"""

    executive_summary: str = Field(description="An executive summary of your changes, fully covering each part of the diff with the existing source code")

    how_to_apply: str | None = Field(description="What changes might need to be made by the upstream verification author to make effective use of your changes")

    why_sound: str = Field(description="A precise, reasoned argument why your changes are *either* sound, OR an acceptable over-approximation")

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
                orig_vfs=self.state["vfs"].copy(),
                did_read=False,
                memory=None,
                compile_conf=self.state["config"],
                vfs=self.state["vfs"].copy()
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
    def mat(self) -> VFSAccessor[VFSState]: ...

EDITOR_KEY = CacheKey[CVLGeneration, EditorAgent]("editor")
JUDGE_KEY = CacheKey[EditorAgent, EditorAgent]("editor")

def editor_tool(
    ctx: WorkflowContext[CVLGeneration],
    edit_store: EditStore,
    edit_tools: EditToolsHost,
    env: ServiceHost
) -> BaseTool:
    editor_ctx = ctx.child(EDITOR_KEY)
    b = (
        env
        .builder_heavy()
        .with_input(MungerAgentInput)
        .with_state(MungerAgentState)
        .with_output_key("result")
        .with_tools(edit_tools.write_tools)
        .with_tools([editor_ctx.get_memory_tool()])
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