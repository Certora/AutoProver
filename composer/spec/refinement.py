from dataclasses import dataclass
import enum

from typing import Callable, Literal, Never, cast


from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START
from langgraph.graph import MessagesState
from langgraph.types import Command
from abc import ABC, abstractmethod

from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolInvocationError
from langgraph.types import interrupt
from langgraph.checkpoint.base import BaseCheckpointSaver

from rich.console import RenderableType

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import WithAsyncImplementation, WithImplementation, WithInjectedId
from graphcore.utils import ainvoke

from langchain_core.messages import AnyMessage, BaseMessage, AIMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from composer.io.conversation import (
    ConversationClient, AIYapping, ToolComplete, ToolBatch, ThinkingStart,
    StateUpdate, HumanPrompt
)
from composer.io.mailbox import chat_reply, replies_in
from composer.io.protocol import IOHandler, RefuseInterrupts
from composer.io.event_handler import NullEventHandler
from composer.io.context import with_handler

@dataclass
class EndConversation:
    pass

class ConversationStateEnum(enum.Enum):
    CHAT = 1
    INIT = 2

class ConversationState[T](MessagesState):
    state: ConversationStateEnum
    extra_data: T

class _WithStateUpdate[T](WithInjectedId):
    def _update(
        self, new_data: T
    ) -> Command:
        return tool_state_update(
            tool_call_id=self.tool_call_id,
            content="Accepted",
            extra_data=new_data
        )
class AsyncStateUpdateTool[T](WithAsyncImplementation[Command], _WithStateUpdate[T], ABC):
    @abstractmethod
    async def run(self) -> Command:
        ...


class SyncStateUpdateTool[T](WithImplementation[Command], _WithStateUpdate[T], ABC):
    @abstractmethod
    def run(self) -> Command:
        ...

