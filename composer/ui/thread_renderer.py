"""Textual widget that renders a thread's timeline as a scrollable, collapsible
trace. Consumed by ``snapshot-viewer`` (one timeline = one app) and by
``ap-trail view`` (one timeline per thread segment, swapped via ContentSwitcher).
"""

import json
from typing import Awaitable, Callable

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static, Collapsible

from rich.console import Group
from rich.text import Text
from rich.syntax import Syntax

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from composer.io.thread_timeline import SummarizationMarker, TimelineItem
from composer.ui.content import normalize_content


def _first_line(s: str, max_len: int = 100) -> str:
    for line in s.splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > max_len:
                return stripped[:max_len] + "..."
            return stripped
    return "(empty)"


def _compact_args(args: dict, max_len: int = 80) -> str:
    parts = []
    for k, v in args.items():
        if isinstance(v, str):
            shown = v if len(v) <= 30 else v[:27] + "..."
            parts.append(f'{k}="{shown}"')
        elif isinstance(v, (int, float, bool)):
            parts.append(f"{k}={v}")
        elif isinstance(v, list):
            parts.append(f"{k}=[{len(v)} items]")
        elif isinstance(v, dict):
            parts.append(f"{k}={{...}}")
        else:
            parts.append(f"{k}=...")
    result = ", ".join(parts)
    if len(result) > max_len:
        return result[:max_len] + "..."
    return result


class LazyStateView(Collapsible):
    """Per-turn view of a checkpoint's non-message state channels.

    The channel values are fetched only on the first expansion (one
    ``aget_tuple`` for the single checkpoint), so building a thread's
    timeline never reads state for turns the user doesn't open. Wired up
    only in live-run mode; replay/export sources pass no loader and no
    state view is rendered.
    """

    def __init__(
        self,
        *,
        checkpoint_id: str,
        loader: Callable[[str], Awaitable[dict[str, object] | None]],
        **kwargs,
    ):
        self._body = Static("(expand to load state)", markup=False)
        super().__init__(self._body, title="  ⚙ state channels", collapsed=True, **kwargs)
        self._checkpoint_id = checkpoint_id
        self._loader = loader
        self._loaded = False

    async def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._body.update("loading…")
        channel_values = await self._loader(self._checkpoint_id)
        if channel_values is None:
            self._body.update("(state unavailable)")
            return
        channels = {k: v for k, v in channel_values.items() if k != "messages"}
        if not channels:
            self.title = "  ⚙ state channels (none)"
            self._body.update("(no non-message channels)")
            return
        self.title = f"  ⚙ state channels ({len(channels)})"
        renderables: list = []
        for key in sorted(channels):
            renderables.append(Text(key, style="bold cyan"))
            dumped = json.dumps(channels[key], indent=2, default=str)
            renderables.append(Syntax(dumped, "json", theme="monokai", word_wrap=True))
        self._body.update(Group(*renderables))


