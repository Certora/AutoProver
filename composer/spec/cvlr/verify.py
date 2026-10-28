"""The gate tools.

``cargo_check`` compiles the draft on the host and is not a stamp.
``verify_rules`` builds it for the chain, submits the rules the build listed,
and stamps the draft when every one of them is accounted for. A rule marked
as an expected failure is left out of that check. A build that fails
reports the compiler error and submits nothing
(:class:`~composer.spec.cvlr.prover.BuildRejected`).
"""

import asyncio
from dataclasses import dataclass, replace
import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Mapping, TypedDict, override

from elftools.common.exceptions import ELFError
from langchain_core.tools import BaseTool

from graphcore.graph import tool_return, tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.state import ValidationStamper
from composer.cargo.features import CargoFeature
from composer.cargo.sbf import Built, SbfRun
from composer.cargo.session import CargoSession, CompileFailed, CompileRun, Compiled
from composer.cargo.symbols import SymbolsUnavailable, nearest, prover_function_names, unmatched
from composer.prover.core import (
    CexHandler,
    CexProgressCallbacks,
    ProverOptions,
)
from composer.prover.accounting import RunAccounting
from composer.prover.ptypes import LoopUnwound, RuleResult, StatusCodes
from composer.prover.results import AlertReport, UnreadableAlertReport
from composer.prover.conf import SelectRules
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    CvlrOutcome,
    Prepared,
    Submission,
    SubmissionFailed,
    build_for_submission,
    run_submission,
    write_submission,
)
from composer.spec.cvlr.author_tools import (
    AdjustProverConfig,
    SummarizeForProver,
    stamp_under,
    unaccounted,
)
from composer.spec.cvlr.externals import ExternalWarnings, external_warnings
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.incomplete import GeneratedAssertion, OverflowUnproven, incomplete_checks
from composer.spec.cvlr.manifest import ManifestCorrupt, ManifestMissing, rule_names_in_elf
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.cvlr.state import (
    PROVER_VALIDATION_KEY,
    CvlrGenerationState,
    LastVerdicts,
    tuning_history,
)
from composer.spec.cvlr.tree import SharedTree, UnitEdits
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.spec.gen_types import TypedTemplate
from composer.templates.loader import load_jinja_template
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)


class _InertSummary(TypedDict):
    pattern: str
    suggestions: tuple[str, ...]


class _InertSummariesParams(TypedDict):
    #: How many directives the unit has, of which ``missed`` matched no symbol.
    total: int
    missed: list[_InertSummary]


_InertSummaries = TypedTemplate[_InertSummariesParams]("cvlr/inert_summaries.j2")


class _CheckedRule(TypedDict):
    name: str
    assertion: str


class _IncompleteCheckParams(TypedDict):
    rules: list[_CheckedRule]
    #: One rendering of ``cvlr/incomplete_advice.j2`` per kind of generated assertion the run hit.
    advice: str


class _IncompleteAdviceParams(TypedDict):
    #: ``"loop"`` or ``"overflow"``, from :func:`_incomplete_kind`.
    kind: str


class _ExternalFunctionsParams(TypedDict):
    warnings: str


class _NamesParams(TypedDict):
    #: Rule names, comma-separated.
    names: str


_IncompleteCheck = TypedTemplate[_IncompleteCheckParams]("cvlr/incomplete_check.j2")
_IncompleteAdvice = TypedTemplate[_IncompleteAdviceParams]("cvlr/incomplete_advice.j2")
_ExternalFunctions = TypedTemplate[_ExternalFunctionsParams]("cvlr/external_functions.j2")
_ExpectedVerified = TypedTemplate[_NamesParams]("cvlr/expected_verified.j2")
_NoVerdict = TypedTemplate[_NamesParams]("cvlr/no_verdict.j2")
_Unaccounted = TypedTemplate[_NamesParams]("cvlr/unaccounted.j2")
_NoPropertyVerdict = TypedTemplate[_NamesParams]("cvlr/no_property_verdict.j2")


