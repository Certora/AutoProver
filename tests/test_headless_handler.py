"""A headless autoprove run: ``MultiJobConsoleHandler.over_mailbox`` wires the mailbox in
as the interrupt handler, as the observer of the output events it acks on, and as the
refinement conversation provider, so the pipeline never learns the person is elsewhere.
``resolve_mailbox`` is how the entry point obtains that mailbox without importing any
control plane itself.
"""

import sys
import types
from collections.abc import Sequence

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Interrupt

from composer.io.mailbox import InboxMessage, MailboxConversation, MailboxInterrupts, OpenQuestion, WarmWait
from composer.io.multi_job import TaskInfo
from composer.spec.source.autoprove_common import resolve_mailbox
from composer.ui.autoprove_app import AutoProvePhase
from composer.ui.autoprove_console import AutoProveConsoleHandler
from graphcore.tools.human import Question, QuestionId

pytestmark = pytest.mark.asyncio


class FakeMailbox:
    def __init__(self, messages: dict[str, str] | None = None) -> None:
        self.messages = dict(messages or {})
        self.acked: list[str] = []
        self.recorded: list[OpenQuestion] = []

    async def inbox(self) -> Sequence[InboxMessage]:
        return [
            InboxMessage(id=QuestionId(k), kind="answer", payload=v, thread_id=None, sort=None)
            for k, v in self.messages.items() if k not in self.acked
        ]

    async def ack(self, message_ids: Sequence[QuestionId]) -> None:
        self.acked.extend(message_ids)

    async def dismiss(self, message_ids: Sequence[QuestionId]) -> None:
        raise AssertionError(f"nothing here is obsolete: {list(message_ids)}")

    async def record_question(self, question: OpenQuestion) -> None:
        self.recorded.append(question)

    async def awaiting_input(self) -> None:
        pass


async def test_over_mailbox_answers_acks_on_evidence_and_converses_through_the_mailbox() -> None:
    mailbox = FakeMailbox({"call-a": "A"})
    handler = AutoProveConsoleHandler.over_mailbox(mailbox, WarmWait())
    handle = await handler.make_handler(TaskInfo("extract-0", "Deposits", AutoProvePhase.DISCOVER_DESIGN_DOC))

    # Input: the mailbox answers, and records the question against its thread.
    assert isinstance(handle.interrupt_handler, MailboxInterrupts)
    resume = await handle.interrupt_handler.handle_interrupts(
        [Interrupt(value=Question(QuestionId("call-a"), {"question": "keep it?"}), id="i1")], {}, thread_id="t",
    )
    assert resume == {"i1": "A"}
    assert [(q.id, q.thread_id, q.sort) for q in mailbox.recorded] == [("call-a", "t", "tool")]

    # Output: the console handler is observed by the same mailbox handler, so the ack
    # follows the ToolMessage landing on the thread and that thread's next checkpoint.
    await handle.handler.log_state_update(["t"], {"tools": {"messages": [ToolMessage(content="A", tool_call_id="call-a")]}})
    assert mailbox.acked == []
    await handle.handler.log_checkpoint_id(path=["t"], checkpoint_id="c1")
    assert mailbox.acked == ["call-a"]

    # Conversations: a refinement gets a mailbox client on its own thread.
    async with handle.conversation_provider("the properties", "t/refinement") as client:
        assert isinstance(client, MailboxConversation)


async def test_the_plain_console_handler_refuses_questions_and_converses_at_the_console() -> None:
    handle = await AutoProveConsoleHandler().make_handler(
        TaskInfo("extract-0", "Deposits", AutoProvePhase.DISCOVER_DESIGN_DOC)
    )
    with pytest.raises(RuntimeError, match="no human interaction"):
        await handle.interrupt_handler.handle_interrupts(
            [Interrupt(value=Question(QuestionId("call-a"), {}), id="i1")], {}, thread_id="t",
        )


def test_resolve_mailbox_calls_the_named_factory_with_the_runs_identity(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    plugin = types.ModuleType("fake_control_plane")

    def factory(run_id: str, execution_id: str) -> FakeMailbox:
        seen.append((run_id, execution_id))
        return FakeMailbox()

    plugin.factory = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_control_plane", plugin)

    assert isinstance(resolve_mailbox("fake_control_plane:factory", "run-1", "exec-1"), FakeMailbox)
    assert seen == [("run-1", "exec-1")]
    with pytest.raises(ValueError, match="MODULE:FACTORY"):
        resolve_mailbox("fake_control_plane", "run-1", "exec-1")
