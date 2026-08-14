"""Session tools that are the same for every backend.

The give-up exit and the skip declarations. Both are worth sharing less for their bodies than for
their *contracts*, which the session reads back: ``failed=True`` with the reason in ``result`` is
how a session reports that it produced nothing, and a skip is what excuses a property from the
publish-time mapping check. Backends agreeing on those by coincidence is one refactor away from two
of them disagreeing.

What varies is LLM-facing text — descriptions, the reason field, the UI label — so each tool is a
:func:`~graphcore.tools.schemas.tool_family` instantiated with the backend's own wording. The
CVL and Foundry instantiations keep the text those tools had on master.
"""

import inspect
from typing import Callable, Sequence, override

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import Field

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import (
    ToolFamilyParams, WithAsyncDependencies, WithImplementation, WithInjectedId, tool_family,
)

from composer.authoring.state import SkippedProperty
from composer.spec.types import PropertyTitle
from composer.ui.tool_display import suppress_ack, tool_family_display


#: Supplies the batch's property titles when a skip tool runs. A thunk rather than the list itself
#: because a backend may only be able to reach them through the graph's runtime context.
type Titles = Callable[[], Sequence[PropertyTitle]]


def _as_titles(titles: Titles | Sequence[PropertyTitle]) -> Titles:
    if callable(titles):
        return titles
    snapshot = titles
    return lambda: snapshot


def _llm_doc(text: str) -> str:
    """Class ``__doc__`` is already ``cleandoc``'d; a ``{description}`` substitution is not."""
    return inspect.cleandoc(text)


# ---------------------------------------------------------------------------
# Skip / unskip
# ---------------------------------------------------------------------------

# Exact master wording. Do not "clean up" — the golden test pins these strings.

CVL_SKIP_DESCRIPTION = """
    Declare that you are skipping a property from the batch.
    You must provide the property's title and a justification.
    The feedback judge will evaluate whether your justification is valid.
    Only use this after genuinely attempting to formalize the property.
    """

FOUNDRY_SKIP_DESCRIPTION = """
    Declare that you are skipping a property from the batch.

    You must provide the property's title and a justification. Skipping
    excludes the property from the publish-time property→test mapping
    check; only use after a genuine attempt to formalize.
    """

RUST_SKIP_DESCRIPTION = """
    Declare that you are skipping a property from the batch.

    You must provide the property's title and a justification. Skipping
    excludes the property from the publish-time mapping check; only use
    after a genuine attempt to formalize.
    """

CVL_SKIP_REASON = "Justification for why this property cannot be formalized"
FOUNDRY_SKIP_REASON = "Justification for why this property cannot be formalized as a foundry test"

CVL_UNSKIP_DESCRIPTION = """
    Remove a previously declared skip for a property.
    Use this if you later find a way to formalize a property you previously skipped.
    """

FOUNDRY_UNSKIP_DESCRIPTION = """
    Remove a previously declared skip for a property. Use this if you later
    find a way to formalize a property you previously skipped.
    """

#: Rust used the shared class, whose wording matched Foundry's line breaks.
RUST_UNSKIP_DESCRIPTION = FOUNDRY_UNSKIP_DESCRIPTION


class SkipParams(ToolFamilyParams):
    description: str
    reason: str


def _skip_label(p: dict, *, description: str, reason: str) -> str:
    return f"Skipping property `{p.get('property_title', '?')}`"


def _skip_result(
    _name: str, msg: ToolMessage, *, description: str, reason: str,
) -> str | None:
    return suppress_ack("Skip result", ("Recorded skip",))(_name, msg)


@tool_family_display(_skip_label, _skip_result)
@tool_family(SkipParams)
class RecordSkip(WithInjectedId, WithAsyncDependencies[Command, Titles]):
    """{description}"""
    property_title: PropertyTitle = Field(
        description="The snake_case title of the property from the batch listing"
    )
    reason: str = Field(description="{reason}")

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


class UnskipParams(ToolFamilyParams):
    description: str


def _unskip_label(p: dict, *, description: str) -> str:
    return f"Un-skipping property `{p.get('property_title', '?')}`"


def _unskip_result(_name: str, msg: ToolMessage, *, description: str) -> str | None:
    return suppress_ack("Unskip result", ("Removed skip",))(_name, msg)


@tool_family_display(_unskip_label, _unskip_result)
@tool_family(UnskipParams)
class Unskip(WithInjectedId, WithAsyncDependencies[Command, Titles]):
    """{description}"""
    property_title: PropertyTitle = Field(
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
        return tool_state_update(
            self.tool_call_id,
            f"Removed skip for property {self.property_title}.",
            skipped=[SkippedProperty(property_title=self.property_title, reason="")],
        )


def skip_tools(
    titles: Titles | Sequence[PropertyTitle],
    *,
    skip_description: str,
    skip_reason: str,
    unskip_description: str,
) -> list[BaseTool]:
    """The skip / unskip pair, bound to the batch's property titles."""
    get = _as_titles(titles)
    return [
        RecordSkip.with_template(description=_llm_doc(skip_description), reason=skip_reason)
        .bind(get)
        .as_tool("record_skip"),
        Unskip.with_template(description=_llm_doc(unskip_description))
        .bind(get)
        .as_tool("unskip_property"),
    ]


# ---------------------------------------------------------------------------
# Give up
# ---------------------------------------------------------------------------

class GiveUpParams(ToolFamilyParams):
    description: str
    reason: str
    label: str


def _give_up_label(p: dict, *, description: str, reason: str, label: str) -> str:
    return f"Giving up on {label}: {p['reason']}"


@tool_family_display(_give_up_label, None)
@tool_family(GiveUpParams)
class GiveUp(WithImplementation[Command], WithInjectedId):
    """{description}"""
    reason: str = Field(description="{reason}")

    @override
    def run(self) -> Command:
        return tool_state_update(
            self.tool_call_id,
            "Accepted",
            failed=True,
            result=self.reason,
        )


def give_up_tool(
    *,
    name: str,
    description: str,
    label: str,
    reason_description: str = "The reason for giving up on your task",
) -> BaseTool:
    """The last-resort exit. ``label`` names the task in the UI line, which reads
    ``Giving up on {label}: {reason}``.

    A session that gives up is a real outcome, not an error: the reason is the agent's own account
    of why the task could not be completed, and it reaches the report."""
    return GiveUp.with_template(
        description=_llm_doc(description),
        reason=reason_description,
        label=label,
    ).as_tool(name)
