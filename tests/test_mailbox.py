"""``composer.io.mailbox``: interrupts answered from a mailbox by question id, the
warm wait, suspension with the questions recorded, the ack that follows the
evidence of consumption, the startup inventory, and the console bridge's view of
a ``Question``.

The graphs are static: two nodes fan out from START, each asks a question through
``ask`` and records the answer the way a tool does, as a ``ToolMessage`` for its
question id. The mailbox is an in-memory fake.
"""

import asyncio
import operator
from collections.abc import Sequence
from typing import Annotated, TypedDict

import pytest

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from composer.io.context import run_to_completion, with_handler
from composer.io.event_handler import NullEventHandler
from composer.io.context import successor_thread_id
from composer.io.mailbox import (
    InboxMessage,
    MailboxInterrupts,
    OpenQuestion,
    QuestionSort,
    WarmWait,
    chat_reply,
    default_prompt,
    reconcile_inbox,
    thread_evidence,
)
from composer.io.protocol import GraphSuspended, HumanInteractionBridge, observed
from graphcore.tools.human import QuestionId, ask

pytestmark = pytest.mark.asyncio


class FakeMailbox:
    def __init__(self, messages: dict[str, str] | None = None) -> None:
        self.messages: dict[str, str] = dict(messages or {})
        self.acked: list[str] = []
        self.dismissed: list[str] = []
        self.questions: dict[str, str] = {}  # question id -> prompt
        self.threads: dict[str, str] = {}  # question id -> the thread recorded with it
        self.sorts: dict[str, QuestionSort] = {}  # question id -> the sort recorded with it
        self.awaiting = 0

    async def inbox(self) -> Sequence[InboxMessage]:
        return [
            InboxMessage(
                id=QuestionId(k), kind="answer", payload=v, thread_id=self.threads.get(k), sort=self.sorts.get(k)
            )
            for k, v in self.messages.items() if k not in self.acked and k not in self.dismissed
        ]

    async def ack(self, message_ids: Sequence[QuestionId]) -> None:
        self.acked.extend(message_ids)

    async def dismiss(self, message_ids: Sequence[QuestionId]) -> None:
        self.dismissed.extend(message_ids)

    async def record_question(self, question: OpenQuestion) -> None:
        self.questions[question.id] = question.prompt
        self.threads[question.id] = question.thread_id
        self.sorts[question.id] = question.sort

    async def awaiting_input(self) -> None:
        self.awaiting += 1


class _Quiet:
    def __init__(self) -> None:
        self.checkpoints: list[tuple[str, ...]] = []

    async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str) -> None:
        self.checkpoints.append(tuple(path))

    async def log_state_update(self, path: list[str], st: dict) -> None:
        pass

    async def log_start(self, *, path: list[str], description: str, tool_id: str | None) -> None:
        pass

    async def log_end(self, path: list[str]) -> None:
        pass


def Handler(mailbox: FakeMailbox, warm: WarmWait = WarmWait()) -> MailboxInterrupts:
    return MailboxInterrupts(mailbox, warm)


class QState(TypedDict):
    log: Annotated[list[str], operator.add]
    messages: Annotated[list, operator.add]


def _answered(qid: str, answer: str) -> dict:
    return {"log": [f"{qid}={answer}"], "messages": [ToolMessage(content=answer, tool_call_id=qid)]}


def two_questions(saver: InMemorySaver | None = None):
    def ask_a(state: QState) -> dict:
        return _answered("call-a", ask(QuestionId("call-a"), {"question": "a?"}))

    def ask_b(state: QState) -> dict:
        return _answered("call-b", ask(QuestionId("call-b"), {"question": "b?"}))

    b = StateGraph(QState)
    b.add_node("ask_a", ask_a)
    b.add_node("ask_b", ask_b)
    b.add_edge(START, "ask_a")
    b.add_edge(START, "ask_b")
    b.add_edge("ask_a", END)
    b.add_edge("ask_b", END)
    return b.compile(checkpointer=saver or InMemorySaver())


