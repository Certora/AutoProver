"""Explicit thinking and rough draft tools for multi-step reasoning workflows.

Ported from composer/spec/cvl_generation.py and composer/spec/draft.py on
the jtoman/auto-prover branch.
"""

from typing import cast, overload, override
from typing_extensions import TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from langgraph.types import Command
from pydantic import Field
from composer.ui.tool_display import tool_display_of, CommonTools

from graphcore.tools.schemas import WithImplementation, WithInjectedId, WithInjectedState

class RoughDraftState(TypedDict):
    memory: str | None
    drafted: bool


class _RoughDraftWithMessages(RoughDraftState, MessagesState):
    """Bound for the reminder-injecting overload — the guard reads the
    messages channel and the HumanMessage rides on the same channel."""


@overload
def get_rough_draft_tools[ST: RoughDraftState](
    ty: type[ST],
) -> list[BaseTool]: ...


@overload
def get_rough_draft_tools[ST: _RoughDraftWithMessages](
    ty: type[ST],
    *,
    review_reminder: str,
) -> list[BaseTool]: ...


def get_rough_draft_tools[ST](
    ty: type[ST],
    *,
    review_reminder: str | None = None,
) -> list[BaseTool]:
    """Build the (write_rough_draft, read_rough_draft) tool pair.

    ``write_rough_draft`` stores the draft, echoes it back as the tool result, and
    stamps ``drafted``. The next model turn therefore sees the draft as a
    ToolMessage rather than only as its own previous tool-arg — that is the
    review. ``read_rough_draft`` is for a later re-read after other work.

    ``review_reminder`` is an optional prompt fragment surfaced as a
    ``<system-reminder>`` HumanMessage whenever the draft is delivered (on
    write, and again on an explicit read). Use it to re-state, at the moment of
    review, what specifically the agent should be checking — the failure modes
    the validator will reject, the shape requirements of the result, etc. This
    is the same head-of-recent-context lever the prover-violation reminder
    uses; appending the cue at delivery time defeats the long-context drift
    where the agent reviews its draft without remembering the original
    reviewing criteria.

    The reminder rides alongside the tool result as a separate HumanMessage,
    which means the delivering call MUST be the only tool call in its turn —
    otherwise the appended user-role content breaks the tool_use ↔
    tool_result pairing for any siblings. Both tools enforce this via a
    ``state["messages"][-1].tool_calls`` check, mirroring the parallel-prover
    guard in ``CertoraProverTool``. The overload bound requires ``ST`` to also
    satisfy ``MessagesState`` whenever a reminder is provided so that channel
    access is statically valid; the impl casts unsafely on that promise.
    """
    def _parallel_error(tool_call_id: str, tool_name: str, state: ST) -> str | None:
        if review_reminder is None:
            return None
        # Overloads guarantee ST extends MessagesState here.
        msg_channel = cast(MessagesState, state)["messages"]
        last = msg_channel[-1]
        if isinstance(last, AIMessage):
            tcs = last.tool_calls
            if any(tc["id"] == tool_call_id for tc in tcs) and len(tcs) > 1:
                return (
                    f"Error: {tool_name} must be the only tool call "
                    "in its turn. Re-issue this call alone."
                )
        return None

    def _echo(tool_call_id: str, draft: str) -> list:
        messages: list = [
            ToolMessage(tool_call_id=tool_call_id, content=draft),
        ]
        if review_reminder is not None:
            messages.append(HumanMessage(
                content=f"<system-reminder>\n{review_reminder}\n</system-reminder>",
                display_tag="rough_draft_review_reminder",
            ))
        return messages

    @tool_display_of(CommonTools.read_rough_draft)
    class GetMemory(WithInjectedState[ST], WithImplementation[Command | str], WithInjectedId):
        """
        Retrieve the rough draft of the feedback
        """
        @override
        def run(self) -> str | Command:
            if err := _parallel_error(self.tool_call_id, "read_rough_draft", self.state):
                return err
            mem_state = cast(RoughDraftState, self.state)
            mem = mem_state["memory"]
            if mem is None:
                return "Rough draft not yet written"
            return Command(update={
                "messages": _echo(self.tool_call_id, mem),
                "drafted": True,
            })

    @tool_display_of(CommonTools.write_rough_draft)
    class SetMemory(WithInjectedState[ST], WithImplementation[Command | str], WithInjectedId):
        """
        Write your rough draft for review. The draft is echoed back as this tool's result.
        """
        rough_draft: str = Field(description="The new rough draft of your feedback")

        @override
        def run(self) -> Command | str:
            if err := _parallel_error(self.tool_call_id, "write_rough_draft", self.state):
                return err
            return Command(update={
                "memory": self.rough_draft,
                "drafted": True,
                "messages": _echo(self.tool_call_id, self.rough_draft),
            })

    return [SetMemory.as_tool("write_rough_draft"), GetMemory.as_tool("read_rough_draft")]
