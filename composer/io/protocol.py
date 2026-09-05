"""
Handler protocols for graph execution.

Output and input are separate protocols so they vary independently: a
headless container keeps its console output while taking answers from a
mailbox, a local run keeps the same output while prompting inline.

``IOHandler`` is output: it receives structural events (start/end, state
updates, checkpoints) from the background drainer. ``InterruptHandler`` is
input: it answers the interrupts a graph pauses on. ``StateObserver`` is how an
input handler that needs to see output events (the mailbox acks on evidence in
state updates) gets them without sitting in the output handler's inheritance
chain: ``observed(io, observer)`` forwards them. A UI that is both simply
implements both protocols and is passed as both.

Domain-specific sub-protocols (``CodeGenIOHandler``,
``NatSpecIOHandler``) extend ``IOHandler`` with workflow-specific
output methods called after the graph completes.

See ``DESIGN.md`` in this directory for the full event flow.
"""

import enum
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from langgraph.errors import GraphBubbleUp
from langgraph.types import Interrupt

from graphcore.tools.human import Question
from graphcore.tools.vfs import VFSAccessor

from composer.diagnostics.stream import ProgressUpdate
from composer.human.types import HumanInteractionType
from composer.core.state import ResultStateSchema, AIComposerState


if TYPE_CHECKING:
    class InterruptId(str):
        """LangGraph's id for a pending interrupt: the task that raised it, not
        the question it carries. A resume map is keyed by these; a mailbox is
        keyed by ``QuestionId``. Nominally distinct so neither can stand in for
        the other. A plain ``str`` at runtime."""
        ...
else:
    InterruptId = str


class GraphSuspended(GraphBubbleUp):
    """Raised by ``InterruptHandler.handle_interrupts`` when the answers will not
    arrive in this process. The interrupted thread keeps its tip checkpoint
    with the interrupts pending; the next process re-runs the parent from ITS
    checkpoint, reaches the same interrupts, and asks the handler again, which
    by then has the answers. Resumption is the handler's concern, never the
    runner's.

    A ``GraphBubbleUp`` on purpose: LangGraph neither retries it nor counts it
    as a task failure, so raised from inside a tool it lets the sibling tool
    tasks of that turn finish and persist their writes, and leaves the parent
    graph's checkpoint where it was. The retry floors and the durable-thread
    exhaustion marking pass it through untouched for the same reason.
    """

    def __init__(self, interrupts: Sequence[Interrupt]) -> None:
        super().__init__(f"suspended on {len(interrupts)} interrupt(s)")
        self.interrupts: tuple[Interrupt, ...] = tuple(interrupts)


class IOHandler(Protocol):
    """Protocol for consuming graph execution events.

    One ``IOHandler`` is active per ``with_handler()`` scope.  The
    background drainer calls its methods as events arrive from the
    ``EventQueue``.

    The ``path`` parameter on most methods is a list of thread IDs
    from outermost to innermost, reconstructed from ``Nested``
    event wrappers.  A top-level execution has ``len(path) == 1``.
    """

    async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str): ...

    async def log_state_update(self, path: list[str], st: dict):
        """A graph node emitted new state (messages, tool results, etc.)."""
        ...

    async def log_start(self, *, path: list[str], description: str, tool_id: str | None):
        """Graph execution began.  ``tool_id`` is set when the graph runs inside a tool call."""
        ...

    async def log_end(self, path: list[str]):
        """Graph execution ended (success or failure)."""
        ...

def io_handler[T: type[IOHandler]](t: T) -> T:
    """Marks a class as an ``IOHandler`` so the type checker verifies it
    satisfies the protocol. Identity at runtime."""
    return t

class InterruptHandler(Protocol):
    """Input: answers the interrupts a graph pauses on."""

    async def handle_interrupts(
        self, interrupts: Sequence[Interrupt], state: Any, *, thread_id: str
    ) -> Mapping[InterruptId, str]:
        """A person's text reply for each interrupt, keyed by ``Interrupt.id``.
        Interrupts left out stay pending and are asked again. Raise
        :class:`GraphSuspended` when the answers will not arrive in this process.

        ``thread_id`` is the thread that paused: the one whose checkpoints will
        carry the answers once applied. A handler that records the questions
        outside the process records it too, so a later execution knows where
        the evidence of consumption lives.

        Called synchronously from ``run_graph()`` (not from the drainer) — the
        graph pauses until this returns.
        """
        ...


