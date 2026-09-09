"""The run's mailbox: how a graph's questions reach a person outside the process
and how the answers come back.

A question comes in two sorts, and its record says which. A *tool* question is
an interrupt raised through ``graphcore.tools.human.ask``: a :class:`Question`
whose ``QuestionId`` is the asking tool call's id, and whose answer is consumed
as the ``ToolMessage`` for that call. A *chat* question is a turn of the
refinement conversation: its id is the AI message the person is replying to,
and its answer is consumed as the ``HumanMessage`` that :func:`chat_reply`
builds, which names that AI message. The chat sort never touches ``Question``
or the interrupt handler; the conversation provider drives it over this same
transport. Either way the id is the identity everything is keyed by: the inbox
message that answers it, the ack that retires the message, and the message that
proves the answer was consumed. It is a different thing from LangGraph's
``InterruptId``, which names the pending task; the resume map is keyed by the
latter, the mailbox by the former, and the types keep them apart. Answers are
text, always.

:class:`Mailbox` is the transport: four calls, implemented over whatever the
run's control plane is (``aws_mock.mailbox`` for the laptop mock, the AISS API
once it has the endpoints). :class:`MailboxInterrupts` is the ``IOHandler``
piece: it records every question it is asked, with the thread that paused on
it, answers what the inbox already holds, waits warm for the rest, and raises
``GraphSuspended``. An answer is acked only once the handler has SEEN it
consumed, a ``ToolMessage`` for its question in a state update on some thread,
and that thread has then checkpointed; handing the answer to the graph proves
nothing, and a checkpoint on another thread proves nothing about this one.
:func:`reconcile_inbox` is the startup inventory: a predecessor may have
consumed an answer and died before acking it, and the ack must never be lost or
duplicated into a re-application; an answer no execution will ever consume must
be retired as obsolete, or the control plane restarts a finished run over it
forever. The inbox says which thread each message's question paused, so the
inventory reads that thread's history and no other.
"""

import asyncio
import io
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Interrupt
from rich.console import Console, RenderableType

from composer.io.context import successor_thread_id
from composer.io.conversation import ConversationClient, ConversationContextProvider, HumanPrompt, ProgressPayload
from composer.io.protocol import GraphSuspended, InterruptId
from graphcore.tools.human import Question, QuestionId


type QuestionSort = Literal["tool", "chat"]
"""What a question's id names and how its answer is consumed: a tool call
answered by a ``ToolMessage``, or an AI message answered by a reply."""


@dataclass(frozen=True)
class OpenQuestion:
    """What a question's record on the control plane holds: enough for a person
    to answer it and for a later execution to find the answer's evidence."""

    id: QuestionId
    prompt: str
    #: The thread that paused on the question, whose history will hold the
    #: consumption evidence.
    thread_id: str
    sort: QuestionSort


@dataclass(frozen=True)
class InboxMessage:
    """An un-acked message for this run: a person's text reply to a question."""

    id: QuestionId
    kind: str
    payload: str
    #: From the question's record, as it was asked; both None when the control
    #: plane has no question record for this id.
    thread_id: str | None
    sort: QuestionSort | None


class Mailbox(Protocol):
    """The control-plane transport, scoped to one run and one execution."""

    async def inbox(self) -> Sequence[InboxMessage]:
        """Every message for the run that no execution has acked yet, each with
        the thread and sort its question was recorded with."""
        ...

    async def ack(self, message_ids: Sequence[QuestionId]) -> None:
        """Retire messages this execution has durably consumed. Idempotent."""
        ...

    async def dismiss(self, message_ids: Sequence[QuestionId]) -> None:
        """Retire messages no execution will ever consume, recorded as obsolete
        rather than applied, so a person can be told their answer went unused.
        Idempotent."""
        ...

    async def record_question(self, question: OpenQuestion) -> None:
        """Tell the control plane a question is open, so a person can answer it,
        and where and how its answer will be consumed. Idempotent by id: asking
        again re-announces, never duplicates."""
        ...

    async def awaiting_input(self) -> None:
        """This execution is about to exit because it is waiting on answers."""
        ...


