"""Generic frontend for a Rust application.

Both frontends are thin, descriptor-driven subclasses of the shared bases:

* :class:`GenericRustApp` — a ``MultiJobApp`` TUI whose phase labels / section
  order come from the descriptor.
* :class:`GenericRustConsoleHandler` — the stdout ``HandlerFactory``.

Domain-event rendering is *data-driven* by the descriptor's ``event_kinds``: a
Rust ``Command::Emit`` becomes a ``{"type": kind, ...}`` custom-stream payload,
which the handler writes to the task's log if ``kind`` is a declared event kind.
No per-application Python subclass is needed — the same generic handler renders
any Rust app's events (see ``docs/rust-applications.md`` §4.4).
"""

import json
from collections.abc import Set as AbstractSet
from typing import Any, override

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, RichLog

from composer.io.event_handler import EventHandler, NullEventHandler
from composer.io.multi_job import TaskInfo
from composer.ui.multi_console_handler import MultiJobConsoleHandler
from composer.ui.multi_job_app import MultiJobApp, MultiJobTaskHandler, TaskHost
from composer.ui.tool_display import ToolDisplayConfig


def _render_event(payload: dict) -> str:
    """A one-line rendering of an emit payload: prefer a ``line`` field, else a
    compact JSON of everything but the discriminating ``type``."""
    if isinstance(payload.get("line"), str):
        return payload["line"]
    rest = {k: v for k, v in payload.items() if k != "type"}
    return json.dumps(rest) if rest else ""


# Glyph for a notice event that carries a neutral ``outcome`` (e.g. a verdict). Mirrors
# the report/TUI GOOD→✓ / BAD→✗ vocabulary so a result scans at a glance.
_OUTCOME_GLYPH: dict[str, str] = {
    "GOOD": "✓",  # ✓
    "BAD": "✗",  # ✗
    "TIMEOUT": "⧖",  # ⧖
    "ERROR": "!",
    "UNKNOWN": "?",
}


def _notice_headline(payload: dict) -> str:
    """The persistent-callout headline for a notice event: its one-line rendering,
    prefixed with an outcome glyph when the payload carries one."""
    body = _render_event(payload)
    glyph = _OUTCOME_GLYPH.get(payload.get("outcome", "")) if isinstance(payload.get("outcome"), str) else None
    return f"{glyph} {body}" if glyph else body


class GenericRustTaskHandler(MultiJobTaskHandler[None], NullEventHandler):
    """Per-task handler that streams the app's declared domain events into a
    collapsible log under the task panel."""

    def __init__(
        self,
        task_id: str,
        label: str,
        panel: VerticalScroll,
        host: TaskHost,
        tool_config: ToolDisplayConfig,
        event_kinds: set[str],
        notice_kinds: AbstractSet[str] = frozenset(),
    ):
        super().__init__(task_id, label, panel, host, tool_config)
        self._event_kinds = event_kinds
        # Notice kinds are surfaced as a persistent callout (post_notice) instead of a
        # buried log line; ``event_kinds`` here is the streaming (non-notice) remainder.
        self._notice_kinds = notice_kinds
        self._event_log: RichLog | None = None

    def format_hitl_prompt(self, ty: None) -> list[Text | str]:
        raise NotImplementedError("Rust applications do not use HITL interrupts")

    async def _ensure_event_log(self) -> RichLog:
        if self._event_log is None:
            log = RichLog(highlight=True, markup=False)
            log.styles.min_height = 12
            self._event_log = log
            await self._mount_to(self._panel, Collapsible(log, title="Events"))
        return self._event_log

    @override
    async def handle_event(self, payload: dict, path: list[str], checkpoint_id: str) -> None:
        kind = payload.get("type")
        if kind in self._notice_kinds:
            # A one-shot important result — a persistent callout in the panel + a toast,
            # visible without expanding the events log.
            await self.post_notice(_notice_headline(payload))
        elif kind in self._event_kinds:
            log = await self._ensure_event_log()
            log.write(f"[{kind}] {_render_event(payload)}")


class GenericRustApp(MultiJobApp[Any, GenericRustTaskHandler]):
    """Textual TUI for a Rust application."""

    def __init__(
        self,
        *,
        phase_labels: dict[Any, str],
        section_order: list[str],
        header_text: str,
        event_kinds: set[str],
        notice_kinds: AbstractSet[str] = frozenset(),
    ):
        super().__init__(
            phase_labels=phase_labels, section_order=section_order, header_text=header_text
        )
        self._event_kinds = event_kinds
        self._notice_kinds = notice_kinds

    def create_task_handler(
        self, panel: VerticalScroll, info: TaskInfo[Any]
    ) -> GenericRustTaskHandler:
        return GenericRustTaskHandler(
            info.task_id, info.label, panel, self, ToolDisplayConfig(),
            self._event_kinds, self._notice_kinds,
        )

    def create_event_handler(
        self, handler: GenericRustTaskHandler, info: TaskInfo[Any]
    ) -> EventHandler:
        return handler


class GenericRustConsoleHandler(MultiJobConsoleHandler[Any]):
    """Stdout ``HandlerFactory`` for a Rust application."""

    def __init__(self, event_kinds: set[str]):
        super().__init__()
        self._event_kinds = event_kinds

    @override
    async def handle_event(self, payload: dict, path: list[str], checkpoint_id: str) -> None:
        kind = payload.get("type")
        if kind in self._event_kinds:
            self._output(f"[{self._label(path)}] {kind}: {_render_event(payload)}")

    @override
    async def handle_progress_event(self, payload: dict) -> None:
        # Rust applications stream everything through Command::Emit (the custom
        # channel); there are no progress-channel events.
        pass
