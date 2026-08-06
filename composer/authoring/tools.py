"""Session tools that are the same for every backend.

Only the give-up exit lives here so far. It is worth sharing not because the body is long but
because the *contract* is what the session reads back: ``failed=True`` with the reason in
``result`` is how a session reports that it produced nothing, and three backends agreeing on that by
coincidence is one refactor away from two of them disagreeing.
"""

from typing import Annotated

from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field, create_model

from graphcore.graph import tool_state_update

from composer.ui.tool_display import ToolDisplay, tool_display_of


class _GiveUpTemplate(BaseModel):
    pass


def give_up_tool(
    *,
    name: str,
    description: str,
    label: str,
    reason_description: str = "The reason for giving up on your task",
    title: str = "GiveUpTool",
) -> BaseTool:
    """The last-resort exit. ``label`` names the task in the UI line, which reads
    ``Giving up on {label}: {reason}``.

    A session that gives up is a real outcome, not an error: the reason is the agent's own account
    of why the task could not be completed, and it reaches the report."""
    schema = create_model(
        title,
        __base__=_GiveUpTemplate,
        __doc__=description,
        reason=(str, Field(description=reason_description)),
        tool_call_id=(Annotated[str, InjectedToolCallId], ...),
    )

    @tool_display_of(
        ToolDisplay(lambda p: f"Giving up on {label}: {p['reason']}", None)
    )
    @tool(name_or_callable=name, args_schema=schema)
    def give_up(**args) -> Command:
        return tool_state_update(
            args["tool_call_id"],
            "Accepted",
            failed=True,
            result=args["reason"],
        )

    return give_up
