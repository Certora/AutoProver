"""Generic Rust frontend event routing (no wheel / no running TUI).

A declared ``notice`` event kind (e.g. Crucible's per-invariant ``verdict``) must be
surfaced as a persistent callout via ``post_notice`` — not buried in the collapsible
events log — while ordinary kinds still stream to the log and undeclared kinds are
ignored. The notice headline carries an outcome glyph (✓/✗) when the payload has one.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from composer.rustapp.frontend import GenericRustTaskHandler, _notice_headline
from composer.ui.tool_display import ToolDisplayConfig


def test_notice_headline_prefixes_outcome_glyph():
    assert _notice_headline({"outcome": "GOOD", "line": "held"}) == "✓ held"
    assert _notice_headline({"outcome": "BAD", "line": "refuted"}) == "✗ refuted"


def test_notice_headline_without_outcome_is_plain_line():
    assert _notice_headline({"line": "building…"}) == "building…"


class _RecordingHandler(GenericRustTaskHandler):
    """Records where each event is routed, bypassing real textual mounting."""

    def __init__(self, event_kinds: set[str], notice_kinds: set[str]):
        super().__init__(
            "t", "Label", cast(Any, None), cast(Any, None), ToolDisplayConfig(),
            event_kinds, notice_kinds,
        )
        self.notices: list[str] = []
        self.logged: list[str] = []

    async def post_notice(self, headline, detail=None, *, toast=True):  # type: ignore[override]
        self.notices.append(headline if isinstance(headline, str) else headline.plain)

    async def _ensure_event_log(self):  # type: ignore[override]
        handler = self

        class _Log:
            def write(self, line: str) -> None:
                handler.logged.append(line)

        return _Log()


def _handle(handler: _RecordingHandler, payload: dict) -> None:
    asyncio.run(handler.handle_event(payload, ["t"], "cp"))


def test_notice_kind_routes_to_post_notice_not_log():
    h = _RecordingHandler(event_kinds={"fuzz_pulse", "verdict"}, notice_kinds={"verdict"})
    _handle(h, {"type": "verdict", "outcome": "BAD", "line": "counterexample found"})
    assert h.notices == ["✗ counterexample found"]
    assert h.logged == []


def test_streaming_kind_routes_to_log_not_notice():
    h = _RecordingHandler(event_kinds={"fuzz_pulse", "verdict"}, notice_kinds={"verdict"})
    _handle(h, {"type": "fuzz_pulse", "line": "fuzzing…"})
    assert h.logged == ["[fuzz_pulse] fuzzing…"]
    assert h.notices == []


def test_undeclared_kind_is_ignored():
    h = _RecordingHandler(event_kinds={"verdict"}, notice_kinds={"verdict"})
    _handle(h, {"type": "mystery", "line": "nope"})
    assert h.notices == [] and h.logged == []