class BuildPermit:
    """Held from staging until the prover reports the job link, or until the slot ends.

    Staging goes through the permit, so no draft is staged without it.
    """

    def __init__(self, target: "HarnessTarget") -> None:
        self._target = target
        self._held = False

    async def acquire(self) -> None:
        await self._target.build_sem.acquire()
        self._held = True

    def release(self) -> None:
        """A second call does nothing."""
        if self._held:
            self._held = False
            self._target.build_sem.release()

    def require_held(self) -> None:
        if not self._held:
            raise RuntimeError("the build permit is not held")

    async def stage(
        self,
        draft: str,
        summaries: Sequence[SummaryDirective],
        overlay: Mapping[str, str],
    ) -> "StagedUnit":
        """Write the draft, the summaries, and this unit's program edits into the tree.

        ``summaries`` is rewritten even when empty, so a dropped directive leaves
        the file the conf names. Returns what builds the staged unit.
        """
        self.require_held()
        target = self._target
        target.tuning.write(tuple(summaries))
        await target.tree.reconcile(
            target.unit.module,
            UnitEdits(module_path=target.module_path, draft=draft, overlay=overlay),
        )
        return StagedUnit(self, target)


class StagedUnit:
    """A unit's draft in the tree, under the permit that staged it.

    Every build of the draft goes through it, so none runs before staging or
    after the permit is released, and all of them run in the target's session.
    """

    def __init__(self, permit: BuildPermit, target: "HarnessTarget") -> None:
        self._permit = permit
        self._target = target

    async def check(self) -> CompileRun:
        """Compile on the host target."""
        self._permit.require_held()
        target = self._target
        return await target.session.check(
            manifest_dir=target.package_dir, features=target.features
        )

    async def build_for_submission(self, submission: Submission) -> SbfRun:
        self._permit.require_held()
        return await build_for_submission(self._target.session, submission)

    async def prepare(self, build: SbfRun, submission: Submission, *, into: Path) -> Prepared:
        """Write the conf that verifies ``build``, bound to the session that built it."""
        self._permit.require_held()
        session = self._target.session
        return Prepared(session, build, await write_submission(session, submission, into=into))


@dataclass(frozen=True)
class HarnessTarget:
    """Where a draft is staged so the crate compiles with it.

    Staging writes the file ``specs/mod.rs`` declares for :attr:`unit`
    (:meth:`composer.spec.cvlr.harness.CvlrArtifactStore.declare_modules`).
    Every unit shares ``tree`` and ``build_sem``.
    """

    session: CargoSession
    package: str
    package_root: Path
    tuning: TuningFiles
    #: The module name the tree keys edits by, and the cargo feature that selects
    #: this unit.
    unit: HarnessModule
    tree: SharedTree
    build_sem: asyncio.Semaphore

    def __post_init__(self) -> None:
        # The conf names this file, including when the unit has no summaries yet.
        self.tuning.ensure_composite()

    @property
    def features(self) -> tuple[CargoFeature, ...]:
        """Features for every build of this unit: the harness, and this unit's module."""
        return (DEFAULT_FEATURE, self.unit.feature)

    @property
    def package_dir(self) -> Path:
        """The package directory relative to the session workdir.

        That relative path is how a build is told which package to compile.
        """
        return self.package_root.relative_to(self.session.workdir)

    @property
    def module_path(self) -> Path:
        return self.package_root / SPECS_DIR / self.unit.artifact_file

    @asynccontextmanager
    async def build_slot(self) -> AsyncIterator[BuildPermit]:
        """Serialize staging and the cargo invocation that follows it.

        Cargo's build-directory lock already serializes the builds. A build
        waiting on that lock still holds its sandbox grants, so the permit
        queues them here. A submission holds the permit longer
        (:func:`_stage_and_submit`).
        """
        permit = BuildPermit(self)
        await permit.acquire()
        try:
            yield permit
        finally:
            permit.release()


class _RunAccounting(RunAccounting):
    """Accounts for the run, and releases ``permit`` when the Prover reports the
    job's link, which is after the upload."""

    def __init__(self, permit: BuildPermit) -> None:
        super().__init__()
        self._permit = permit

    @override
    async def on_prover_link(self, link: str) -> None:
        self._permit.release()
        await super().on_prover_link(link)


