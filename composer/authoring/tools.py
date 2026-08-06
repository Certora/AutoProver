"""Session tools that are the same for every backend.

The give-up exit and the skip declarations. Both are worth sharing less for their bodies than for
their *contracts*, which the session reads back: ``failed=True`` with the reason in ``result`` is
how a session reports that it produced nothing, and a skip is what excuses a property from the
publish-time mapping check. Backends agreeing on those by coincidence is one refactor away from two
of them disagreeing.

What varies is LLM-facing text — tool names, descriptions, the noun for a check — so it is
parameterized rather than unified away.
"""

from typing import Annotated, Callable, override

from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field, create_model

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import WithAsyncDependencies, WithInjectedId

from composer.authoring.state import SkippedProperty
from composer.ui.tool_display import ToolDisplay, suppress_ack, tool_display, tool_display_of


#: Supplies the batch's property titles when a skip tool runs. A thunk rather than the list itself
#: because a backend may only be able to reach them through the graph's runtime context.
type Titles = Callable[[], list[str]]


@tool_display(
    lambda p: f"Skipping property `{p.get('property_title', '?')}`",
    suppress_ack("Skip result", ("Recorded skip",)),
)
class RecordSkip(WithInjectedId, WithAsyncDependencies[Command, Titles]):
    """
    Declare that you are skipping a property from the batch.

    You must provide the property's title and a justification. Skipping
    excludes the property from the publish-time mapping check; only use
    after a genuine attempt to formalize.
    """
    property_title: str = Field(
        description="The snake_case title of the property from the batch listing"
    )
    reason: str = Field(
        description="Justification for why this property cannot be formalized"
    )

    @override
    async def run(self) -> Command:
        with self.tool_deps() as titles:
            known = titles()
        if self.property_title not in known:
            return tool_state_update(
                self.tool_call_id,
                f"Unknown property title {self.property_title!r}. Must be one "
                f"of: {', '.join(known)}.",
            )
        if not self.reason.strip():
            return tool_state_update(
                self.tool_call_id,
                "A non-empty justification is required when skipping a property.",
            )
        return tool_state_update(
            self.tool_call_id,
            f"Recorded skip for property {self.property_title}.",
            skipped=[SkippedProperty(property_title=self.property_title, reason=self.reason)],
        )


@tool_display(
    lambda p: f"Un-skipping property `{p.get('property_title', '?')}`",
    suppress_ack("Unskip result", ("Removed skip",)),
)
class Unskip(WithInjectedId, WithAsyncDependencies[Command, Titles]):
    """
    Remove a previously declared skip for a property. Use this if you later
    find a way to formalize a property you previously skipped.
    """
    property_title: str = Field(
        description="The snake_case title of the property to un-skip"
    )

    @override
    async def run(self) -> Command:
        with self.tool_deps() as titles:
            known = titles()
        if self.property_title not in known:
            return tool_state_update(
                self.tool_call_id,
                f"Unknown property title {self.property_title!r}. Must be one "
                f"of: {', '.join(known)}.",
            )
        # An empty reason is the sentinel ``merge_skips`` reads as "no longer skipped".
        return tool_state_update(
            self.tool_call_id,
            f"Removed skip for property {self.property_title}.",
            skipped=[SkippedProperty(property_title=self.property_title, reason="")],
        )


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
