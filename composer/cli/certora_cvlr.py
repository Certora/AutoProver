"""``certora-cvlr``: the mechanical steps of the Solana skill, one subcommand each.

Output is plain text for the agent running the skill. Exit status is 0 on success, 1 when the
project is refused, with the reason on stderr, and 2 on a usage error.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, Built, SbfVerdict
from composer.cargo.session import Compiled, CompileFailed
from composer.prover.conf import ExcludeRules, InheritRules, RuleSelection, SelectRules
from composer.prover.core import ProverOptions, ProverReport, make_prover_options
from composer.prover.results import AlertReport, UnreadableAlertReport
from composer.spec.cvlr.conf import (
    DEFAULT_FEATURE,
    CheckVerdicts,
    CollectUnsatCore,
    OptimisticLoop,
    TunableConf,
)
from composer.spec.cvlr.externals import external_warnings
from composer.spec.cvlr.preflight import PreflightFailed
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.prover import BuildRejected, SubmissionFailed
from composer.spec.cvlr.scaffold import ScaffoldStale
from composer.spec.cvlr.skill import (
    INVARIANT_RULES,
    ComponentNameRejected,
    JobStarted,
    NoRules,
    NotSetUp,
    ProveRequest,
    Running,
    SetupResult,
    check,
    prove,
    setup,
    vacuity,
    wait,
)
from composer.spec.cvlr.verify import externals_note


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

    check_ = commands.add_parser(
        "check",
        help="Type-check one component's rules for the SBF target, without building or "
        "submitting. Seconds; run it after every edit.",
    )
    check_.add_argument("project_root", type=Path, help="The project setup was run on.")
    check_.add_argument("--package", help="As for setup.")
    check_.add_argument(
        "--component",
        required=True,
        metavar="MODULE",
        help=f"The rule module to check: a component setup declared, or {INVARIANT_RULES}.",
    )

    prove_ = commands.add_parser(
        "prove",
        help="Build one component's rules and submit them to the Certora Solana Prover. Prints the "
        "job link once the job is accepted; `wait` reads the verdicts.",
    )
    prove_.add_argument("project_root", type=Path, help="The project setup was run on.")
    prove_.add_argument("--package", help="As for setup.")
    prove_.add_argument(
        "--component",
        required=True,
        metavar="MODULE",
        help=f"The rule module to check: a component setup declared, or {INVARIANT_RULES}.",
    )
    selection = prove_.add_mutually_exclusive_group()
    selection.add_argument(
        "--rule", action="append", default=[], help="Check only this rule (a glob). Repeatable."
    )
    selection.add_argument(
        "--exclude-rule", action="append", default=[], help="Skip this rule (a glob). Repeatable."
    )
    prove_.add_argument(
        "--loop-iter",
        type=int,
        default=TunableConf.loop_iter,
        help="How many iterations of each loop the Prover unrolls (default %(default)s).",
    )
    prove_.add_argument(
        "--optimistic-loop",
        metavar="WHY",
        help="Assume every loop finishes within --loop-iter. Unsound: every verdict in the job "
        "then depends on it. Give the reason.",
    )
    prove_.add_argument("--msg", help="The job's message on the Prover dashboard.")
    prove_.add_argument(
        "--unsat-core",
        action="store_true",
        help="Rerun the one rule --rule names with the vacuity check off and unsat cores on, for "
        "`vacuity` to read. For a rule reported SANITY_FAILED.",
    )

    wait_ = commands.add_parser(
        "wait",
        help="Wait a bounded time for a prove job. Prints RUNNING and the job status if it has "
        "not finished, else every rule's verdict.",
    )
    wait_.add_argument("link", help="The job link prove printed.")
    wait_.add_argument(
        "--for",
        dest="seconds",
        type=float,
        default=90,
        metavar="SECONDS",
        help="How long to wait before printing RUNNING (default %(default)s).",
    )

    vacuity_ = commands.add_parser(
        "vacuity",
        help="Print the unsat core of a rule from a finished `prove --unsat-core` job, and whether "
        "the rule's own assertion is in it.",
    )
    vacuity_.add_argument("link", help="The job link prove printed.")
    vacuity_.add_argument("--rule", required=True, help="The rule reported SANITY_FAILED.")
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


def _sandbox() -> SandboxConfig:
    return SandboxConfig.from_env(extra_ro=(PLATFORM_TOOLS_ROOT,))


async def _setup(args: argparse.Namespace) -> int:
    try:
        result = await setup(
            args.project_root.resolve(),
            package=args.package,
            components=args.component,
            sandbox=_sandbox(),
            check=args.check,
        )
    except (PreflightFailed, ScaffoldStale, ComponentNameRejected) as exc:
        print(f"certora-cvlr setup: {exc}", file=sys.stderr)
        return 1
    print(_report_setup(result))
    return 0


async def _check(args: argparse.Namespace) -> int:
    try:
        verdict = await check(
            args.project_root.resolve(), args.component, package=args.package, sandbox=_sandbox()
        )
    except (PreflightFailed, NotSetUp) as exc:
        print(f"certora-cvlr check: {exc}", file=sys.stderr)
        return 1
    match verdict:
        case Compiled():
            print("ok")
            return 0
        case CompileFailed(diagnostics=diagnostics):
            print(diagnostics)
            return 1


def _rule_selection(args: argparse.Namespace) -> RuleSelection:
    if args.rule:
        return SelectRules(tuple(args.rule))
    if args.exclude_rule:
        return ExcludeRules(tuple(args.exclude_rule))
    return InheritRules()


def _prover_options() -> ProverOptions:
    return make_prover_options(cloud=True, app="solana")


async def _prove(args: argparse.Namespace) -> int:
    if args.unsat_core and len(args.rule) != 1:
        print("certora-cvlr prove: --unsat-core needs exactly one --rule", file=sys.stderr)
        return 2
    request = ProveRequest(
        component=args.component,
        rules=_rule_selection(args),
        settings=TunableConf(
            loop_iter=args.loop_iter,
            optimistic_loop=(
                OptimisticLoop(args.optimistic_loop) if args.optimistic_loop is not None else None
            ),
        ),
        msg=args.msg,
        purpose=CollectUnsatCore() if args.unsat_core else CheckVerdicts(),
    )
    try:
        outcome = await prove(
            args.project_root.resolve(),
            request,
            package=args.package,
            sandbox=_sandbox(),
            prover_opts=_prover_options(),
            on_waiting=lambda: print(
                "waiting for another prove of this workspace to be accepted", file=sys.stderr
            ),
        )
    except (PreflightFailed, NotSetUp) as exc:
        print(f"certora-cvlr prove: {exc}", file=sys.stderr)
        return 1
    match outcome:
        case JobStarted(link=link, rules=rules, summaries=summaries, inert_summaries=inert):
            print(f"rules: {', '.join(rules)}")
            if summaries is not None:
                print(f"summaries: {summaries}")
            if inert is not None:
                print(inert)
            print(f"submitted: {link}")
            return 0
        case NoRules(reason=reason):
            print(f"nothing submitted: {reason}")
            return 1
        case BuildRejected(build=build):
            print(f"build failed, nothing submitted:\n{_diagnostics(build.verdict)}")
            return 1
        case SubmissionFailed(reason=reason):
            print(f"submission failed:\n{reason}")
            return 1


def _diagnostics(verdict: SbfVerdict) -> str:
    match verdict:
        case CompileFailed(diagnostics=diagnostics):
            return diagnostics
        case Built():
            return "(the build succeeded)"


async def _wait(args: argparse.Namespace) -> int:
    outcome = await wait(args.link, seconds=args.seconds, prover_opts=_prover_options())
    match outcome:
        case Running(status=status):
            print(f"RUNNING {status}")
            return 0
        case ProverReport():
            print(f"FINISHED {outcome.link}")
            for rule, status in sorted(outcome.rule_outcomes().items()):
                print(f"  {status} {rule}")
            print()
            print(outcome.result_str)
            match outcome.alerts:
                case UnreadableAlertReport():
                    print(
                        "\nThe alert report could not be read, so the functions this job treated "
                        "as external are unknown."
                    )
                case AlertReport() as alerts:
                    if (note := externals_note(external_warnings(alerts).messages)) is not None:
                        print(f"\n{note}")
            return 0
        case str():
            print(f"FAILED\n{outcome}")
            return 1


async def _vacuity(args: argparse.Namespace) -> int:
    read = await vacuity(args.link, args.rule)
    if read is None:
        print(
            f"The job wrote no unsat core for {args.rule}. Either it was not a "
            "`prove --unsat-core` job, or the rule did not verify with the vacuity check off "
            "(`wait` shows its verdict), or the Prover settled the rule without a solver, which "
            "writes no core: assumptions that contradict each other directly, such as two bounds "
            "on one value, are removed before solving. Read the rule's assumptions for that first."
        )
        return 1
    print(read.finding.summary())
    print(f"\nUnsat core data:\n{read.core}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    match args.command:
        case "setup":
            return asyncio.run(_setup(args))
        case "check":
            return asyncio.run(_check(args))
        case "prove":
            return asyncio.run(_prove(args))
        case "wait":
            return asyncio.run(_wait(args))
        case "vacuity":
            return asyncio.run(_vacuity(args))
        case _:
            raise AssertionError(args.command)
