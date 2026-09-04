"""``ap-trail ls`` — list recent runs for the current user."""

import argparse
import asyncio
from datetime import datetime

from rich.console import Console
from rich.table import Table

from composer.io.run_index import list_runs
from composer.io.thread_logging import Run
from composer.workflow.services import store_context
from .uid_bind import bind_uid_args


def add_arguments(parser: argparse.ArgumentParser) -> None:
    bind_uid_args(parser)
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of runs to show (default 20).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 timestamp; only runs started after this point are shown.",
    )


def _fmt_duration(start: str, end: str | None) -> str:
    if end is None:
        return "--"
    try:
        d_start = datetime.fromisoformat(start)
        d_end = datetime.fromisoformat(end)
    except ValueError:
        return "?"
    delta = d_end - d_start
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_tags(run: Run) -> str:
    if not run.tags:
        return "(none)"
    return " ".join(f"{k}={v}" for k, v in run.tags.items())


_STATUS_STYLES = {"in-progress": "yellow", "completed": "green", "failed": "red", "awaiting_input": "magenta"}


def _fmt_start(start: str) -> str:
    try:
        return datetime.fromisoformat(start).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return start


async def _main(args: argparse.Namespace) -> int:
    async with store_context() as store:
        runs = await list_runs(store, uid=args.uid, limit=args.limit, since=args.since)

    if not runs:
        print("No runs found.")
        return 0

    table = Table(show_header=True, header_style="bold")
    table.add_column("run_id", style="cyan", no_wrap=True)
    table.add_column("started", no_wrap=True)
    table.add_column("duration", no_wrap=True, justify="right")
    table.add_column("execs", no_wrap=True, justify="right")
    table.add_column("status", no_wrap=True)
    table.add_column("tags")

    for run in runs:
        status = run.status
        status_style = _STATUS_STYLES.get(status, "white")
        table.add_row(
            run.run_id,
            _fmt_start(run.start_time),
            _fmt_duration(run.start_time, run.end_time),
            str(len(run.executions)),
            f"[{status_style}]{status}[/{status_style}]",
            _fmt_tags(run),
        )

    Console().print(table)
    return 0


def main(args: argparse.Namespace) -> int:
    return asyncio.run(_main(args))
