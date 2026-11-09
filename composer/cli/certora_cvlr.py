"""``certora-cvlr``: the mechanical steps of the Solana skill, one subcommand each.

Output is plain text for the agent running the skill. Exit status is 0 on success, 1 when the
project is refused, with the reason on stderr, and 2 on a usage error.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.preflight import PreflightFailed
from composer.spec.cvlr.scaffold import ScaffoldStale
from composer.spec.cvlr.skill import ComponentNameRejected, SetupResult, setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="certora-cvlr", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    set_up = commands.add_parser(
        "setup",
        help="Scaffold a Solana program for CVLR rules, declare its rule modules, and write the "
        "CVLR reference text.",
    )
    set_up.add_argument("project_root", type=Path, help="The Cargo workspace or package.")
    set_up.add_argument(
        "--package",
        help="The package to verify, when the workspace has more than one with a library target.",
    )
    set_up.add_argument(
        "--component",
        action="append",
        default=[],
        metavar="MODULE",
        help="A component's rule module, as a snake_case name. Repeat for each. Every setup names "
        "every component: the list replaces the one before it.",
    )
    set_up.add_argument(
        "--no-check",
        dest="check",
        action="store_false",
        help="Do not compile the scaffolded package.",
    )
    return parser


def _report_setup(result: SetupResult) -> str:
    pre = result.preflight
    lines = [pre.describe(), ""]
    lines.append(f"package: {pre.package} at {result.package_root}")
    lines.append(f"rule modules: {result.specs_dir}")
    lines.append(f"  invariants.rs   compiled with --features {DEFAULT_FEATURE}")
    for m in result.modules:
        lines.append(f"  {m.artifact_file}   compiled with --features {DEFAULT_FEATURE},{m.feature}")
    lines += [f"created {path}" for path in result.created]
    lines += [f"declared feature {feature}" for feature in result.added_features]
    lines.append(f"reference: {result.reference_dir}")
    lines.append(
        f"compiled with --features {DEFAULT_FEATURE}: ok"
        if result.checked
        else "not compiled (--no-check)"
    )
    return "\n".join(lines)


async def _setup(args: argparse.Namespace) -> int:
    try:
        result = await setup(
            args.project_root.resolve(),
            package=args.package,
            components=args.component,
            sandbox=SandboxConfig.from_env(extra_ro=(PLATFORM_TOOLS_ROOT,)),
            check=args.check,
        )
    except (PreflightFailed, ScaffoldStale, ComponentNameRejected) as exc:
        print(f"certora-cvlr setup: {exc}", file=sys.stderr)
        return 1
    print(_report_setup(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    match args.command:
        case "setup":
            return asyncio.run(_setup(args))
        case _:
            raise AssertionError(args.command)
