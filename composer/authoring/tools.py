"""Session tools that are the same for every backend.

The give-up exit and the skip declarations. Both are worth sharing less for their bodies than for
their *contracts*, which the session reads back: ``failed=True`` with the reason in ``result`` is
how a session reports that it produced nothing, and a skip is what excuses a property from the
publish-time mapping check. Backends agreeing on those by coincidence is one refactor away from two
of them disagreeing.

What varies on skip and give-up is LLM-facing text, so those are
:func:`~graphcore.tools.schemas.tool_family` classes; each backend supplies its own wording at
instantiate time. Unskip does not vary.
"""

from dataclasses import dataclass
from typing import Callable, Literal, Sequence, override

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import Field

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import (
    ToolFamilyParams, WithAsyncDependencies, WithImplementation, WithInjectedId,
    WithInjectedState, tool_family,
)

from composer.authoring.state import SkippedProperty
from composer.spec.types import PropertyTitle
from composer.ui.tool_display import suppress_ack, tool_display, tool_family_display


#: Supplies the batch's property titles when a skip tool runs. A thunk rather than the list itself
#: because a backend may only be able to reach them through the graph's runtime context.
type Titles = Callable[[], Sequence[PropertyTitle]]


def _as_titles(titles: Titles | Sequence[PropertyTitle]) -> Titles:
    if callable(titles):
        return titles
    snapshot = titles
    return lambda: snapshot


@dataclass(frozen=True)
class SkipScope:
    """What ``record_skip`` is allowed to retire: the batch's titles, and the subset it must
    refuse.

    ``protected`` is a thunk, not a set, because the answer changes during a session — the
    budget monitor's wrap-up order tells the agent to skip everything that does not work, so a
    protection that outlived that order would deadlock the session it was meant to curtail.
    A backend with nothing to protect passes a thunk returning ``()``."""
    titles: Titles
    protected: Titles


# ---------------------------------------------------------------------------
# Skip / unskip
# ---------------------------------------------------------------------------

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
class RecordSkip(WithInjectedId, WithAsyncDependencies[Command, SkipScope]):
    """{description}"""
    property_title: PropertyTitle = Field(
        description="The snake_case title of the property from the batch listing"
    )
    reason: str = Field(description="{reason}")

    @override
    async def run(self) -> Command:
        with self.tool_deps() as scope:
            known = scope.titles()
            protected = scope.protected()
        if self.property_title not in known:
            return tool_state_update(
                self.tool_call_id,
                f"Unknown property title {self.property_title!r}. Must be one "
                f"of: {', '.join(known)}.",
            )
        if self.property_title in protected:
            return tool_state_update(
                self.tool_call_id,
                f"Property {self.property_title!r} is the focus of this run and cannot be "
                "skipped. Decompose it into lemmas, strengthen the harness, or formalize a "
                "weaker core of it and say so in your commentary — but do not retire it.",
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
    """Remove a previously declared skip for a property. Use this if you later find a way to formalize a property you previously skipped."""
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
    protected: Titles | Sequence[PropertyTitle] = (),
) -> list[BaseTool]:
    """The skip / unskip pair, bound to the batch's property titles.

    ``protected`` names titles ``record_skip`` must refuse — the focus of a prioritized run,
    whose whole point is that it is pursued rather than retired. Unskip is deliberately not
    constrained: un-retiring a property is always allowed."""
    get = _as_titles(titles)
    scope = SkipScope(titles=get, protected=_as_titles(protected))
    return [
        RecordSkip.with_template(description=skip_description, reason=skip_reason)
        .bind(scope)
        .as_tool("record_skip"),
        Unskip.bind(get).as_tool("unskip_property"),
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
        description=description,
        reason=reason_description,
        label=label,
    ).as_tool(name)


# ---------------------------------------------------------------------------
# Give up, behind an escalation gate
# ---------------------------------------------------------------------------

def _gated_give_up_label(p: dict, *, description: str, reason: str, label: str) -> str:
    return f"Giving up on {label}: {p['reason']}"


def _verify_attempts(state: dict) -> int:
    """How many times this session has put a spec in front of the prover.

    Counted from the message history rather than from ``prover_history``: that list only grows
    when the prover returns a *report*, and the failures worth escalating through — a spec that
    will not type-check, a toolchain that will not run — return early without one. Counting
    calls counts attempts, which is what the gate is about. The same walk over ``messages`` is
    how the prover tool identifies its own prior calls."""
    return sum(
        1
        for msg in state.get("messages", [])
        if isinstance(msg, AIMessage)
        for call in msg.tool_calls
        if call["name"] == "verify_spec"
    )


@tool_family_display(_gated_give_up_label, None)
@tool_family(GiveUpParams)
class GatedGiveUp(
    WithInjectedId,
    WithInjectedState[dict],
    WithAsyncDependencies[Command, int],
):
    """{description}"""
    sort: Literal["environment", "exhausted"] = Field(
        description="Why you are stopping. 'environment' means the toolchain itself is unusable "
        "— the prover or compiler is missing or broken — and nothing you could write would "
        "succeed. 'exhausted' means you have tried to verify this and cannot find a way."
    )
    reason: str = Field(description="{reason}")
    attempts: list[str] = Field(
        min_length=1,
        description="One entry per distinct approach you actually tried, saying what you "
        "attempted and how it failed.",
    )

    @override
    async def run(self) -> Command:
        with self.tool_deps() as floor:
            if self.sort == "exhausted" and (ran := _verify_attempts(self.state)) < floor:
                return tool_state_update(
                    self.tool_call_id,
                    f"Rejected: you have run the prover {ran} time(s) on this task, and this run "
                    f"requires at least {floor} before a property may be abandoned. This is the "
                    "one property the run exists to establish. Decompose it into lemmas, add or "
                    "strengthen a harness, or formalize a weaker core of it and state the gap — "
                    "then verify. If the toolchain itself is broken, stop with "
                    "sort='environment' instead.",
                )
        return tool_state_update(
            self.tool_call_id,
            "Accepted",
            failed=True,
            result=self.reason,
        )


def gated_give_up_tool(
    *,
    name: str,
    description: str,
    label: str,
    min_attempts: int,
    reason_description: str = "The reason for giving up on your task",
) -> BaseTool:
    """:func:`give_up_tool` with a floor under it: an ``exhausted`` surrender is refused until
    the session has actually put a spec in front of the prover ``min_attempts`` times, and the
    reason must enumerate what was tried.

    For a run that has staked itself on one property, where an early surrender is the whole run.
    The ``environment`` escape is not optional: a missing prover or compiler produces no prover
    run at all, so a bare attempt floor would be unsatisfiable and would hold the agent against
    its recursion limit instead of letting it report a broken toolchain."""
    return GatedGiveUp.with_template(
        description=description,
        reason=reason_description,
        label=label,
    ).bind(min_attempts).as_tool(name)