@dataclass(frozen=True)
class VerifyDeps:
    """What ``verify_rules`` needs beyond the draft."""

    target: HarnessTarget
    submission: Submission
    #: The directory :func:`composer.spec.cvlr.prover.write_submission` writes into.
    #: Outside the tree. The confined build can write the tree, so the conf and
    #: the build script must not live there.
    submission_dir: Path
    prover_opts: ProverOptions
    stamper: ValidationStamper

    @property
    def unit_submission(self) -> Submission:
        """The unit's submission, compiling this unit's module.

        :attr:`HarnessTarget.features` is ``certora`` plus this unit's feature, the
        same set ``cargo_check`` builds with. The submission's own default is only
        ``certora``, and ``specs/mod.rs`` gates this unit's module on its feature,
        so a build that kept the default would compile the staged file out.
        """
        return replace(self.submission, features=self.target.features)

    def submission_for(self, state: CvlrGenerationState, rules: Sequence[str]) -> Submission:
        """The unit's submission, checking ``rules`` under the current prover settings.

        The settings come from state. A submission built earlier would send the old
        settings, while :func:`~composer.spec.cvlr.conf.conf_history` is what the
        stamp records.
        """
        return replace(
            self.unit_submission,
            rules=SelectRules(tuple(rules)),
            settings=state["prover_settings"],
        )


def _incomplete_kind(stopped: GeneratedAssertion) -> str:
    match stopped:
        case LoopUnwound():
            return "loop"
        case OverflowUnproven():
            return "overflow"


def _incomplete_note(incomplete: Mapping[str, GeneratedAssertion]) -> str:
    ordered = sorted(incomplete.items())
    kinds = list(dict.fromkeys(_incomplete_kind(stopped) for _, stopped in ordered))
    return _IncompleteCheck.bind({
        "rules": [{"name": name, "assertion": stopped.assertion} for name, stopped in ordered],
        "advice": "\n".join(
            _IncompleteAdvice.bind({"kind": kind}).render_to(load_jinja_template) for kind in kinds
        ),
    }).render_to(load_jinja_template)


def _externals_note(externals: Sequence[str]) -> str | None:
    """Text for the author about functions this run treated as external, or None.

    ``externals`` is the Prover's own warning text. Not a gate. The starting
    configuration leaves most platform code external, and the author cannot add
    inlining directives.
    """
    if not externals:
        return None
    _log.info("prover treated functions as external:\n%s", "\n".join(externals))
    return _ExternalFunctions.bind({"warnings": "\n\n".join(externals)}).render_to(
        load_jinja_template
    )


def _unverdicted(declared: Sequence[str], status: Mapping[str, StatusCodes]) -> list[str]:
    """Rules the build listed that the report gave no verdict for.

    The verdict roll-up only sees rules the report names. A missing rule would
    otherwise leave "every rule is accounted for" true.
    """
    return sorted(set(declared) - status.keys())


def _names_note(template: TypedTemplate[_NamesParams], names: Sequence[str]) -> str:
    return template.bind({"names": ", ".join(names)}).render_to(load_jinja_template)


