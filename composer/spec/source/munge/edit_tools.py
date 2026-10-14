"""The author's side of source editing: apply an edit the editor committed, read back the edits
applied so far, and return to an earlier one.

Every backend whose author works against a VFS overlay of the project uses these. They read only
``version_history``; what applying or reverting an edit *does* is replace the author's ``vfs``
wholesale with the edit store's snapshot, so a backend's state must carry ``vfs`` without a merging
reducer.
"""

from dataclasses import dataclass
from typing import override

from pydantic import Field
from langchain_core.tools import BaseTool
from langgraph.types import Command

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import WithAsyncDependencies, WithInjectedId, WithInjectedState
from graphcore.tools.vfs import VFSAccessor, VFSState

from composer.spec.source.live_explorer import VersionedHistory, WIPE_HISTORY
from .edit_store import EditStore
from .tool_names import COMMIT_EDIT, EDIT_HISTORY_LOG, REVERT_TO_EDIT
from .vfs_diff import summarize_changes


class ApplyEditTool(WithAsyncDependencies[str | Command, EditStore], WithInjectedState[VersionedHistory], WithInjectedId):
    """
     Apply the edit staged by the edit agent to your working tree.
    """
    edit_id: str = Field(description="The unique edit ID produced by the editor you want to apply")

    @override
    async def run(self) -> str | Command:
        with self.tool_deps() as dep:
            if self.edit_id in self.state["version_history"]:
                return f"{self.edit_id} has already been applied; if you want to revert to that state, use the {REVERT_TO_EDIT} tool"
            new_state = await dep.read(self.edit_id)
            if new_state is None:
                return f"{self.edit_id} does not denote any known edit"
            return tool_state_update(
                tool_call_id=self.tool_call_id,
                content="Edit applied",
                vfs=new_state.vfs,
                version_history=[self.edit_id]
            )

@dataclass
class HistoryDeps:
    mat: VFSAccessor[VFSState]
    edit_store: EditStore

class EditHistoryLog(WithAsyncDependencies[str, HistoryDeps], WithInjectedState[VersionedHistory]):
    """
    Use this to view a list of the applied edit ids, and the changes to the source code made on
    each edit
    """

    @override
    async def run(self) -> str:
        hist = self.state["version_history"]
        if len(hist) == 0:
            return "No edits applied, working against clean project directory"
        fetched_states: list[dict[str, str]] = []
        history : list[tuple[str, str, str]] = []
        with self.tool_deps() as dep:
            for (i, edit_id) in enumerate(hist):
                edit_state = await dep.edit_store.read(edit_id)
                if edit_state is None:
                    return "Something has gone very wrong; your edit history has an orphan ID. This is an unrecoverable error; terminate your task immediately"
                if i == 0:
                    prev = {}
                else:
                    prev = fetched_states[i - 1]
                diff = summarize_changes(
                    {"vfs": edit_state.vfs}, dep.mat, prev
                )
                history.append((edit_id, edit_state.executive_summary, diff))
                fetched_states.append(edit_state.vfs)
        to_format = [
            f"""
--- Edit #{i} (ID: {t})

Summary: {summary}

Diff from {"prior edit" if i > 0 else "project directory"}:

{diff}
"""
            for (i,(t, summary, diff)) in enumerate(history)
        ]
        return "\n\n".join(to_format)

class RevertToEdit(WithAsyncDependencies[Command | str, EditStore], WithInjectedId, WithInjectedState[VersionedHistory]):
    """
    Call this tool to revert to a prior edit in your history, or (with a null
    edit id) to discard every applied edit and return to the unedited project
    directory.
    """
    edit_id: str | None = Field(description="An edit ID to revert to (it must appear in your history), or null to revert to the unedited project directory")

    async def run(self) -> str | Command:
        if self.edit_id is None:
            if not self.state["version_history"]:
                return "No edits are applied; the working copy is already the unedited project directory"
            return tool_state_update(
                self.tool_call_id,
                "Reverted to the unedited project directory",
                vfs={},
                version_history=[WIPE_HISTORY],
            )
        if self.edit_id not in self.state["version_history"]:
            return f"{self.edit_id} does not appear in your edit history, nothing to revert to"
        if self.state["version_history"][-1] == self.edit_id:
            return f"Already at edit id {self.edit_id}, nothing to do"
        with self.tool_deps() as dep:
            i = self.state["version_history"].index(self.edit_id)
            target = await dep.read(self.edit_id)
            if target is None:
                return f"{self.edit_id} is in your history but absent from the edit store; this is an unrecoverable error"
            return tool_state_update(
                self.tool_call_id,
                f"Reverted to id {self.edit_id}",
                vfs=target.vfs,
                version_history=[WIPE_HISTORY, *self.state["version_history"][:i+1]]
            )


def edit_history_tools(store: EditStore, mat: VFSAccessor[VFSState]) -> list[BaseTool]:
    """``commit_edit``, ``edit_history_log`` and ``revert_to_edit``, under their shared names."""
    return [
        ApplyEditTool.bind(store).as_tool(COMMIT_EDIT),
        EditHistoryLog.bind(HistoryDeps(mat, store)).as_tool(EDIT_HISTORY_LOG),
        RevertToEdit.bind(store).as_tool(REVERT_TO_EDIT),
    ]
