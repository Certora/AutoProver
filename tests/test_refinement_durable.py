"""The refinement conversation is durable: it runs on the run's checkpointer under
a deterministic thread id, so a process that dies mid-conversation is picked up
by the next one at the turn it was waiting on, without replaying the LLM turn
that asked it. Each reply names the AI message it answers, and the client hears
``answer_applied`` for that question once the checkpoint carrying the reply is
written.

The LLM is a canned list of replies with no tool calls; the client is scripted.
"""

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from rich.console import RenderableType

from composer.io.conversation import HumanPrompt, ProgressPayload, StateUpdate, ThinkingStart
from composer.io.mailbox import REPLY_FIELD, InboxMessage, MailboxConversation, OpenQuestion, WarmWait
from composer.io.protocol import GraphSuspended
from composer.spec.refinement import refinement_loop
from graphcore.tools.human import QuestionId

pytestmark = pytest.mark.asyncio

THREAD = "component-7-refinement"


class Died(Exception):
    """The process went away mid-conversation."""


class NoToolsFake(FakeMessagesListChatModel):
    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "NoToolsFake":
        return self


class Scripted:
    """Answers from a script; dies when it runs out. Records everything."""

    def __init__(self, answers: Sequence[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[HumanPrompt] = []
        self.applied: list[str] = []
        self.progress: list[ProgressPayload] = []

    def progress_update(self, progress: ProgressPayload) -> None:
        self.progress.append(progress)

    async def human_turn(self, prompt: HumanPrompt, state: RenderableType | None) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise Died()
        return self.answers.pop(0)

    async def answer_applied(self, question_id: str) -> None:
        self.applied.append(question_id)

    def llm_turns(self) -> int:
        return sum(isinstance(p, ThinkingStart) for p in self.progress)


async def run(saver: InMemorySaver, llm: NoToolsFake, client: Scripted) -> None:
    await refinement_loop(
        llm, client, init_data=["p1"],
        init_messages=[SystemMessage("sys"), AIMessage("<task-complete>", id="opener")],
        tools=[], checkpointer=saver, thread_id=THREAD, state_renderer=lambda d: ", ".join(d),
    )


async def test_a_conversation_resumes_at_the_turn_it_was_waiting_on() -> None:
    saver = InMemorySaver()
    llm = NoToolsFake(responses=[AIMessage("what color?"), AIMessage("ok, done"), AIMessage("bye")])

    # Process 1: answers the opening turn, is asked about color, dies.
    first = Scripted(["blue"])
    with pytest.raises(Died):
        await run(saver, llm, first)
    assert first.prompts[0] == HumanPrompt(question_id="opener", ai_message=None)
    asked_color = first.prompts[1]
    assert asked_color.ai_message == "what color?"
    assert first.applied == ["opener"], "the opening reply was acknowledged once checkpointed"
    assert first.llm_turns() == 1

    # Process 2: finds the same question waiting, answers it, dies at the next.
    second = Scripted(["red"])
    with pytest.raises(Died):
        await run(saver, llm, second)
    assert isinstance(second.progress[0], StateUpdate), "joining in progress shows the current state"
    assert second.prompts[0] == asked_color, "resumed at the very question, same id, no re-ask"
    assert second.prompts[1].ai_message == "ok, done"
    assert second.applied == [asked_color.question_id]
    assert second.llm_turns() == 1, "the turn that asked about color was not replayed"

    # The replies in the durable state name the AI messages they answered.
    saved = await saver.aget_tuple({"configurable": {"thread_id": THREAD}})
    assert saved is not None
    replies = [
        (m.content, getattr(m, REPLY_FIELD, None))
        for m in saved.checkpoint["channel_values"]["messages"]
        if isinstance(m, HumanMessage)
    ]
    assert replies == [("blue", "opener"), ("red", asked_color.question_id)]


class FakeMailbox:
    """The control plane, in memory: id-keyed messages, an ack list, the records."""

    def __init__(self) -> None:
        self.messages: dict[str, str] = {}
        self.acked: list[str] = []
        self.recorded: list[OpenQuestion] = []
        self.awaiting = 0

    async def inbox(self) -> Sequence[InboxMessage]:
        by_id = {q.id: q for q in self.recorded}
        return [
            InboxMessage(
                id=QuestionId(k), kind="answer", payload=v,
                thread_id=by_id[k].thread_id if k in by_id else None, sort=by_id[k].sort if k in by_id else None,
            )
            for k, v in self.messages.items() if k not in self.acked
        ]

    async def ack(self, message_ids: Sequence[QuestionId]) -> None:
        self.acked.extend(message_ids)

    async def dismiss(self, message_ids: Sequence[QuestionId]) -> None:
        raise AssertionError(f"nothing here is obsolete: {list(message_ids)}")

    async def record_question(self, question: OpenQuestion) -> None:
        self.recorded.append(question)

    async def awaiting_input(self) -> None:
        self.awaiting += 1


async def test_a_conversation_over_a_mailbox_suspends_and_resumes_per_answer() -> None:
    """Three processes, one per posted answer, the way a control plane would run
    it: each suspends with its question recorded, the next finds the answer in
    the inbox, applies it, acks it, and asks the next question."""
    saver = InMemorySaver()
    llm = NoToolsFake(responses=[AIMessage("what color?"), AIMessage("ok, done"), AIMessage("bye")])
    plane = FakeMailbox()

    def client() -> MailboxConversation:
        return MailboxConversation(plane, THREAD, WarmWait(seconds=0.0, poll_interval=0.01))

    # Process 1: nothing in the inbox; records the opening question and suspends.
    with pytest.raises(GraphSuspended):
        await run(saver, llm, client())
    (opening,) = plane.recorded
    assert opening.id == "opener" and opening.sort == "chat" and opening.thread_id == THREAD
    assert "p1" in opening.prompt, "the state render is the person's context"
    assert plane.acked == []

    # Process 2: the answer is there; it is applied and acked, and the color question recorded.
    plane.messages["opener"] = "blue"
    with pytest.raises(GraphSuspended):
        await run(saver, llm, client())
    assert plane.acked == ["opener"]
    color = plane.recorded[-1]
    assert color.sort == "chat" and color.prompt.startswith("what color?")

    # Process 3: same again for the color answer; the LLM was asked exactly twice overall.
    plane.messages[color.id] = "red"
    with pytest.raises(GraphSuspended):
        await run(saver, llm, client())
    assert plane.acked == ["opener", color.id]
    assert plane.recorded[-1].prompt.startswith("ok, done")
    assert llm.i == 2, "each process resumed at its question; no LLM turn was replayed"


async def test_a_fresh_thread_starts_from_the_initial_messages() -> None:
    saver = InMemorySaver()
    llm = NoToolsFake(responses=[AIMessage("what color?")])
    client = Scripted([])
    with pytest.raises(Died):
        await run(saver, llm, client)
    assert client.prompts == [HumanPrompt(question_id="opener", ai_message=None)]
    assert client.progress == [], "a fresh start shows nothing before the first turn"
    assert client.llm_turns() == 0