async def run(graph, interrupts, *, resume: bool = False) -> QState:
    """Run under a quiet output handler; ``interrupts`` answers, and observes the
    output events when it is a mailbox handler."""
    output = observed(_Quiet(), interrupts) if isinstance(interrupts, MailboxInterrupts) else _Quiet()
    async with with_handler(output, NullEventHandler(), interrupts):
        return await run_to_completion(
            graph, None if resume else {"log": [], "messages": []}, thread_id="t",
            recursion_limit=10, description="q",
        )


async def test_answers_present_resume_by_question_id_and_are_acked_once_consumed() -> None:
    mailbox = FakeMailbox({"call-a": "A", "call-b": "B", "unrelated": "x"})
    handler = Handler(mailbox)
    result = await run(two_questions(), handler)
    assert sorted(result["log"]) == ["call-a=A", "call-b=B"]
    assert sorted(mailbox.acked) == ["call-a", "call-b"]
    # Recorded even though answered on the first look: the record is what names
    # the thread a later inventory would have to check.
    assert mailbox.questions == {"call-a": "a?", "call-b": "b?"}
    assert mailbox.threads == {"call-a": "t", "call-b": "t"}
    assert mailbox.sorts == {"call-a": "tool", "call-b": "tool"}


async def test_no_answers_records_the_questions_and_suspends() -> None:
    mailbox = FakeMailbox()
    with pytest.raises(GraphSuspended) as caught:
        await run(two_questions(), Handler(mailbox))
    assert {i.value.id for i in caught.value.interrupts} == {"call-a", "call-b"}
    assert mailbox.questions == {"call-a": "a?", "call-b": "b?"}
    assert mailbox.threads == {"call-a": "t", "call-b": "t"}
    assert mailbox.acked == []


async def test_partial_answers_wait_for_the_rest_and_do_not_consume() -> None:
    mailbox = FakeMailbox({"call-a": "A"})
    with pytest.raises(GraphSuspended):
        await run(two_questions(), Handler(mailbox))
    assert mailbox.questions == {"call-a": "a?", "call-b": "b?"}
    assert mailbox.acked == []  # the present answer stays in the inbox for the next process


async def test_warm_wait_picks_up_answers_that_arrive_in_time() -> None:
    mailbox = FakeMailbox()

    async def deliver_later() -> None:
        await asyncio.sleep(0.05)
        mailbox.messages.update({"call-a": "A", "call-b": "B"})

    asyncio.get_running_loop().create_task(deliver_later())
    result = await run(two_questions(), Handler(mailbox, WarmWait(seconds=2.0, poll_interval=0.01)))
    assert sorted(result["log"]) == ["call-a=A", "call-b=B"]
    assert mailbox.questions == {"call-a": "a?", "call-b": "b?"}  # announced while waiting
    assert sorted(mailbox.acked) == ["call-a", "call-b"]


async def test_a_payload_without_a_question_id_cannot_be_mailed() -> None:
    def bare(state: QState) -> dict:
        return {"log": [interrupt({"question": "bare?"})]}

    b = StateGraph(QState)
    b.add_node("bare", bare)
    b.add_edge(START, "bare")
    b.add_edge("bare", END)
    graph = b.compile(checkpointer=InMemorySaver())
    with pytest.raises(RuntimeError, match="no question id"):
        await run(graph, Handler(FakeMailbox({"x": "y"})))


async def test_an_answer_never_seen_consumed_is_never_acked() -> None:
    """A node that takes the answer but records no ToolMessage for it gives the
    handler no evidence, so the message stays in the inbox."""

    def take(state: QState) -> dict:
        return {"log": [ask(QuestionId("call-a"), {"question": "a?"})]}

    b = StateGraph(QState)
    b.add_node("take", take)
    b.add_edge(START, "take")
    b.add_edge("take", END)
    graph = b.compile(checkpointer=InMemorySaver())
    mailbox = FakeMailbox({"call-a": "A"})
    result = await run(graph, Handler(mailbox))
    assert result["log"] == ["A"]
    assert mailbox.acked == []