class StateObserver(Protocol):
    """Sees the output events an input handler may need: state updates and
    checkpoints, per thread path. Delivered by :func:`observed` from the same
    sequential drainer that feeds the ``IOHandler``, in the same order."""

    async def on_state_update(self, path: list[str], st: dict) -> None: ...

    async def on_checkpoint(self, path: list[str], checkpoint_id: str) -> None: ...


class RefuseInterrupts:
    """The ``InterruptHandler`` for a run whose graphs never ask a person anything."""

    async def handle_interrupts(
        self, interrupts: Sequence[Interrupt], state: Any, *, thread_id: str
    ) -> Mapping[InterruptId, str]:
        raise RuntimeError(
            "unexpected interrupt(s) in a run with no human interaction: "
            f"{[intr.value for intr in interrupts]!r}"
        )

@io_handler
class ObservedIOHandler:
    """An ``IOHandler`` that forwards updates and checkpoints to observers after
    delegating everything to the inner handler."""

    def __init__(self, inner: IOHandler, observers: Sequence[StateObserver]) -> None:
        self._inner = inner
        self._observers = tuple(observers)

    async def log_checkpoint_id(self, *, path: list[str], checkpoint_id: str):
        await self._inner.log_checkpoint_id(path=path, checkpoint_id=checkpoint_id)
        for observer in self._observers:
            await observer.on_checkpoint(path, checkpoint_id)

    async def log_state_update(self, path: list[str], st: dict):
        await self._inner.log_state_update(path, st)
        for observer in self._observers:
            await observer.on_state_update(path, st)

    async def log_start(self, *, path: list[str], description: str, tool_id: str | None):
        await self._inner.log_start(path=path, description=description, tool_id=tool_id)

    async def log_end(self, path: list[str]):
        await self._inner.log_end(path)


def observed(inner: IOHandler, *observers: StateObserver) -> IOHandler:
    """``inner``, with ``observers`` told about its updates and checkpoints. With
    no observers it is ``inner`` itself."""
    return ObservedIOHandler(inner, observers) if observers else inner


class HumanInteractionBridge[H]:
    """``InterruptHandler`` for a UI that puts questions to a person one at a
    time through ``human_interaction``: each pending interrupt's payload is
    asked in turn and the answers are keyed by interrupt id. ``H`` is the
    payload type the UI knows how to ask. A class mixing this in is usually the
    run's ``IOHandler`` too, and is passed as both."""

    async def human_interaction(self, ty: H) -> str:
        raise NotImplementedError

    async def handle_interrupts(
        self, interrupts: Sequence[Interrupt], state: Any, *, thread_id: str
    ) -> Mapping[InterruptId, str]:
        # A person at the console does not care which tool call, or thread, asked.
        return {
            InterruptId(intr.id): await self.human_interaction(
                cast(H, intr.value.payload if isinstance(intr.value, Question) else intr.value)
            )
            for intr in interrupts
        }


class WorkflowPurpose(enum.Enum):
    """Identifies which sub-workflow a thread belongs to."""
    CODEGEN = "codegen"
    NATREQ = "natreq"


class CodeGenIOHandler(IOHandler, InterruptHandler, Protocol):
    """Extended handler for the code-generation workflow."""

    async def log_workflow_thread(self, purpose: WorkflowPurpose, thread_id: str) -> None:
        """Record a thread ID for a specific sub-workflow purpose."""
        ...

    async def show_error(self, error: Exception) -> None:
        """Display a fatal workflow error (crash, recursion limit, etc.)."""
        ...


    async def progress_update(self, path: list[str], upd: ProgressUpdate) -> None:
        """Domain-specific progress notification.  Called directly, not via the event queue."""
        ...


    async def output(
        self,
        res: ResultStateSchema,
        mat: VFSAccessor[AIComposerState],
        st: AIComposerState
    ): ...
