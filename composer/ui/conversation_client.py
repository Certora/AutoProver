import asyncio

from composer.io.conversation import (
    ProgressPayload, AIYapping, ToolBatch, ToolComplete, ThinkingStart, StateUpdate, HumanPrompt
)
from composer.io.stream import managed_streamer, AsyncDataQueue, ManagedQueue, EndConversation, Checkpoint
from rich.console import Console, RenderableType
from rich.status import Status
from rich.markdown import Markdown

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML

class ConsoleConversationClient():
    def __init__(
        self, init_msg: RenderableType
    ):
        self.init_msg = init_msg
        self.ev_queue : ManagedQueue[ProgressPayload] = AsyncDataQueue(asyncio.Event(), [])
        self._thinking_item : Status | None = None
        self._console = Console()
        self.drain_task : asyncio.Task[None]

    def _reset_thinking(self):
        if self._thinking_item is not None:
            self._thinking_item.stop()
            self._thinking_item = None

    async def _update(
        self, r: ProgressPayload
    ):
        match r:
            case ThinkingStart():
                if self._thinking_item is None:
                    self._thinking_item = self._console.status("Thinking...")
                    self._thinking_item.start()
            case ToolComplete():
                pass
            case AIYapping():
                self._reset_thinking()
                self._console.print(r.yap_content, markup=False, style="italic dim")
            case ToolBatch():
                print(f"AI called: {", ".join([ t['name'] for t in r.calls ])}")
            case StateUpdate():
                self._reset_thinking()
                self._console.print(r.state_display, markup=False)

    def progress_update(
        self, progress: ProgressPayload
    ):
        self.ev_queue.push(progress)

    async def answer_applied(self, question_id: str) -> None:
        pass  # the person answered at this console; nothing to retire elsewhere

    async def human_turn(self, prompt: HumanPrompt, state: RenderableType | None) -> str:
        self._reset_thinking()
        ev = asyncio.Event()
        self.ev_queue.push(Checkpoint(ev))
        await ev.wait()
        if prompt.ai_message is not None:
            self._console.print(Markdown(prompt.ai_message))
        while True:
            text = await self._read_line()
            # A console command, not a reply: show the current state and ask again.
            if text.strip() == "/list" and state is not None:
                self._console.print(state, markup=False)
                continue
            return text

    async def _read_line(self) -> str:
        multiline = False

        @Condition
        def is_multiline():
            return multiline

        kb = KeyBindings()

        @kb.add("c-e")  # Ctrl+E to toggle
        def _toggle(event):
            nonlocal multiline
            multiline = not multiline

        session = PromptSession()
        text = await session.prompt_async(
            ">>> ",
            multiline=is_multiline,
            key_bindings=kb,
            bottom_toolbar=lambda: HTML(
                "<b>Ctrl+E</b> multiline: <b>{}</b>{}".format(
                    "ON" if multiline else "OFF",
                    "  |  <b>Alt+Enter</b> to submit" if multiline else "",
                )
            ),
        )
        return text

    async def __aenter__(self):
        self.drain_task = managed_streamer(
            self.ev_queue, self._update
        )
        print("--- Entering refinement conversation (all other output suppressed) ---")
        self._console.print(self.init_msg)

    async def __aexit__(self, exc_type, exc, tb):
        self.ev_queue.push(EndConversation())
        try:
            await self.drain_task
        except Exception:
            print("Conversation cleanup failed")