class DescendableToolCall(Collapsible):
    """Tool-call collapsible whose id is known to spawn a sub-thread.
    A click outside the toggle area fires ``on_descend(tool_call_id)``."""

    def __init__(
        self,
        *args,
        tool_call_id: str,
        on_descend: Callable[[str], Awaitable[None]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._tcid = tool_call_id
        self._on_descend = on_descend

    async def action_descend(self) -> None:
        await self._on_descend(self._tcid)


class ThreadRenderer(VerticalScroll):
    """Renders a ``list[(TimelineItem, checkpoint_id)]`` as a scrolling
    sequence of collapsible widgets.

    When ``descendable_tool_call_ids`` contains a given tool_call's id, that
    call's collapsible is rendered with a "↘" affordance and clicking the
    affordance fires ``on_tool_descend(tool_call_id)``. Snapshot-viewer passes
    an empty set + None callback for no drill-down.

    When ``on_view_state`` is supplied, each turn gets a collapsible that
    lazily loads the checkpoint's non-message state channels on first expand
    (live-run mode only). Replay/export and snapshot-viewer pass None.
    """

    DEFAULT_CSS = """
    ThreadRenderer { height: 1fr; padding: 0 2; }
    ThreadRenderer > * { margin-bottom: 1; }
    ThreadRenderer .turn-header { margin-top: 1; }
    ThreadRenderer .turn-header:hover { background: $accent 30%; }
    ThreadRenderer .turn-state { margin-left: 2; color: $text-muted; }
    ThreadRenderer .tool-call { margin-left: 2; }
    ThreadRenderer .tool-result { margin-left: 2; }
    ThreadRenderer .ai-text { margin-left: 2; color: #6699cc; }
    ThreadRenderer Collapsible { background: transparent; border: none; padding: 0; }
    ThreadRenderer CollapsibleTitle { padding: 0 1; }
    ThreadRenderer Collapsible Contents { padding: 0 0 0 3; }

    ThreadRenderer DescendableToolCall {
        background: $warning 15%;
        border-left: thick $warning;
    }
    ThreadRenderer DescendableToolCall > CollapsibleTitle {
        color: $warning;
        text-style: bold;
    }
    ThreadRenderer DescendableToolCall > CollapsibleTitle:focus {
        background: $warning 40%;
    }
    """

    BINDINGS = [
        Binding("up", "focus_prev", "Prev", show=False),
        Binding("down", "focus_next", "Next", show=False),
        Binding("k", "focus_prev", "Prev", show=False),
        Binding("j", "focus_next", "Next", show=False),
        Binding("pageup", "scroll_page_up", "Page up", show=False),
        Binding("pagedown", "scroll_page_down", "Page down", show=False),
    ]

    def action_focus_prev(self) -> None:
        self.screen.focus_previous()

    def action_focus_next(self) -> None:
        self.screen.focus_next()

    def __init__(
        self,
        timeline: list[tuple[TimelineItem, str | None]],
        *,
        descendable_tool_call_ids: set[str] | None = None,
        on_tool_descend: Callable[[str], Awaitable[None]] | None = None,
        on_view_state: Callable[[str], Awaitable[dict[str, object] | None]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._timeline = timeline
        self._descendable = descendable_tool_call_ids or set()
        self._on_tool_descend = on_tool_descend
        self._on_view_state = on_view_state

    def on_mount(self) -> None:
        self.mount_all(self._render_all())

    def set_timeline(self, timeline: list[tuple[TimelineItem, str | None]]) -> None:
        """Replace the rendered timeline. Used when refreshing live data."""
        self._timeline = timeline
        self.remove_children()
        self.mount_all(self._render_all())

    def _render_all(self) -> list:
        widgets: list = []

        # Pair tool results to calls by tool_call_id. Ids are unique across
        # summary epochs so pairing across them is fine.
        tool_results: dict[str, ToolMessage] = {}
        for item, _ in self._timeline:
            if isinstance(item, ToolMessage):
                tool_results[item.tool_call_id] = item

        turn = 0
        for idx, (item, ckpt_id) in enumerate(self._timeline):
            match item:
                case SummarizationMarker():
                    widgets.append(self._render_summarization(item, idx))
                case SystemMessage():
                    widgets.append(self._render_system(item, idx))
                case HumanMessage():
                    widgets.append(self._render_human(item, idx))
                case AIMessage():
                    turn += 1
                    widgets.extend(self._render_turn(item, idx, turn, ckpt_id, tool_results))
                case ToolMessage():
                    pass  # rendered inline with its AI message
                case _:
                    widgets.append(Static(Text(f"[{idx}] {type(item).__name__}", style="dim")))

        return widgets

    def _render_summarization(self, marker: SummarizationMarker, idx: int) -> Static:
        line = Text()
        line.append("─" * 60 + "\n", style="yellow")
        line.append(f"[{idx}] Summarization", style="bold yellow")
        line.append(
            f"  (post-summary checkpoint {marker.checkpoint_id[:16]}...)",
            style="dim",
        )
        line.append("\n" + "─" * 60, style="yellow")
        return Static(line)

    def _render_system(self, msg: SystemMessage, idx: int) -> Collapsible:
        content = msg.text()
        return Collapsible(
            Static(content, markup=False),
            title=f"[{idx}] System ({len(content):,} chars)",
            collapsed=True,
        )

    def _render_human(self, msg: HumanMessage, idx: int) -> Collapsible:
        content = msg.text()
        tag = getattr(msg, "display_tag", None)
        tag_label = f" [{tag}]" if tag else ""
        preview = _first_line(content)
        return Collapsible(
            Static(content, markup=False),
            title=f"[{idx}] Human{tag_label}: {preview}",
            collapsed=True,
        )

    def _render_turn(
        self,
        msg: AIMessage,
        idx: int,
        turn: int,
        ckpt_id: str | None,
        tool_results: dict[str, ToolMessage],
    ) -> list:
        widgets: list = []
        blocks = normalize_content(msg.content)
        n_tool_calls = len(msg.tool_calls) if msg.tool_calls else 0

        usage_str = ""
        if isinstance(msg.response_metadata, dict):
            u = msg.response_metadata.get("usage")
            if u:
                inp = u.get("input_tokens", 0)
                out = u.get("output_tokens", 0)
                cache_r = u.get("cache_read_input_tokens", 0)
                if inp or out:
                    parts = [f"in={inp:,}", f"out={out:,}"]
                    if cache_r:
                        parts.append(f"cached={cache_r:,}")
                    usage_str = f"  ({', '.join(parts)})"

        header = Text()
        header.append(f"Turn {turn}", style="bold blue")
        header.append(f"  [{idx}]", style="dim")
        if n_tool_calls:
            header.append(f"  {n_tool_calls} tool call(s)", style="dim")
        header.append(usage_str, style="dim")
        widgets.append(Static(header, classes="turn-header"))

        if self._on_view_state is not None and ckpt_id is not None:
            widgets.append(LazyStateView(
                checkpoint_id=ckpt_id,
                loader=self._on_view_state,
                classes="turn-state",
            ))

        for block in blocks:
            match block["type"]:
                case "thinking":
                    text = block.get("thinking", "")
                    widgets.append(Collapsible(
                        Static(text, markup=False),
                        title=f"  Thinking ({len(text):,} chars)",
                        collapsed=True,
                        classes="tool-call",
                    ))
                case "text":
                    text = block["text"]
                    if text.strip():
                        widgets.append(Static(Text(text, style="#6699cc"), classes="ai-text"))
                case "tool_use":
                    pass  # rendered below from msg.tool_calls
                case other:
                    widgets.append(Static(f"  [{other}]"))

        for tc in msg.tool_calls or []:
            name = tc["name"]
            args = tc.get("args", {})
            tc_id = tc.get("id") or "?"
            summary = _compact_args(args)

            descendable = tc_id in self._descendable and self._on_tool_descend is not None
            prefix = "▶▶ DRILL " if descendable else "  > "
            call_title = f"{prefix}{name}({summary})"
            args_str = json.dumps(args, indent=2, default=str)

            if descendable:
                assert self._on_tool_descend is not None
                coll: Collapsible = DescendableToolCall(
                    Static(Syntax(args_str, "json", theme="monokai")),
                    title=call_title,
                    collapsed=True,
                    classes="tool-call",
                    tool_call_id=tc_id,
                    on_descend=self._on_tool_descend,
                )
            else:
                coll = Collapsible(
                    Static(Syntax(args_str, "json", theme="monokai")),
                    title=call_title,
                    collapsed=True,
                    classes="tool-call",
                )
            widgets.append(coll)

            result_msg = tool_results.get(tc_id)
            if result_msg is not None:
                content = result_msg.text()
                status = getattr(result_msg, "status", "ok")
                preview = _first_line(content)
                result_title = f"  < {name}"
                if status != "ok":
                    result_title += f" [{status}]"
                result_title += f": {preview}"
                widgets.append(Collapsible(
                    Static(content, markup=False),
                    title=result_title,
                    collapsed=True,
                    classes="tool-result",
                ))

        return widgets
