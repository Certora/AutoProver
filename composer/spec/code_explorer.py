"""
Reusable code exploration sub-agent tool.

Creates a BaseTool that delegates focused source code questions to a
sub-agent with file system tools (list_files, get_file, grep_files).
"""

from typing import Literal, NotRequired, override, Protocol, Any, TypedDict

from pydantic import Field, BaseModel

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from graphcore.graph import FlowInput, MessagesState
from graphcore.tools.schemas import WithAsyncImplementation, WithInjectedId

from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.tool_env import BaseSourceTools, BasicAgentTools
from composer.spec.util import uniq_thread_id
from composer.spec.agent_index import AgentIndex, IndexedTool
from composer.templates.loader import load_jinja_template
from composer.ui.tool_display import tool_display_of, CommonTools


type PriorFindingsMode = Literal["none", "established", "versioned"]


class CodeExplorerPromptParams(TypedDict):
    """Kwargs for an ecosystem's code-explorer system prompt.

    ``prior_findings`` selects which index protocol is appended: none (a
    fresh explorer), established facts from the frozen index, or the
    versioned/possibly-stale protocol used by the live editor.
    """

    prior_findings: PriorFindingsMode


def render_code_explorer_prompt(
    template: TypedTemplate[CodeExplorerPromptParams],
    prior_findings: PriorFindingsMode,
) -> str:
    return template.bind({"prior_findings": prior_findings}).render_to(load_jinja_template)

class _ExplorerST(MessagesState):
    result: NotRequired[str]

class CodeExplorerEnv(BaseSourceTools, BasicAgentTools, Protocol):
    pass

def _code_explorer_graph(
    env: CodeExplorerEnv,
    sys_prompt: str,
) -> CompiledStateGraph[_ExplorerST, None, FlowInput, Any]:
    return bind_standard(
        env.builder, _ExplorerST, "Your findings about the source code"
    ).with_input(
        FlowInput
    ).with_tools(
        env.base_source_tools
    ).with_sys_prompt(
        sys_prompt
    ).with_initial_prompt(
        "Answer the following question about the source code"
    ).compile_async()

class _ExploreCodeCommon(BaseModel):
    """
    Delegate a focused question about the source code to a code exploration sub-agent.
    The sub-agent has its own conversation thread with file tools (list_files, get_file,
    grep_files) and will return a synthesized answer. Use this instead of reading files
    directly when you need to understand a specific aspect of the codebase.

    Each invocation is independent — sub-agents do not share memory or context with
    each other. When you have several questions to ask, issue them as parallel tool
    calls in a single response rather than asking one, waiting for the answer, and
    then asking the next; the calls run concurrently and the overall wall-clock cost
    is roughly the slowest single answer instead of the sum.
    """
    question: str = Field(
        description="""
A specific, focused question about the source code. Do not ask questions about:
* The current task you're working on
* How to use other tools
* Questions about CVL or the prover
* Protocol related questions unrelated to the source code (e.g. expected deployment params, contract address seeds)
* Questions about the source language itself

Good: 'What state variables does withdraw() modify and how?'
Bad: 'Tell me about the contract'
Bad: 'What is the definition of function X?' (read the source directly)
Bad: 'Is it realistic to expect deposits > 2^128?'
"""
    )


def code_explorer_tool(
    env: CodeExplorerEnv,
    recursion_limit: int,
    explorer_prompt: TypedTemplate[CodeExplorerPromptParams],
) -> BaseTool:
    """Create a code exploration sub-agent tool from a pre-configured builder.

    Args:
        env: Code explorer env with builder and tools bound.
        recursion_limit: LangGraph recursion limit for each sub-agent run.
        explorer_prompt: Ecosystem-specific explorer system prompt.

    Returns:
        A BaseTool named ``explore_code``.
    """
    graph = _code_explorer_graph(
        env, sys_prompt=render_code_explorer_prompt(explorer_prompt, "none")
    )

    @tool_display_of(CommonTools.code_explorer)
    class ExploreCodeSchema(_ExploreCodeCommon, WithAsyncImplementation[str], WithInjectedId):
        __doc__ = _ExploreCodeCommon.__doc__

        @override
        async def run(self) -> str:
            st = await run_to_completion(
                graph=graph,
                context=None,
                description=f"Code Explorer: {self.question}",
                input=FlowInput(
                    input=[self.question]
                ),
                recursion_limit=recursion_limit,
                thread_id=uniq_thread_id("code_explorer"),
                within_tool=self.tool_call_id,
            )
            assert "result" in st
            return st["result"]

    return ExploreCodeSchema.as_tool("explore_code")

class ExtCodeExplorerEnv(CodeExplorerEnv, Protocol):
    @property
    def index(self) -> AgentIndex:
        ...

def indexed_code_explorer_tool(
    env: ExtCodeExplorerEnv,
    recursion_limit: int,
    explorer_prompt: TypedTemplate[CodeExplorerPromptParams],
) -> BaseTool:
    builder_graph = _code_explorer_graph(
        env, sys_prompt=render_code_explorer_prompt(explorer_prompt, "established")
    )

    @tool_display_of(CommonTools.code_explorer)
    class CodeExplorerTool(_ExploreCodeCommon, IndexedTool[AgentIndex], WithInjectedId):
        __doc__ = _ExploreCodeCommon.__doc__

        @override
        def get_question(self) -> str:
            return self.question

        @override
        async def answer_question(self, context: list[str]) -> str:
            res = await run_to_completion(
                graph=builder_graph,
                context=None,
                description=f"Code Explorer: {self.question}",
                thread_id=uniq_thread_id("code_explorer"),
                recursion_limit=recursion_limit,
                input=FlowInput(input=[
                    self.question,
                    *context
                ]),
                within_tool=self.tool_call_id,
            )
            assert "result" in res
            return res["result"]

    return CodeExplorerTool.bind(env.index).as_tool("code_explorer")
