from typing import Protocol, Callable, AsyncContextManager
from dataclasses import dataclass, field
from rich.console import RenderableType

from langchain_core.messages.tool import ToolCall

#: Opens a refinement conversation: given the opening render and the conversation's
#: checkpoint thread id, yields the client the loop talks to. A UI ignores the thread
#: id; a client relaying turns to a control plane records questions against it.
type ConversationContextProvider = Callable[[RenderableType, str], AsyncContextManager[ConversationClient]]


@dataclass
class ThinkingStart:
    pass


@dataclass
class ToolBatch:
    """A single AI turn's tool calls.

    One ``ToolBatch`` per LangGraph ``AIMessage`` that contained tool
    calls — grouping across batches is handled by the renderer's
    consecutive-group state and is reset on human turns.
    """
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolComplete:
    thread_id: str


@dataclass
class AIYapping:
    yap_content: str

@dataclass
class StateUpdate:
    state_display: RenderableType

type ProgressPayload = ToolComplete | ToolBatch | ThinkingStart | AIYapping | StateUpdate


@dataclass(frozen=True)
class HumanPrompt:
    """One turn's ask of the person. ``question_id`` is the id of the AI message
    they are replying to, the identity the reply is recorded and answered under;
    ``ai_message`` is its text when there is any to show, none on the opening
    turn."""

    question_id: str
    ai_message: str | None


class ConversationClient(Protocol):
    async def human_turn(self, prompt: HumanPrompt, state: RenderableType | None) -> str:
        """Put the turn to the person and return their reply. ``state`` is the
        current state of the thing under refinement, rendered, when the loop
        has a renderer for it: a console shows it on request (``/list``), a
        client relaying the turn elsewhere sends it along as context."""
        ...

    async def answer_applied(self, question_id: str) -> None:
        """The reply to ``question_id`` is in the conversation's durable state.
        A client that fetched the answer from outside the process retires it
        here; one that asked a person directly has nothing to do."""
        ...

    def progress_update(
        self, progress: ProgressPayload
    ):
        ...