@dataclass(frozen=True)
class WarmWait:
    """How long to keep polling the inbox before suspending, and how often."""

    seconds: float = 0.0
    poll_interval: float = 1.0


def default_prompt(payload: Any) -> str:
    """The text a person is shown for a question: a ``question`` field when the
    payload has one, else the payload itself."""
    if isinstance(payload, Mapping) and "question" in payload:
        return str(payload["question"])
    question = getattr(payload, "question", None)
    return str(payload) if question is None else str(question)


class MailboxInterrupts:
    """The ``InterruptHandler`` that answers from a :class:`Mailbox`, and the
    ``StateObserver`` that acks what it answered once it sees the answer
    consumed. Pass it as the scope's interrupt handler and wrap the scope's
    output handler with ``observed(io, mailbox_interrupts)`` so it sees the
    updates and checkpoints it acks on.

    Answering is all-or-nothing per pause: the graph's step cannot complete
    until every interrupting task has its answer, so a partial resume would
    make no progress and would only complicate the ack.

    The ack follows the evidence, not the hand-off. An answer handed to the
    graph is *answered*; when a state update on some path carries the
    ``ToolMessage`` whose tool call id is the question's, it has *landed* on
    that path; when that same path's next checkpoint event arrives, the update
    is in the durable state and the answer is acked. A checkpoint on any other
    path, a delegated sub-agent moving on with its own work, say, is ignored.
    An answer whose ``ToolMessage`` never appears is never acked here: only a
    question asked by a tool call can be a mailbox question. With LangGraph's
    default ``"async"`` durability the checkpoint's own write may still be in
    flight when its event arrives; a graph that must never lose an answer to a
    crash in that window runs with ``durability="sync"``.
    """

    def __init__(
        self,
        mailbox: Mailbox,
        warm: WarmWait = WarmWait(),
        *,
        prompt_of: Callable[[Any], str] = default_prompt,
    ) -> None:
        self._mailbox = mailbox
        self._warm = warm
        self._prompt_of = prompt_of
        #: Handed to the graph, consumption not yet seen.
        self._answered: set[QuestionId] = set()
        #: Consumption seen in a state update on this path; acked on the path's next checkpoint.
        self._landed: dict[tuple[str, ...], list[QuestionId]] = {}

    async def handle_interrupts(
        self, interrupts: Sequence[Interrupt], state: Any, *, thread_id: str
    ) -> Mapping[InterruptId, str]:
        questions: dict[QuestionId, Interrupt] = {}
        for intr in interrupts:
            if not isinstance(intr.value, Question):
                raise RuntimeError(
                    f"interrupt payload of type {type(intr.value).__name__} carries no question id; "
                    "only questions raised through graphcore.tools.human.ask can be answered from a mailbox"
                )
            questions[intr.value.id] = intr

        # Recorded before the first look at the inbox, answered or not: the record
        # is what names the thread an answer's evidence will be in, and an answer
        # posted ahead of the question would otherwise leave no record at all.
        for qid, intr in questions.items():
            await self._mailbox.record_question(
                OpenQuestion(qid, self._prompt_of(intr.value.payload), thread_id, "tool")
            )

        deadline = time.monotonic() + self._warm.seconds
        while True:
            inbox = {m.id: m for m in await self._mailbox.inbox()}
            answers = {qid: inbox[qid].payload for qid in questions if qid in inbox}
            if len(answers) == len(questions):
                self._answered.update(answers)
                return {InterruptId(questions[qid].id): payload for qid, payload in answers.items()}
            if time.monotonic() >= deadline:
                raise GraphSuspended(interrupts)
            await asyncio.sleep(self._warm.poll_interval)

    async def on_state_update(self, path: list[str], st: dict) -> None:
        if self._answered:
            landed = [qid for qid in consumed_in(st) if qid in self._answered]
            if landed:
                self._answered.difference_update(landed)
                self._landed.setdefault(tuple(path), []).extend(landed)

    async def on_checkpoint(self, path: list[str], checkpoint_id: str) -> None:
        ids = self._landed.pop(tuple(path), None)
        if ids:
            await self._mailbox.ack(ids)


