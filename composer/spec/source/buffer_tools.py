"""Agent-facing tools for authoring several named CVL spec buffers — the multi-buffer generalization
of the single ``curr_spec`` tools in :mod:`composer.authoring.buffer`.

Each tool reads and writes ``state["buffers"][name]`` (a :class:`NamedBuffer`) instead of the single
``curr_spec`` string, reusing the shared CVL validator (:func:`cvl_syntax_error`) and the
surgical-edit primitive (:func:`replace_unique`). Writes go through the buffers-map reducer, so an
edit to one buffer leaves the others untouched. Each factory takes the concrete state type so the
tool can inject it.
"""

from typing import Annotated

from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field, create_model
from typing_extensions import TypedDict

from graphcore.graph import tool_state_update

from composer.core.edit import EditErr, EditOk, replace_unique
from composer.cvl.tools import cvl_syntax_error
from composer.spec.source.spec_buffers import NamedBuffer
from composer.ui.tool_display import ToolDisplay, suppress_ack, tool_display_of


class WithBuffers(TypedDict):
    buffers: dict[str, NamedBuffer]


_put_display = ToolDisplay("Writing spec buffer", suppress_ack("Buffer write result"))
_get_display = ToolDisplay("Reading spec buffer", None)
_edit_display = ToolDisplay("Editing spec buffer", suppress_ack("Buffer edit result"))
_list_display = ToolDisplay("Listing spec buffers", None)
_delete_display = ToolDisplay("Deleting spec buffer", suppress_ack("Buffer delete result"))


put_buffer_description = """
Create or replace a whole CVL spec buffer, identified by `name`. A buffer is a self-contained spec:
its own rules, its `methods{}` block, and `import` statements pulling in shared buffers. The text is
run through the CVL parser; if it fails to parse the update is rejected and the buffer is unchanged.

Set `is_run_target` false for a shared buffer that only supplies imports (ghosts/invariants/models)
and runs no rules of its own. List the names it imports in `imports` so shared-buffer edits correctly
invalidate this one. Re-putting an existing buffer keeps its property->rule mapping.
"""


class _PutBufferTemplate(BaseModel):
    name: str = Field(description="Unique buffer name (also its on-disk spec stem).")
    cvl: str = Field(description="The buffer's full CVL text (rules, methods{}, imports).")
    imports: list[str] = Field(
        default_factory=list, description="Names of the buffers this one imports."
    )
    is_run_target: bool = Field(
        default=True, description="False for a shared, imported-only buffer that runs no rules."
    )


def put_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    schema = create_model(
        "PutBuffer", __base__=_PutBufferTemplate, __doc__=put_buffer_description,
        state=(Annotated[ty, InjectedState], ...),
        tool_call_id=(Annotated[str, InjectedToolCallId], ...),
    )

    @tool_display_of(_put_display)
    @tool(args_schema=schema)
    def put_buffer(**args) -> str | Command:
        if (err := cvl_syntax_error(args["cvl"])) is not None:
            return err
        existing = (args["state"].get("buffers") or {}).get(args["name"])
        buf = NamedBuffer(
            name=args["name"], cvl=args["cvl"], imports=tuple(args["imports"]),
            is_run_target=args["is_run_target"],
            property_rules=dict(existing.property_rules) if existing else {},
        )
        return tool_state_update(
            tool_call_id=args["tool_call_id"], content="Accepted", buffers={args["name"]: buf}
        )
    return put_buffer


edit_buffer_description = """
Make a surgical edit to one spec buffer instead of re-emitting it. Provide `name`, an exact
`old_string` span copied from that buffer (must occur exactly once — include context to disambiguate),
and `new_string`. The edited buffer is re-parsed; if it fails to parse the edit is rejected and the
buffer is unchanged. Dramatically cheaper than `put_buffer` for a small change.
"""


class _EditBufferTemplate(BaseModel):
    name: str = Field(description="The buffer to edit.")
    old_string: str = Field(description="Exact span to replace; must occur exactly once.")
    new_string: str = Field(description="Replacement text.")


def edit_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    schema = create_model(
        "EditBuffer", __base__=_EditBufferTemplate, __doc__=edit_buffer_description,
        state=(Annotated[ty, InjectedState], ...),
        tool_call_id=(Annotated[str, InjectedToolCallId], ...),
    )

    @tool_display_of(_edit_display)
    @tool(args_schema=schema)
    def edit_buffer(**args) -> str | Command:
        existing = (args["state"].get("buffers") or {}).get(args["name"])
        if existing is None:
            return f"No buffer named {args['name']!r}. Create it with put_buffer first."
        match replace_unique(existing.cvl, args["old_string"], args["new_string"]):
            case EditErr(message=msg):
                return msg
            case EditOk(text=new_text):
                if (err := cvl_syntax_error(new_text)) is not None:
                    return err
                buf = existing.model_copy(update={"cvl": new_text})
                return tool_state_update(
                    tool_call_id=args["tool_call_id"], content="Accepted",
                    buffers={args["name"]: buf},
                )
    return edit_buffer


def get_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    schema = create_model(
        "GetBuffer", __doc__="Read one spec buffer's current CVL text.",
        name=(str, Field(description="The buffer to read.")),
        state=(Annotated[ty, InjectedState], ...),
    )

    @tool_display_of(_get_display)
    @tool(args_schema=schema)
    def get_buffer(**args) -> str:
        buf = (args["state"].get("buffers") or {}).get(args["name"])
        return buf.cvl if buf is not None else f"No buffer named {args['name']!r}."
    return get_buffer


def list_buffers[S: WithBuffers](ty: type[S]) -> BaseTool:
    schema = create_model(
        "ListBuffers", __doc__="List the spec buffers: name, kind, imports, and rule count.",
        state=(Annotated[ty, InjectedState], ...),
    )

    @tool_display_of(_list_display)
    @tool(args_schema=schema)
    def list_buffers(**args) -> str:
        buffers: dict[str, NamedBuffer] = (args["state"].get("buffers") or {})
        if not buffers:
            return "No spec buffers yet."
        lines = []
        for name in sorted(buffers):
            b = buffers[name]
            kind = "run-target" if b.is_run_target else "shared"
            imp = f", imports {sorted(b.imports)}" if b.imports else ""
            lines.append(f"- {name} ({kind}, {len(b.owned_rules)} rules{imp})")
        return "\n".join(lines)
    return list_buffers


def delete_buffer[S: WithBuffers](ty: type[S]) -> BaseTool:
    schema = create_model(
        "DeleteBuffer",
        __doc__="Delete a spec buffer (e.g. after merging its rules into another).",
        name=(str, Field(description="The buffer to delete.")),
        state=(Annotated[ty, InjectedState], ...),
        tool_call_id=(Annotated[str, InjectedToolCallId], ...),
    )

    @tool_display_of(_delete_display)
    @tool(args_schema=schema)
    def delete_buffer(**args) -> str | Command:
        if args["name"] not in (args["state"].get("buffers") or {}):
            return f"No buffer named {args['name']!r}."
        return tool_state_update(
            tool_call_id=args["tool_call_id"], content="Deleted", buffers={args["name"]: None}
        )
    return delete_buffer
