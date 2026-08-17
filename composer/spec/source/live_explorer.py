from dataclasses import dataclass
from pathlib import PurePath
from typing_extensions import TypedDict
from typing import Annotated, Callable, Awaitable, cast, NotRequired

from pydantic import Field

from langchain_core.tools import BaseTool

from langgraph.store.base import BaseStore
from langgraph.graph import MessagesState

from graphcore.tools.vfs import VFSState, VFSInput, VFSAccessor, vfs_tools
from graphcore.tools.schemas import WithAsyncDependencies, WithInjectedId, WithInjectedState
from graphcore.graph import Builder
from graphcore.tools.results import result_tool_generator

from composer.spec.agent_index import AgentIndex, RetrieveDocumentTool
from composer.spec.source.versioned_index import VersionedAgentIndex, MigrationOracle
from composer.pipeline.ecosystem import Ecosystem
from composer.spec.code_explorer import (
    _ExploreCodeCommon,
    render_code_explorer_prompt,
)

from composer.spec.context import SourceCode, user_data_ns
from composer.spec.util import uniq_thread_id
from composer.spec.graph_builder import run_to_completion



WIPE_HISTORY = "__wipe__"
"""Sentinel for the ``version_history`` reducer: an update whose first element
is this constant *replaces* the history with the remaining elements instead of
appending. ``RevertToEdit`` uses it to truncate history back to a prior edit."""


def _merge_version_history(left: list[str], right: list[str]) -> list[str]:
    if right and right[0] == WIPE_HISTORY:
        return right[1:]
    return left + right


class VersionedHistory(TypedDict):
    version_history: Annotated[list[str], _merge_version_history]

class ExplorerInput(VersionedHistory, VFSState):
    ...


type _ExplorerRunner = Callable[[VFSInput, str], Awaitable[str]]

@dataclass
class VersionedExplorerDeps:
    ind: VersionedAgentIndex
    runner: _ExplorerRunner

class LiveCodeExplorerTool(
    WithAsyncDependencies[str, VersionedExplorerDeps],
    WithInjectedState[ExplorerInput],
    _ExploreCodeCommon,
    WithInjectedId
):
    __doc__ = _ExploreCodeCommon.__doc__

    async def run(self) -> str:
        with self.tool_deps() as deps:
            reference = await deps.ind.asearch_versioned(
                self.question, self.state["version_history"]
            )
            if isinstance(reference, dict):
                return AgentIndex.format_document(reference)
            
            search_context = VersionedAgentIndex.format_context(reference)
            flow_input : list[str | dict] = [self.question, *search_context]
            answer = await deps.runner(VFSInput(
                input = flow_input,
                vfs=self.state["vfs"]
            ), self.tool_call_id)
            key = await deps.ind.aput(self.question, answer, self.state["version_history"])
            if key is None:
                return answer
            return AgentIndex.format_document(answer, key)

class LiveDocumentRef(WithAsyncDependencies[str, VersionedAgentIndex], WithInjectedState[VersionedHistory]):
    __doc__ = cast(str, RetrieveDocumentTool.__doc__)

    ref: str = Field(description="The document reference key")

    async def run(self) -> str:
        with self.tool_deps() as dep:
            res = await dep.aget(self.ref, self.state["version_history"])
            if res is None:
                return f"Document with reference id {self.ref} was not found"
            doc = [
                f"**Question**: {res['question']}",
                "",
                "**Answer**:",
                res["answer"]
            ]
            if res["caveat"] is not None:
                doc.append("The above answer may no longer be entirely accurate due to" \
                f"code changes made since its creation. The following caveats apply: {res['caveat']}")
            return "\n".join(doc)

@dataclass
class LiveEditTools:
    """The vfs-aware tool suite for the editing pipeline.

    ``read_tools`` are the raw primitives (get/list/grep over the working
    copy): safe for any consumer whose state carries a ``vfs``. ``explorer``
    and ``doc_tool`` additionally require ``version_history`` in the consumer's
    state (they key the finding cache by version), so they are separate slots —
    a consumer reviewing an *uncommitted* draft (the munge feedback judge) must
    take the primitives only, both because it lacks the history and because
    draft-derived answers must not enter the version-keyed cache."""
    read_tools: tuple[BaseTool, ...]
    write_tools: tuple[BaseTool, ...]
    explorer: BaseTool
    doc_tool: BaseTool
    mat: VFSAccessor[VFSState]

class _LiveExplorerState(MessagesState, VFSState):
    result: NotRequired[str]

def _prover_output_dirs(p: PurePath) -> bool:
    """Globally exclude prover outputs from the live tool surface AND the
    materializer: they are never compilation inputs, and copying prior runs'
    ``.certora_internal`` / ``emv-*`` trees into each prover-run
    materialization would be enormously wasteful."""
    return bool(p.parts) and (
        p.parts[0] == ".certora_internal" or p.parts[0].startswith("emv-")
    )


def setup_live_edits(
    builder: Builder[None, None, None],
    sc: SourceCode,
    base_store: AgentIndex,
    store: BaseStore,
    source_key: str,
    oracle: MigrationOracle,
    recursion_limit: int,
    ecosystem: Ecosystem,
) -> LiveEditTools:
    x = VersionedAgentIndex(
        _wrapped=base_store,
        _store=store,
        _target_ns=user_data_ns() + ("versioned_store", source_key),
        _migration_oracle=oracle
    )
    read_tools, mat = vfs_tools({
        "forbidden_read": sc.forbidden_read,
        "fs_layer": sc.project_root,
        "immutable": True,
        "global_exclude": _prover_output_dirs,
    }, VFSState)

    write_tools, _ = vfs_tools({
        "forbidden_read": sc.forbidden_read,
        # Specs are the author's domain; confs are reached through config_edit —
        # a conf edit can never pass the compile gate (it is never parsed by solc).
        'forbidden_write': r'^.+\.(spec|conf)$',
        "immutable": False,
        "fs_layer": sc.project_root,
        "global_exclude": _prover_output_dirs,
    }, VFSState)

    d = (
        builder
        .with_input(VFSInput)
        .with_state(_LiveExplorerState)
        .with_tools(read_tools)
        .with_tools([
            result_tool_generator(
                "result",
                (str, "Your answer to the posed question"),
                "Call this tool to deliver your answer"
            )
        ])
        .with_initial_prompt("Answer the following question")
        .with_sys_prompt(render_code_explorer_prompt(ecosystem.code_explorer_prompt, "versioned"))
        .with_output_key("result")
        .compile_async()
    )
    async def runner(
        inp: VFSInput, tool_call_id: str
    ) -> str:
        res = await run_to_completion(
            graph=d,
            input=inp, 
            context=None,
            description="Code Explorer",
            recursion_limit=recursion_limit,
            thread_id=uniq_thread_id("code-explorer"),
            within_tool=tool_call_id
        )
        assert "result" in res
        return res["result"]
    
    explorer = LiveCodeExplorerTool.bind(VersionedExplorerDeps(
        ind=x,
        runner=runner
    )).as_tool("code_explorer")

    doc_retriever = LiveDocumentRef.bind(x).as_tool("code_document_ref")

    return LiveEditTools(
        doc_tool=doc_retriever,
        mat=mat,
        read_tools=tuple(read_tools),
        explorer=explorer,
        write_tools=tuple(write_tools)
    )
