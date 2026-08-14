"""The ``curr_spec`` buffer and the tools over it.

One buffer per session, replaced wholesale by a put or surgically by an edit, and read back by
whoever needs the current text (the agent itself, its judge, its checker). Every write goes through
:func:`apply_spec_update`, so a backend that can reject a malformed spec at write time does it in
exactly one place — and a rejected write leaves the buffer untouched.

The tools are factories rather than classes because the pieces that vary between backends are the
tool's *name* and *description* (both LLM-facing, so they can't be unified away) and the validator.
:mod:`composer.core.edit` holds the one string operation an edit is.
"""

from typing import Annotated, Callable, Literal, overload
from typing_extensions import TypedDict

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field, create_model

from graphcore.graph import tool_state_update

from composer.core.edit import EditErr, EditOk, replace_unique
from composer.ui.tool_display import ToolDisplay, tool_display_of


#: Validates a candidate spec at write time. ``None`` accepts it; a string rejects the write and is
#: returned to the agent verbatim, so it must say what is wrong. A backend with no cheap syntactic
#: check passes ``None`` instead of a validator and lets its gate tool be the only judge.
type SpecValidator = Callable[[str], str | None]

SPEC_KEY = "curr_spec"

#: The flag a judge's ``did_read`` gate reads. Writing the spec clears it: a review that read the
#: previous draft has not read this one.
READ_KEY = "did_read"


class SpecBuffer(TypedDict):
    curr_spec: str | None


class SpecBufferSet(TypedDict):
    """A buffer known to be written — a judge's state, which is handed the spec under review."""

    curr_spec: str


class SpecBufferWithRead(SpecBuffer):
    did_read: bool


def apply_spec_update(
    *,
    tool_call_id: str,
    text: str,
    validator: SpecValidator | None = None,
    spec_key: str = SPEC_KEY,
    reset_read: str | None = None,
) -> str | Command:
    """Write ``text`` into the buffer, or reject it.

    Returns the validator's complaint (a plain string, which the agent sees as the tool result and
    the buffer is unchanged) or the state update that installs it."""
    if validator is not None and (err := validator(text)) is not None:
        return err
    update: dict[str, object] = {spec_key: text}
    if reset_read:
        update[reset_read] = False
    return tool_state_update(tool_call_id=tool_call_id, content="Accepted", **update)


class _GetSpecTemplate(BaseModel):
    """
    Retrive the textual representation of the current specification.
    """


@overload
def get_spec_tool[S: SpecBufferWithRead](
    ty: type[S], *, name: str, description: str, missing: str, display: ToolDisplay,
    title: str = ..., set_did_read: Literal[True],
) -> BaseTool: ...


@overload
def get_spec_tool[S: SpecBufferSet](
    ty: type[S], *, name: str, description: str, missing: str, display: ToolDisplay,
    title: str = ...,
) -> BaseTool: ...


@overload
def get_spec_tool[S: SpecBuffer](
    ty: type[S], *, name: str, description: str, missing: str, display: ToolDisplay,
    title: str = ...,
) -> BaseTool: ...


def get_spec_tool(
    ty: type,
    *,
    name: str,
    description: str,
    missing: str,
    display: ToolDisplay,
    title: str = "GetSpec",
    set_did_read: bool = False,
) -> BaseTool:
    """Read-back tool over the buffer. ``missing`` is what the agent is told when nothing has been
    written yet.

    ``set_did_read`` additionally stamps :data:`READ_KEY`, which is how a judge's completion
    validator knows the review actually looked at the draft rather than at the copy in its prompt.
    """
    extra_fields: dict = {}
    if set_did_read:
        extra_fields["tool_call_id"] = (Annotated[str, InjectedToolCallId], ...)
    schema = create_model(
        title,
        __base__=_GetSpecTemplate,
        __doc__=description,
        state=(Annotated[ty, InjectedState], ...),
        **extra_fields,
    )

    @tool_display_of(display)
    @tool(name_or_callable=name, args_schema=schema)
    def get_spec(**args) -> str | Command:
        st = args["state"]
        if st[SPEC_KEY] is None:
            return missing
        spec = st[SPEC_KEY]
        if set_did_read:
            return tool_state_update(
                tool_call_id=args["tool_call_id"], content=spec, **{READ_KEY: True}
            )
        return spec

    return get_spec


class _EditSpecTemplate(BaseModel):
    old_string: str = Field(
        description="The exact span of the current spec to replace. Must occur exactly once; "
        "include surrounding context to disambiguate."
    )
    new_string: str = Field(description="The text to replace `old_string` with.")


def edit_spec_tool[S: SpecBuffer](
    ty: type[S],
    *,
    name: str,
    description: str,
    missing: str,
    display: ToolDisplay,
    title: str = "EditSpec",
    validator: SpecValidator | None = None,
    reset_read: str | None = READ_KEY,
) -> BaseTool:
    """Surgical single-occurrence replace over the buffer, re-validated exactly as a put is.

    A failed match leaves the buffer alone and returns the reason, which names what to do about it
    (add context, or re-read the buffer) — an edit that silently hit the wrong site would be far
    worse than one that is refused.

    Two calls of this tool in the same turn are refused: parallel edits race through the state
    reducer. A single edit alongside a different tool is allowed.
    """
    schema = create_model(
        title,
        __base__=_EditSpecTemplate,
        __doc__=description,
        state=(Annotated[ty, InjectedState], ...),
        tool_call_id=(Annotated[str, InjectedToolCallId], ...),
    )

    @tool_display_of(display)
    @tool(name_or_callable=name, args_schema=schema)
    def edit_spec(**args) -> str | Command:
        st = args["state"]
        if st[SPEC_KEY] is None:
            return missing
        messages = st.get("messages")
        if messages:
            last = messages[-1]
            if (
                isinstance(last, AIMessage)
                and len([t for t in last.tool_calls if t["name"] == name]) > 1
            ):
                return f"`{name}` tool cannot be called in parallel within the same turn."
        match replace_unique(st[SPEC_KEY], args["old_string"], args["new_string"]):
            case EditErr(message=msg):
                return msg
            case EditOk(text=new_text):
                return apply_spec_update(
                    tool_call_id=args["tool_call_id"],
                    text=new_text,
                    validator=validator,
                    reset_read=reset_read,
                )

    return edit_spec
