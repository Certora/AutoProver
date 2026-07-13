"""
Mixin for Textual Apps that support IDE bridge content viewing.

Provides content snapshot storage, clickable link generation, and
a show-file action with an overridable no-IDE fallback.

Used by both ``BaseRichConsoleApp`` (codegen) and ``PipelineApp``
(natspec pipeline).
"""

from typing import TYPE_CHECKING

from textual.widgets import Static

from rich.markup import escape
from rich.text import Text

if TYPE_CHECKING:
    from textual.app import App
    _Base = App
else:
    _Base = object


class FileContentMixin(_Base):
    """Mixin for Textual Apps with IDE bridge content viewing.

    Call ``_init_ide_content(ide)`` from your ``__init__``.

    Requires: host class inherits ``textual.app.App``.
    """

    _content_snapshots: dict[int, tuple[str, str, str]]
    _next_snap_id: int

    def _init_file_content(self) -> None:
        self._content_snapshots = {}
        self._next_snap_id = 0

    # ── Snapshot storage ─────────────────────────────────────

    def _store_snapshot(self, label: str, content: str, filename: str) -> int:
        """Store a content snapshot and return its ID."""
        snap_id = self._next_snap_id
        self._next_snap_id += 1
        self._content_snapshots[snap_id] = (label, content, filename)
        return snap_id

    # ── Link rendering ───────────────────────────────────────

    def _make_content_link_markup(self, snap_id: int, display_text: str) -> str:
        """Return Rich markup string for a clickable content link."""
        return (
            f"[@click=app.show_content({snap_id})]"
            f"[bold underline cyan]{escape(display_text)}[/bold underline cyan][/]"
        )

    def _make_content_link_widget(self, snap_id: int, prefix: str, display_text: str) -> Static:
        """Return a Static widget with a clickable content link."""
        link = self._make_content_link_markup(snap_id, display_text)
        return Static(f"[cyan]\u2022[/cyan] {escape(prefix)}: {link}", classes="content-link")

    def _make_content_text_widget(self, prefix: str, display_text: str) -> Static:
        """Return a non-clickable content label (no-IDE, no fallback)."""
        return Static(
            Text.assemble(
                ("\u2022 ", "cyan"),
                f"{prefix}: ",
                (display_text, "bold underline cyan"),
            ),
            classes="content-link",
        )

    # ── Action ───────────────────────────────────────────────

    def action_show_content(self, snap_id: int) -> None:
        """Textual action: show content for a snapshot."""
        snap = self._content_snapshots.get(snap_id)
        if snap is None:
            return
        label, content, filename = snap

        self._show_content_fallback(snap_id, label, content, filename)

    def _show_content_fallback(
        self, snap_id: int, label: str, content: str, filename: str
    ) -> None:
        """No-IDE fallback. Override in subclasses for app-specific behavior."""
        pass

    # ── Utilities ────────────────────────────────────────────

    @staticmethod
    def _guess_lang(filename: str) -> str | None:
        if filename.endswith(".sol"):
            return "solidity"
        elif filename.endswith(".json"):
            return "json"
        elif filename.endswith(".spec"):
            return "javascript"  # CVL ~ JS for syntax highlighting
        return None