async def refinement_loop[T](
    llm: BaseChatModel,
    client: ConversationClient,
    init_data: T,
    init_messages: list[AnyMessage],
    tools: list[BaseTool],
    *,
    checkpointer: BaseCheckpointSaver,
    thread_id: str,
    state_renderer: Callable[[T], RenderableType] | None = None,
    diff_renderer: Callable[[T, T], RenderableType] | None = None
) -> ConversationState[T]:
    """Run the conversation on ``thread_id`` under ``checkpointer``. A thread
    that already has checkpoints is resumed at its latest one, at the turn it
    was waiting on, and ``init_data`` / ``init_messages`` go unused: a restarted
    process naming the same thread picks the conversation back up.

    Each reply is recorded as a message naming the AI message it answers (see
    ``composer.io.mailbox.chat_reply``); once the checkpoint carrying it is
    written the client hears ``answer_applied`` for that question, which is
    what lets a client that took the answer from outside the process retire it."""
    graph = StateGraph(
        state_schema=ConversationState,
        context_schema=None,
        input_schema=ConversationState,
        output_schema=None
    )
    bound_llm = llm.bind_tools(tools)
    init_state : ConversationState[T] = {
        "messages": init_messages,
        "extra_data": init_data,
        "state": ConversationStateEnum.INIT
    }

    async def llm_echo(state: ConversationState[T]) -> dict[str, list[BaseMessage]]:
        client.progress_update(ThinkingStart())
        res = await ainvoke(bound_llm, state["messages"])
        assert isinstance(res, AIMessage)
        if len(res.tool_calls):
            if len(res.text) > 0:
                client.progress_update(AIYapping(res.text))
            client.progress_update(ToolBatch(calls=list(res.tool_calls)))
        return {
            "messages": [res]
        }

    tool_node = ToolNode(tools, handle_tool_errors=(ToolInvocationError,))

    async def chat_node(
        state: ConversationState[T]
    ) -> dict[str, list[BaseMessage] | ConversationStateEnum]:
        # The person replies to the last message, whose id is the question's:
        # the reply names it, and an answer arriving from outside is keyed by it.
        msg = state["messages"][-1]
        assert msg.id is not None, "a message the person replies to must have been checkpointed with an id"
        payload_text = None
        if state["state"] != ConversationStateEnum.INIT and isinstance(msg, AIMessage):
            payload_text = msg.text

        prompt = HumanPrompt(question_id=msg.id, ai_message=payload_text)
        res = interrupt(prompt)
        assert isinstance(res, str)
        return {
            "messages": [chat_reply(res, prompt.question_id)],
            "state": ConversationStateEnum.CHAT
        }
    
    graph.add_node("chat_node", chat_node)
    graph.add_edge(START, "chat_node")
    
    graph.add_node("llm_echo", llm_echo)
    graph.add_edge("chat_node", "llm_echo")

    graph.add_node("tools", tool_node)
    graph.add_edge("tools", "llm_echo")

    def conditional_decider(
        state: ConversationState[T]
    ) -> Literal["tools", "chat_node"]:
        last_msg = state["messages"][-1]
        assert isinstance(last_msg, AIMessage)
        if len(last_msg.tool_calls) == 0:
            return "chat_node"
        else:
            return "tools"

    graph.add_conditional_edges("llm_echo", conditional_decider)

    runner = graph.compile(checkpointer=checkpointer)
    config : RunnableConfig = {"configurable": {"thread_id": thread_id}}

    class NullHandler(IOHandler):
        async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str):
            pass

        async def log_end(self, path: list[str]):
            pass

        async def log_state_update(self, path: list[str], st: dict):
            pass

        async def log_start(self, *, path: list[str], description: str, tool_id: str | None):
            pass
    # An absent thread comes back as an empty snapshot echoing the request
    # config, which carries no checkpoint id.
    snapshot = await runner.aget_state(config)
    resuming = snapshot.config.get("configurable", {}).get("checkpoint_id") is not None
    curr_state: T
    to_run: ConversationState[T] | Command | None
    if resuming:
        curr_state = cast(T, snapshot.values["extra_data"]) if "extra_data" in snapshot.values else init_data
        to_run = None
        # The person is joining a conversation in progress: show where it stands.
        if state_renderer is not None:
            client.progress_update(StateUpdate(state_renderer(curr_state)))
    else:
        curr_state = init_data
        to_run = init_state

    async with with_handler(
        NullHandler(), NullEventHandler(), RefuseInterrupts()
    ):
        while True:
            prompt: HumanPrompt | None = None
            ended = False
            # Replies seen in an update and not yet acknowledged as durable: the
            # next checkpoint event says they are.
            landed: list[str] = []
            # The stream is drained to its end even once the interrupt has been
            # seen: the checkpoint that records it may still be in flight when the
            # interrupt update is yielded, and a resume against the checkpoint
            # before it replays the previous node, an LLM turn included.
            async for (ev, payload) in runner.astream(
                to_run, config=config, stream_mode=["updates", "checkpoints"]
            ):
                assert isinstance(payload, dict)
                if ev == "checkpoints":
                    for question_id in landed:
                        await client.answer_applied(question_id)
                    landed = []
                    continue
                assert ev == "updates"
                if "__interrupt__" in payload:
                    interrupt_data = payload["__interrupt__"][0].value
                    assert isinstance(interrupt_data, EndConversation) or isinstance(interrupt_data, HumanPrompt)
                    if isinstance(interrupt_data, HumanPrompt):
                        prompt = interrupt_data
                    else:
                        ended = True
                    continue
                landed.extend(replies_in(payload))
                for (_, v) in payload.items():
                    if "extra_data" in v:
                        new_data = cast(T, v["extra_data"])
                        if diff_renderer is not None:
                            client.progress_update(
                                StateUpdate(diff_renderer(curr_state, new_data))
                            )
                        curr_state = new_data
                if "tools" in payload and "messages" in payload["tools"]:
                    for m in payload["tools"]["messages"]:
                        if isinstance(m, ToolMessage):
                            client.progress_update(
                                ToolComplete(
                                    thread_id=m.tool_call_id
                                )
                            )
            if ended or prompt is None:
                break
            res = await client.human_turn(
                prompt, state_renderer(curr_state) if state_renderer is not None else None
            )
            to_run = Command(resume=res)

    to_res = await runner.aget_state(config)
    return cast(ConversationState[T], to_res.values)