async def test_only_the_landing_path_s_checkpoint_acks() -> None:
    """The bookkeeping in isolation: an answer is handed out, a sub-agent on another
    path checkpoints (no ack), the ToolMessage lands on the parent's path (no ack yet),
    an unrelated path checkpoints again (no ack), the parent's path checkpoints (ack)."""
    from langgraph.types import Interrupt

    mailbox = FakeMailbox({"call-a": "A"})
    handler = Handler(mailbox)
    from graphcore.tools.human import Question

    resume = await handler.handle_interrupts(
        [Interrupt(value=Question(QuestionId("call-a"), {"question": "a?"}), id="i1")], {}, thread_id="parent"
    )
    assert resume == {"i1": "A"}
    assert mailbox.threads == {"call-a": "parent"}

    await handler.on_checkpoint(["parent", "child"], "c1")
    assert mailbox.acked == []
    await handler.on_state_update(["parent"], {"tools": {"messages": [ToolMessage(content="A", tool_call_id="call-a")]}})
    assert mailbox.acked == []
    await handler.on_checkpoint(["parent", "child"], "c2")
    assert mailbox.acked == []
    await handler.on_checkpoint(["parent"], "c3")
    assert mailbox.acked == ["call-a"]
    await handler.on_checkpoint(["parent"], "c4")
    assert mailbox.acked == ["call-a"]  # once


async def test_suspend_then_resume_in_a_new_process_consumes_and_acks() -> None:
    saver = InMemorySaver()
    cold = FakeMailbox()
    with pytest.raises(GraphSuspended):
        await run(two_questions(saver), Handler(cold))

    warm = FakeMailbox({"call-a": "A", "call-b": "B"})
    result = await run(two_questions(saver), Handler(warm), resume=True)
    assert sorted(result["log"]) == ["call-a=A", "call-b=B"]
    assert sorted(warm.acked) == ["call-a", "call-b"]


class MState(TypedDict):
    messages: Annotated[list, operator.add]


def _tool_call(call_id: str) -> dict:
    return {"name": "ask", "args": {}, "id": call_id, "type": "tool_call"}


async def _thread_with(saver: InMemorySaver, thread_id: str, messages: list) -> None:
    """One checkpointed turn on ``thread_id`` appending ``messages``."""
    b = StateGraph(MState)
    b.add_node("turn", lambda state: {"messages": messages})
    b.add_edge(START, "turn")
    b.add_edge("turn", END)
    await b.compile(checkpointer=saver).ainvoke({"messages": []}, {"configurable": {"thread_id": thread_id}})


async def test_startup_inventory_acks_the_consumed_and_retires_the_obsolete() -> None:
    """Judged from the checkpoints alone. On thread t: call-a was asked and answered as a
    ToolMessage, ai-1 was asked and replied to, call-b and ai-2 were asked and never answered,
    and nothing ever asked call-c or ai-9. The sub-agent thread at generation 0 asked call-g
    and was then superseded by generation 1. A message whose question was never recorded
    names no thread and is left alone."""
    saver = InMemorySaver()
    await _thread_with(saver, "t", [
        AIMessage(content="", tool_calls=[_tool_call("call-a"), _tool_call("call-b")]),
        ToolMessage(content="A", tool_call_id="call-a"),
        AIMessage(content="shall I?", id="ai-1"),
        chat_reply("go ahead", "ai-1"),
        AIMessage(content="done. anything else?", id="ai-2"),
    ])
    asks_g = [AIMessage(content="", tool_calls=[_tool_call("call-g")])]
    await _thread_with(saver, "t/agent:tc/g0", asks_g)
    await _thread_with(saver, "t/agent:tc/g1", asks_g)

    evidence = await thread_evidence(saver, "t")
    assert evidence.consumed == {("tool", "call-a"), ("chat", "ai-1")}
    assert {("tool", "call-a"), ("tool", "call-b"), ("chat", "ai-1"), ("chat", "ai-2")} <= evidence.prompted

    mailbox = FakeMailbox({
        "call-a": "A", "call-b": "B", "call-c": "C", "ai-1": "go ahead", "ai-2": "no", "ai-9": "?",
        "call-g": "G", "unrecorded": "x",
    })
    mailbox.threads = {
        "call-a": "t", "call-b": "t", "call-c": "t", "ai-1": "t", "ai-2": "t", "ai-9": "t", "call-g": "t/agent:tc/g0",
    }
    mailbox.sorts = {
        "call-a": "tool", "call-b": "tool", "call-c": "tool", "ai-1": "chat", "ai-2": "chat", "ai-9": "chat",
        "call-g": "tool",
    }
    inventory = await reconcile_inbox(mailbox, saver)
    assert sorted(inventory.consumed) == ["ai-1", "call-a"]
    assert sorted(inventory.obsolete) == ["ai-9", "call-c", "call-g"]
    assert sorted(mailbox.acked) == ["ai-1", "call-a"]
    assert sorted(mailbox.dismissed) == ["ai-9", "call-c", "call-g"]
    assert {m.id for m in await mailbox.inbox()} == {"call-b", "ai-2", "unrecorded"}, "pending stays; unrecorded untouched"


