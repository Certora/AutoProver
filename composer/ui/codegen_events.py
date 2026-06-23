from typing import cast, override

from composer.io.event_handler import EventHandler
from composer.io.protocol import CodeGenIOHandler

from composer.diagnostics.stream import (
    PartialUpdates, SummarizationNotice,
)
from composer.diagnostics.handlers import is_user_update


class CodeGenEventHandler(EventHandler):
    def __init__(
        self,
        io: CodeGenIOHandler,
    ):
        self._io = io

    @override
    async def handle_event(self, payload: dict, path: list[str], checkpoint_id: str) -> None:
        d = cast(PartialUpdates, payload)
        if d["type"] == "summarization_raw":
            notice: SummarizationNotice = {"type": "summarization_notice", "summary": d["summary"]}
            await self._io.progress_update(path, notice)
        elif is_user_update(d):
            await self._io.progress_update(path, d)

    @override
    async def handle_progress_event(self, payload: dict) -> None:
        pass