def _plain_text(display: RenderableType) -> str:
    console = Console(file=io.StringIO(), record=True, width=100, color_system=None)
    console.print(display, markup=False)
    return console.export_text().rstrip()


class MailboxConversation:
    """The ``ConversationClient`` for a run whose person is somewhere else. Each
    turn is recorded as a chat question, with the state render as its text so
    the person has the context a console would show them; the reply is taken
    from the inbox, waiting warm for it the way tool questions do; the message
    is acked once the loop reports the reply durable. Progress goes nowhere:
    the person follows the run through the control plane's own view."""

    def __init__(self, mailbox: Mailbox, thread_id: str, warm: WarmWait = WarmWait()) -> None:
        self._mailbox = mailbox
        self._thread_id = thread_id
        self._warm = warm

    def progress_update(self, progress: ProgressPayload) -> None:
        pass

    async def human_turn(self, prompt: HumanPrompt, state: RenderableType | None) -> str:
        question_id = QuestionId(prompt.question_id)
        parts = [p for p in (prompt.ai_message, None if state is None else _plain_text(state)) if p]
        await self._mailbox.record_question(
            OpenQuestion(question_id, "\n\n".join(parts) or "Your turn.", self._thread_id, "chat")
        )
        deadline = time.monotonic() + self._warm.seconds
        while True:
            inbox = {m.id: m for m in await self._mailbox.inbox()}
            if (message := inbox.get(question_id)) is not None:
                return message.payload
            if time.monotonic() >= deadline:
                # The loop, not the runner, holds this interrupt, so the envelope
                # names the question rather than LangGraph's task-derived id.
                raise GraphSuspended([Interrupt(value=prompt, id=prompt.question_id)])
            await asyncio.sleep(self._warm.poll_interval)

    async def answer_applied(self, question_id: str) -> None:
        await self._mailbox.ack([QuestionId(question_id)])


def mailbox_conversations(mailbox: Mailbox, warm: WarmWait) -> ConversationContextProvider:
    """The conversation provider for a run whose person is elsewhere: each refinement
    conversation gets a :class:`MailboxConversation` recording against its own thread.
    The opening render goes nowhere; every turn carries the current state instead."""

    @asynccontextmanager
    async def provider(initial: RenderableType, thread_id: str) -> AsyncIterator[ConversationClient]:
        yield MailboxConversation(mailbox, thread_id, warm)

    return provider


#: The field on a chat reply naming the AI message it answers. An extra field on
#: the message, the way graphcore's ``display_tag`` is: provider adapters do not
#: forward it, and checkpoints keep it. Not ``additional_kwargs``, which some
#: adapters do forward, and not the reply's own id, which ``add_messages`` would
#: upsert over the AI message.
REPLY_FIELD = "answers_question"


def chat_reply(content: str, question_id: str) -> HumanMessage:
    """The message a chat question's answer is recorded as."""
    return HumanMessage(content=content, **{REPLY_FIELD: question_id})


def _answered_by(message: Any) -> tuple[QuestionSort, QuestionId] | None:
    """The question a message consumes the answer to, if it is such a message."""
    if isinstance(message, ToolMessage):
        return ("tool", QuestionId(message.tool_call_id))
    if isinstance(message, HumanMessage):
        question_id = getattr(message, REPLY_FIELD, None)
        if isinstance(question_id, str):
            return ("chat", QuestionId(question_id))
    return None


def _answers_in(update: Mapping[str, Any], sort: QuestionSort) -> Iterable[QuestionId]:
    for value in update.values():
        if not isinstance(value, Mapping):
            continue
        for message in value.get("messages", None) or []:
            answered = _answered_by(message)
            if answered is not None and answered[0] == sort:
                yield answered[1]


def consumed_in(update: Mapping[str, Any]) -> Iterable[QuestionId]:
    """The tool questions whose answers a state update carries: the tool call ids
    of the ``ToolMessage``s in it, node by node."""
    return _answers_in(update, "tool")


def replies_in(update: Mapping[str, Any]) -> Iterable[QuestionId]:
    """The chat questions whose answers a state update carries: the AI messages
    the replies in it name, node by node."""
    return _answers_in(update, "chat")