async def test_a_generation_still_at_the_frontier_keeps_its_question_pending() -> None:
    saver = InMemorySaver()
    await _thread_with(saver, "t/agent:tc/g0", [AIMessage(content="", tool_calls=[_tool_call("call-g")])])
    mailbox = FakeMailbox({"call-g": "G"})
    mailbox.threads, mailbox.sorts = {"call-g": "t/agent:tc/g0"}, {"call-g": "tool"}
    inventory = await reconcile_inbox(mailbox, saver)
    assert (inventory.consumed, inventory.obsolete) == ([], [])
    assert mailbox.acked == [] and mailbox.dismissed == []


async def test_the_sort_decides_which_evidence_counts() -> None:
    # The same ids recorded under the other sort: no message of that kind asked them, so
    # they are obsolete rather than consumed.
    saver = InMemorySaver()
    await _thread_with(saver, "t", [
        AIMessage(content="", tool_calls=[_tool_call("call-a")]),
        ToolMessage(content="A", tool_call_id="call-a"),
        AIMessage(content="shall I?", id="ai-1"),
        chat_reply("go ahead", "ai-1"),
    ])
    crossed = FakeMailbox({"call-a": "A", "ai-1": "go ahead"})
    crossed.threads = {"call-a": "t", "ai-1": "t"}
    crossed.sorts = {"call-a": "chat", "ai-1": "tool"}
    inventory = await reconcile_inbox(crossed, saver)
    assert inventory.consumed == [] and sorted(inventory.obsolete) == ["ai-1", "call-a"]


def test_successor_thread_id_is_the_inverse_of_the_generation_suffix() -> None:
    assert successor_thread_id("autoprove_r/agent:call-1/g0") == "autoprove_r/agent:call-1/g1"
    assert successor_thread_id("root/a:x/g3/child:y/g12") == "root/a:x/g3/child:y/g13"
    assert successor_thread_id("autoprove_r") is None, "not a durable sub-agent thread"
    assert successor_thread_id("autoprove_r-refinement") is None
    assert successor_thread_id("root/a:x/gx") is None


async def test_console_bridge_sees_the_payload_not_the_envelope() -> None:
    class OneAtATime(HumanInteractionBridge[dict], _Quiet):
        def __init__(self) -> None:
            _Quiet.__init__(self)
            self.seen: list[dict] = []

        async def human_interaction(self, ty: dict) -> str:
            self.seen.append(ty)
            return "ok"

    handler = OneAtATime()
    async with with_handler(handler, NullEventHandler(), handler):
        result = await run_to_completion(
            two_questions(), {"log": [], "messages": []}, thread_id="t", recursion_limit=10, description="q"
        )
    assert sorted(result["log"]) == ["call-a=ok", "call-b=ok"]
    assert sorted(q["question"] for q in handler.seen) == ["a?", "b?"]


def test_default_prompt_prefers_a_question_field() -> None:
    assert default_prompt({"question": "why?", "context": "..."}) == "why?"
    assert default_prompt("plain text") == "plain text"
