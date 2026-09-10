"""Entry point for the CVLR rule author (Solana) — console (no TUI) mode."""

import asyncio

import composer.bind as _

from composer.cli.cvlr_frontend import run_console
from composer.spec.cvlr.chains import SOLANA_CVLR


def main() -> int:
    return asyncio.run(run_console(SOLANA_CVLR))