def _asked_by(message: Any) -> Iterable[tuple[QuestionSort, QuestionId]]:
    """The questions a message asked, if it is an AI message: a tool question per tool
    call it carries, and the chat question a reply to it would answer."""
    if not isinstance(message, AIMessage):
        return
    for call in message.tool_calls:
        if call.get("id"):
            yield ("tool", QuestionId(call["id"]))
    if message.id is not None:
        yield ("chat", QuestionId(message.id))


@dataclass(frozen=True)
class ThreadEvidence:
    """What a thread's checkpoint history says about the questions on it, by sort and
    id: the questions whose asking is in the history (the AI message carrying the tool
    call, or the AI message a chat turn replies to), and the questions whose answer some
    message consumed (a ``ToolMessage`` for the call, a reply naming the AI message).
    The history, not the tip: summarization can drop a message from the tip without
    unasking or un-consuming anything."""

    prompted: frozenset[tuple[QuestionSort, QuestionId]]
    consumed: frozenset[tuple[QuestionSort, QuestionId]]


async def thread_evidence(saver: BaseCheckpointSaver, thread_id: str) -> ThreadEvidence:
    prompted: set[tuple[QuestionSort, QuestionId]] = set()
    consumed: set[tuple[QuestionSort, QuestionId]] = set()
    async for saved in saver.alist({"configurable": {"thread_id": thread_id}}):
        for message in saved.checkpoint.get("channel_values", {}).get("messages", None) or []:
            prompted.update(_asked_by(message))
            if (answered := _answered_by(message)) is not None:
                consumed.add(answered)
    return ThreadEvidence(frozenset(prompted), frozenset(consumed))


async def _superseded(saver: BaseCheckpointSaver, thread_id: str) -> bool:
    """A durable sub-agent thread whose next generation exists will never be resumed,
    the generation walk skips it, so every question pending on it is dead."""
    successor = successor_thread_id(thread_id)
    if successor is None:
        return False
    return await saver.aget_tuple({"configurable": {"thread_id": successor}}) is not None


@dataclass(frozen=True)
class Inventory:
    """What the startup inventory did: the answers acked as consumed by a predecessor,
    and the answers retired as obsolete. Everything else stays pending."""

    consumed: list[QuestionId]
    obsolete: list[QuestionId]


async def reconcile_inbox(mailbox: Mailbox, saver: BaseCheckpointSaver) -> Inventory:
    """Startup inventory. Every un-acked message is judged from durable state alone, so
    the pass is safe to repeat, and lands in one of three places:

    * *consumed*: its thread's history holds the message that applied the answer, so a
      predecessor consumed it and died before acking. Acked, never re-applied.
    * *obsolete*: no execution will ever consume it, because the thread it names was
      superseded by a later generation, or nothing in that thread's history ever asked
      the question, the AI message carrying the tool call or the AI message the chat
      turn replies to being absent. Dismissed, so a person can be told the answer
      went unused. A thread this checkpointer has never seen falls here too: better
      than an inbox entry that lives forever.
    * *pending*: asked, not yet answered. Left for a live interrupt in this execution
      to consume, which is exactly the set that will be.

    After the pass only pending remains un-acked, which is what keeps the control plane
    from restarting a finished run over an answer nobody wants. A message whose question
    was never recorded names no thread and is left alone."""
    by_thread: dict[str, list[tuple[QuestionSort, InboxMessage]]] = {}
    for message in await mailbox.inbox():
        if message.thread_id is not None and message.sort is not None:
            by_thread.setdefault(message.thread_id, []).append((message.sort, message))
    consumed: list[QuestionId] = []
    obsolete: list[QuestionId] = []
    for thread_id, messages in by_thread.items():
        if await _superseded(saver, thread_id):
            obsolete.extend(m.id for _, m in messages)
            continue
        evidence = await thread_evidence(saver, thread_id)
        for sort, m in messages:
            if (sort, m.id) in evidence.consumed:
                consumed.append(m.id)
            elif (sort, m.id) not in evidence.prompted:
                obsolete.append(m.id)
    if consumed:
        await mailbox.ack(consumed)
    if obsolete:
        await mailbox.dismiss(obsolete)
    return Inventory(consumed, obsolete)
