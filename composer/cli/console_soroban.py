"""Entry point for the CVLR rule author (Soroban) — console (no TUI) mode."""

import asyncio

import composer.bind as _

from composer.cli.cvlr_frontend import run_console
from composer.spec.cvlr.chains import SOROBAN_CVLR


def main() -> int:
    return asyncio.run(run_console(SOROBAN_CVLR))