@tool_display("Compiling the harness", "Compile")
class CargoCheck(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Compile the current draft against the program, on the host target.

    Takes seconds and does not submit a job. Call it after every edit. It
    catches an unknown macro, a wrong signature, or a misused derive before a
    prover run.

    It does not catch errors only the chain build reports. It does not replace
    ``verify_rules``.
    """

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        with self.tool_deps() as target:
            async with target.build_slot() as permit:
                staged = await permit.stage(draft, self.state["summaries"], self.state["vfs"])
                run = await staged.check()
        match run.verdict:
            case Compiled():
                return tool_return(
                    self.tool_call_id,
                    f"Compiled in {run.duration_ms}ms.",
                )
            case CompileFailed(diagnostics=diagnostics):
                return tool_return(
                    self.tool_call_id,
                    f"Does not compile:\n{diagnostics}",
                )


def _unread(err: ManifestMissing | ManifestCorrupt) -> str:
    if isinstance(err, ManifestMissing):
        return (
            "The .so has no `.cvlr_rules` section, so the rules were not listed. "
            "Nothing was submitted."
        )
    if err.offset is None:
        return "The `.cvlr_rules` section could not be read. Nothing was submitted."
    return (
        f"The `.cvlr_rules` section could not be read at offset {err.offset}. "
        "Nothing was submitted."
    )


class RuleStatusReport(CexHandler):
    """Each rule's status and the prover's messages.

    ``flat_rule_feedback.j2`` says the analysis found nothing when a violated
    rule has no explanation. That sentence would describe an analysis that ran.
    """

    @override
    async def analyze(
        self,
        all_results: list[RuleResult],
        tool_call_id: str,
        callbacks: CexProgressCallbacks,
        report_dir: Path,
    ) -> str:
        lines: list[str] = []
        for rule in all_results:
            lines.append(f"{rule.name}: {rule.status}")
            for message in rule.error_messages or ():
                lines.append(f"  {message}")
        return "\n".join(lines)


async def _stage_and_submit(
    deps: VerifyDeps,
    state: CvlrGenerationState,
    draft: str,
    *,
    tool_call_id: str,
    choose: Callable[[set[str]], Submission | str],
) -> CvlrOutcome | str:
    """Stage ``draft`` with the unit's summaries and edits, build it, and submit what ``choose`` returns.

    ``choose`` sees the rule names in the ``.so`` and returns the submission to
    write, or a message. A message, or a missing or unreadable section, writes
    no conf.

    Every SBF build of the crate writes the same ``.so``. ``run_submission``
    rebuilds it into the shared target directory and uploads it. A sibling's
    build in between would replace that ``.so`` with one built for the
    sibling's feature (:func:`composer.spec.cvlr.prover.prepare_submission`).
    The build permit is held from staging until the job's link arrives, after
    the upload, or until ``run_submission`` returns without a link. The
    prover's rebuild happens under that permit, so it lists the same rules and
    the section is not read again.

    The cloud job is not covered. A sibling that stages during it compiles
    behind its own feature.
    """
    async with deps.target.build_slot() as permit:
        staged = await permit.stage(draft, state["summaries"], state["vfs"])
        build = await staged.build_for_submission(deps.unit_submission)
        if not isinstance(build.verdict, Built):
            return BuildRejected(build)
        artifact = build.verdict.manifest.artifact
        try:
            names = rule_names_in_elf(artifact)
        except ManifestCorrupt as err:
            # The offset, not the section. A bad record's payload is not a log line.
            _log.warning("could not read .cvlr_rules in %s at offset %s", artifact, err.offset)
            return _unread(err)
        except ManifestMissing as err:
            return _unread(err)
        chosen = choose(names)
        if not isinstance(chosen, Submission):
            return chosen
        prepared = await staged.prepare(build, chosen, into=deps.submission_dir)
        return await run_submission(
            prepared,
            prover_opts=deps.prover_opts,
            cex=RuleStatusReport(),
            callbacks=_RunAccounting(permit),
            tool_call_id=tool_call_id,
        )


@tool_display("Running the Solana Prover", "Prover")
class VerifyRules(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, VerifyDeps],
):
    """Build the program with the harness and check its rules with the Certora Solana Prover.

    This takes minutes and spends prover time. Get ``cargo_check`` green first.
    When every rule the build listed is accounted for, it stamps that draft.

    A failing rule is not always a bad rule. If it exposes a real defect, mark
    it with ``expect_rule_failure`` and say why. Marked rules are excluded from
    this gate, so a run can finish with a real violation still in it.
    """

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        with self.tool_deps() as deps:
            listed: list[str] = []

            def choose(names: set[str]) -> Submission | str:
                listed[:] = sorted(names)
                if not names:
                    return (
                        "The build listed no rules, so nothing was submitted. A rule is a "
                        "`#[rule]` function or a `cvlr_rules!` invocation."
                    )
                # A conf with no `rule` entry makes the cloud job end in FAILED, with no
                # report. The section holds only this unit's rules: the build enables only
                # this unit's feature.
                return deps.submission_for(self.state, listed)

            outcome = await _stage_and_submit(
                deps,
                self.state,
                draft,
                tool_call_id=self.tool_call_id,
                choose=choose,
            )
            if isinstance(outcome, str):
                return outcome
            return await self._report(outcome, deps, declared=listed)

    async def _inert_summaries(self, build: SbfRun) -> str | None:
        """A note when a summary matches nothing, or when the names cannot be read.

        A summary is a regex over demangled names. One that matches nothing
        changes nothing, and the prover reports no diagnostic. A symbol the
        compiler inlined away is absent from the build, so no spelling of it
        matches.

        Returns None when there is nothing to check or every directive matched.
        An unreadable symbol table is a sentence in the tool result, not silence:
        silence reads as every directive matching.
        """
        directives = tuple(d.pattern for d in self.state["summaries"])
        if not directives or not isinstance(build.verdict, Built):
            return None
        try:
            symbols = await prover_function_names(build.verdict.manifest.artifact)
        except (SymbolsUnavailable, ELFError, OSError):
            _log.warning("could not read symbols to check summary directives", exc_info=True)
            return (
                "The build's function names could not be read, so summary directives "
                "were not checked against them."
            )
        missed = unmatched(directives, symbols)
        if not missed:
            return None
        return _InertSummaries.bind({
            "total": len(directives),
            "missed": [
                {"pattern": pattern, "suggestions": nearest(pattern, symbols)} for pattern in missed
            ],
        }).render_to(load_jinja_template)

    async def _report(
        self,
        outcome: CvlrOutcome,
        deps: VerifyDeps,
        *,
        declared: Sequence[str],
    ) -> Command | str:
        stamper = deps.stamper
        expected = self.state["expected_failures"]
        match outcome:
            case BuildRejected(build=build):
                said = (
                    build.verdict.diagnostics
                    if isinstance(build.verdict, CompileFailed)
                    else "no diagnostics were captured"
                )
                return tool_return(
                    self.tool_call_id,
                    f"The chain build failed, so nothing was submitted:\n{said}",
                )
            case SubmissionFailed(build=build, reason=reason):
                said = [f"The prover run did not produce results: {reason}"]
                if (inert := await self._inert_summaries(build)) is not None:
                    said.append(inert)
                return tool_return(self.tool_call_id, "\n\n".join(said))
            case Checked(build=build, report=report):
                status = report.rule_outcomes()
                incomplete = incomplete_checks(report.violated_assertions)
                unexcused = unaccounted(status, expected, incomplete)
                surprising = [name for name in unexcused if status[name] == "VERIFIED"]
                property_failures = [
                    name
                    for name in unexcused
                    if status[name] == "VIOLATED" and name not in incomplete
                ]
                unfinished = [
                    name for name in unexcused if status[name] not in ("VERIFIED", "VIOLATED")
                ]
                unverdicted = _unverdicted(declared, status)
                lines = [report.result_str]
                if (inert := await self._inert_summaries(build)) is not None:
                    lines.append(inert)
                if surprising:
                    lines.append(_names_note(_ExpectedVerified, surprising))
                if incomplete:
                    lines.append(_incomplete_note(incomplete))
                if unverdicted:
                    lines.append(_names_note(_NoVerdict, unverdicted))
                if property_failures:
                    lines.append(_names_note(_Unaccounted, property_failures))
                if unfinished:
                    lines.append(_names_note(_NoPropertyVerdict, unfinished))
                externals: ExternalWarnings | UnreadableAlertReport
                match report.alerts:
                    case UnreadableAlertReport() as unreadable:
                        externals = unreadable
                        lines.append(
                            "The alert report could not be read, so the functions this run "
                            "treated as external are unknown."
                        )
                    case AlertReport() as alerts:
                        externals = external_warnings(alerts)
                        if (note := _externals_note(externals.messages)) is not None:
                            lines.append(note)
                last = LastVerdicts(
                    status=status,
                    incomplete=sorted(incomplete),
                    unverdicted=unverdicted,
                    stamp=stamper(self.state, tuning_history(self.state)),
                )
                # A marking re-decides with this same predicate. The cleared stamp is what a
                # withdrawn marking writes. A run that is not accounted for clears a stamp of
                # this same draft, and leaves a stamp of a different draft where it is.
                earned = stamp_under(last, expected)
                validations = {}
                if earned == last.stamp:
                    lines.append("Every rule is accounted for. This draft is stamped.")
                    validations = {"validations": earned}
                elif (
                    self.state["validations"].get(PROVER_VALIDATION_KEY)
                    == last.stamp[PROVER_VALIDATION_KEY]
                ):
                    lines.append("This run is not accounted for, so the prover stamp is withdrawn.")
                    validations = {"validations": earned}
                return tool_state_update(
                    self.tool_call_id,
                    "\n\n".join(lines),
                    prover_link=report.link,
                    external_functions=externals,
                    last_verdicts=last,
                    **validations,
                )


def gate_tools(deps: VerifyDeps) -> list[BaseTool]:
    """The tools that compile a draft, change what a run checks, and submit it.

    ``cargo_check`` and ``summarize_for_prover`` are bound to ``deps.target``,
    the target ``verify_rules`` submits. No tool here edits the program.
    """
    return [
        CargoCheck.bind(deps.target).as_tool("cargo_check"),
        VerifyRules.bind(deps).as_tool("verify_rules"),
        SummarizeForProver.bind(deps.target.tuning).as_tool("summarize_for_prover"),
        AdjustProverConfig.as_tool("adjust_prover_config"),
    ]
