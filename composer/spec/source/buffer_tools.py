"""Agent-facing tools for authoring several named CVL spec buffers — the multi-buffer generalization
of the single ``curr_spec`` tools in :mod:`composer.authoring.buffer`.

Each tool reads and writes ``state["buffers"][name]`` (a :class:`NamedBuffer`) instead of the single
``curr_spec`` string, reusing the shared CVL validator (:func:`cvl_syntax_error`) and the
surgical-edit primitive (:func:`replace_unique`). Writes go through the buffers-map reducer, so an
edit to one buffer leaves the others untouched. Each tool is a pydantic model generic over the
concrete graph state; the factory subscribes it to that state and mints the tool.
"""

from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import Field
from typing_extensions import TypedDict

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import WithImplementation, WithInjectedId, WithInjectedState

from composer.core.edit import EditErr, EditOk, replace_unique
from composer.cvl.tools import cvl_syntax_error
from composer.spec.source.spec_buffers import NamedBuffer, buffer_imports, max_spec_buffers
from composer.ui.tool_display import ToolDisplay, suppress_ack, tool_display_of


class WithBuffers(TypedDict):
    buffers: dict[str, NamedBuffer]


_put_display = ToolDisplay("Writing spec buffer", suppress_ack("Buffer write result"))
_get_display = ToolDisplay("Reading spec buffer", None)
_edit_display = ToolDisplay("Editing spec buffer", suppress_ack("Buffer edit result"))
_list_display = ToolDisplay("Listing spec buffers", None)
_delete_display = ToolDisplay("Deleting spec buffer", suppress_ack("Buffer delete result"))


class PutBuffer[S: WithBuffers](WithImplementation[str | Command], WithInjectedState[S], WithInjectedId):
    """Create or replace a whole CVL spec buffer, identified by `name`. A buffer is a self-contained
    spec: its own rules, its `methods{}` block, and `import` statements pulling in shared buffers. The
    text is run through the CVL parser; if it fails to parse the update is rejected and the buffer is
    unchanged.

    Set `is_run_target` false for a shared buffer that only supplies imports (ghosts/invariants/models)
    and runs no rules of its own. To depend on a shared buffer, just `import "<name>.spec";` in this
    buffer's CVL — the dependency is read from those import statements, so editing a shared buffer
    correctly re-verifies exactly the buffers that import it. Re-putting an existing buffer keeps its
    property->rule mapping."""

    name: str = Field(description="Unique buffer name (also its on-disk spec stem).")
    cvl: str = Field(description="The buffer's full CVL text (rules, methods{}, imports).")
    property_rules: dict[str, list[str]] = Field(
        default_factory=dict,
        description="The properties this buffer verifies and, for each (by its snake_case title), the "
        "rule/invariant names in this buffer's CVL that verify it. Across all run-target buffers every "
        "non-skipped property must appear in exactly one buffer. Omit for a shared buffer.",
    )
    is_run_target: bool = Field(
        default=True, description="False for a shared, imported-only buffer that runs no rules."
    )

    def run(self) -> str | Command:
        if (err := cvl_syntax_error(self.cvl)) is not None:
            return err
        buffers_now = self.state.get("buffers") or {}
        # Cap how far the agent partitions: a new run-target buffer beyond the cap is refused.
        if self.is_run_target and self.name not in buffers_now:
            cap = max_spec_buffers()
            if sum(1 for b in buffers_now.values() if b.is_run_target) >= cap:
                return (
                    f"Refusing to create run-target buffer {self.name!r}: the run-target buffer cap "
                    f"({cap}) is already reached. Fold these properties into an existing run-target "
                    f"buffer instead."
                )
        existing = buffers_now.get(self.name)
        # Keep the prior property->rule mapping when the agent re-puts text without restating it.
        prop_rules = self.property_rules or (dict(existing.property_rules) if existing else {})
        buf = NamedBuffer(
            name=self.name, cvl=self.cvl,
            is_run_target=self.is_run_target, property_rules=prop_rules,
        )
        return tool_state_update(
            tool_call_id=self.tool_call_id, content="Accepted", buffers={self.name: buf}
        )


def put_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    return tool_display_of(_put_display)(PutBuffer[ty].as_tool("put_buffer"))


class EditBuffer[S: WithBuffers](WithImplementation[str | Command], WithInjectedState[S], WithInjectedId):
    """Make a surgical edit to one spec buffer instead of re-emitting it. Provide `name`, an exact
    `old_string` span copied from that buffer (must occur exactly once — include context to
    disambiguate), and `new_string`. The edited buffer is re-parsed; if it fails to parse the edit is
    rejected and the buffer is unchanged. Dramatically cheaper than `put_buffer` for a small change."""

    name: str = Field(description="The buffer to edit.")
    old_string: str = Field(description="Exact span to replace; must occur exactly once.")
    new_string: str = Field(description="Replacement text.")

    def run(self) -> str | Command:
        existing = (self.state.get("buffers") or {}).get(self.name)
        if existing is None:
            return f"No buffer named {self.name!r}. Create it with put_buffer first."
        match replace_unique(existing.cvl, self.old_string, self.new_string):
            case EditErr(message=msg):
                return msg
            case EditOk(text=new_text):
                if (err := cvl_syntax_error(new_text)) is not None:
                    return err
                buf = existing.model_copy(update={"cvl": new_text})
                return tool_state_update(
                    tool_call_id=self.tool_call_id, content="Accepted", buffers={self.name: buf},
                )


def edit_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    return tool_display_of(_edit_display)(EditBuffer[ty].as_tool("edit_buffer"))


class GetBuffer[S: WithBuffers](WithImplementation[str], WithInjectedState[S]):
    """Read one spec buffer's current CVL text."""

    name: str = Field(description="The buffer to read.")

    def run(self) -> str:
        buf = (self.state.get("buffers") or {}).get(self.name)
        return buf.cvl if buf is not None else f"No buffer named {self.name!r}."


def get_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    return tool_display_of(_get_display)(GetBuffer[ty].as_tool("get_buffer"))


class ListBuffers[S: WithBuffers](WithImplementation[str], WithInjectedState[S]):
    """List the spec buffers: name, kind, imports, and rule count."""

    def run(self) -> str:
        buffers: dict[str, NamedBuffer] = (self.state.get("buffers") or {})
        if not buffers:
            return "No spec buffers yet."
        lines = []
        for name in sorted(buffers):
            b = buffers[name]
            kind = "run-target" if b.is_run_target else "shared"
            imps = buffer_imports(buffers, name)
            imp = f", imports {sorted(imps)}" if imps else ""
            lines.append(f"- {name} ({kind}, {len(b.owned_rules)} rules{imp})")
        return "\n".join(lines)


def list_buffers[S: WithBuffers](ty: type[S]) -> BaseTool:
    return tool_display_of(_list_display)(ListBuffers[ty].as_tool("list_buffers"))


class DeleteBuffer[S: WithBuffers](WithImplementation[str | Command], WithInjectedState[S], WithInjectedId):
    """Delete a spec buffer (e.g. after merging its rules into another)."""

    name: str = Field(description="The buffer to delete.")

    def run(self) -> str | Command:
        if self.name not in (self.state.get("buffers") or {}):
            return f"No buffer named {self.name!r}."
        return tool_state_update(
            tool_call_id=self.tool_call_id, content="Deleted", buffers={self.name: None}
        )


def delete_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    return tool_display_of(_delete_display)(DeleteBuffer[ty].as_tool("delete_buffer"))
