"""The author's gate tools: a host compile, and the prover run that stamps the draft.

* ``cargo_check`` compiles on the host target, confined, in seconds. It is not
  a required stamp. A prover run builds first and reports a compiler error
  without submitting (:class:`~composer.spec.cvlr.prover.BuildRejected`).
* ``verify_rules`` builds for the chain, submits, and stamps the draft's
  digest when every rule is accounted for.

A rule marked as an expected failure is left out of the all-green check. On
this backend a failing rule is often a finding: the property holds as written
and the program violates it. The marking records that claim.
"""

import asyncio
import dataclasses
import logging
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import (
    AsyncIterator,
    Callable,
    Mapping,
    Sequence,
    override,
)

from elftools.common.exceptions import ELFError
from langchain_core.tools import BaseTool
from prover_output_utility import ProverOutputAPI
from pydantic import Field

from graphcore.graph import LLM, tool_return, tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.state import ValidationStamper
from composer.cargo.features import CargoFeature
from composer.cargo.sbf import Built, SbfRun
from composer.cargo.session import CargoSession, CompileFailed, Compiled
from composer.cargo.symbols import SymbolsUnavailable, defined_functions, nearest, unmatched
from composer.prover.cloud import results_api
from composer.prover.core import (
    CexHandler,
    ProverOptions,
    TrivialFanoutCexHandler,
)
from composer.prover.ptypes import (
    IncompleteCheck,
    LoopUnwound,
    OverflowUnproven,
    PropertyViolation,
    RuleResult,
    classify_violation,
)
from composer.prover.accounting import RunAccounting
from composer.prover.conf import SelectRules
from composer.spec.cvlr.conf import (
    DEFAULT_FEATURE,
    CollectUnsatCore,
    tunable_conf,
)
from composer.spec.cvlr.manifest import ManifestCorrupt, ManifestMissing, rule_names_in_elf
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
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.author_tools import (
    AdjustProverConfig,
    SummarizeForProver,
    stamp_under,
    unaccounted,
)
from composer.spec.cvlr.state import (
    CvlrGenerationState,
    LastVerdicts,
    tuning_history,
)
from composer.spec.cvlr.tree import SharedTree, UnitEdits
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.spec.cvlr.vacuity import VacuityAnalyzer, core_finding
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.source.report_prover import fetch_unsat_cores
from composer.spec.types import CheckName
from composer.templates.loader import load_jinja_template
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)


class BuildPermit:
    """The run's build permit, held by one unit.

    A submission holds it through the Prover's local rebuild and upload. Every
    build of the crate writes the same ``.so``. The permit is released when
    that upload finishes, before the cloud job finishes.
    """

    def __init__(self, target: "HarnessTarget") -> None:
        self._target = target
        self._held = False

    async def acquire(self) -> None:
        await self._target.build_sem.acquire()
        self._held = True

    def release(self) -> None:
        """Release the permit. Releasing one that is already released does nothing."""
        if self._held:
            self._held = False
            self._target.build_sem.release()

    async def stage(
        self,
        draft: str,
        summaries: Sequence[SummaryDirective] = (),
        overlay: Mapping[str, str] = {},
    ) -> None:
        """Make the tree match the unit's state.

        The draft, the summaries, and the source overlay are all inputs to the
        build. Each is taken from already-merged state, so two concurrent calls
        cannot each write their own view.

        The summaries file is rewritten from ``summaries`` even when that is
        empty. The composite the conf names has to exist, and a directive
        dropped from state has to leave the file. The overlay replaces the
        unit's edited files
        (:meth:`composer.spec.cvlr.tree.SharedTree.reconcile`).

        Staging needs the permit held. It keeps two units out of the tree at
        once, and keeps a sibling's edits out of this build.
        """
        if not self._held:
            raise RuntimeError("staging needs a held build permit")
        target = self._target
        target.tuning.write(tuple(summaries))
        await target.tree.reconcile(
            target.unit.module,
            UnitEdits(module_path=target.module_path, draft=draft, overlay=overlay),
        )


@dataclasses.dataclass(frozen=True)
class HarnessTarget:
    """Where a draft is staged so the crate compiles with it.

    The harness is a module inside the crate. Staging writes the file the
    scaffold already declared. ``module_path`` is absolute. ``specs/mod.rs``
    was written once for the run
    (:meth:`composer.spec.cvlr.harness.CvlrArtifactStore.declare_modules`).

    Every unit shares ``tree`` and ``build_sem``. A build selects ``certora``
    plus :attr:`~composer.spec.cvlr.harness.HarnessModule.feature`, so no other
    unit's module is compiled.
    """

    session: CargoSession
    module_path: Path
    package: str
    package_root: Path
    tuning: TuningFiles
    #: The module name the tree keys edits by, and the cargo feature that selects
    #: this unit.
    unit: HarnessModule
    #: The run's one working copy, shared by every unit.
    tree: SharedTree
    #: Held across staging, the local cargo invocation, and, for a submission,
    #: the Prover's rebuild and upload. Released before the cloud job. A
    #: sibling's files are behind its own feature.
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

    @asynccontextmanager
    async def build_slot(self) -> AsyncIterator[BuildPermit]:
        """Serialize staging and the cargo invocation that follows it.

        Cargo already serializes concurrent builds on its build-directory lock.
        The permit is a queue the host can see, and it keeps several sandboxed
        builds from sitting on grants.

        A submission needs the permit for another reason. Every SBF build
        writes the same ``.so``. The Prover's local phase rebuilds it and
        uploads it. A sibling build in between would replace that file with one
        built for the sibling's feature. :func:`_stage_and_submit` releases the
        permit through the yielded :class:`BuildPermit` once the upload is done.
        """
        permit = BuildPermit(self)
        await permit.acquire()
        try:
            yield permit
        finally:
            permit.release()


class _RunAccounting(RunAccounting):
    """Accounts for the run, and releases the build permit once the upload is done.

    Separate from :class:`_CaptureCallbacks` because a submission with no
    analysis store still spends prover time.
    """

    def __init__(self) -> None:
        super().__init__()
        self._upload_permit: BuildPermit | None = None

    def release_on_upload(self, permit: BuildPermit) -> None:
        """Release ``permit`` when the Prover reports the job's link.

        The link arrives after the local rebuild and upload. Until then a
        sibling's build could replace the ``.so`` this job uploads.
        """
        self._upload_permit = permit

    @override
    async def on_prover_link(self, link: str) -> None:
        if self._upload_permit is not None:
            self._upload_permit.release()
        await super().on_prover_link(link)


class _CaptureCallbacks(_RunAccounting):
    """Stores analyses of violated rules for the report.

    The handler explains every violated rule to the author, including an
    unwound loop bound. This callback records evidence. An
    :class:`IncompleteCheck` is not evidence about the program. Recording one
    would let a later pass treat the prover's limit as a bug in the program.
    """

    def __init__(self, store: CexAnalysisStore) -> None:
        super().__init__()
        self._store = store

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        # This run replaces analyses for the rules it covers. Clearing them
        # before the handler writes new ones keeps a rule that failed earlier
        # and passes now out of the report.
        await super().on_prover_result(results)
        for rule_name in {r.path.rule for r in results.values()}:
            try:
                await self._store.forget_rule(rule_name)
            except Exception:
                _log.exception("cvlr: failed to clear stale cex analyses for %s", rule_name)

    @override
    async def on_analysis_complete(self, rule: RuleResult, explanation: str) -> None:
        match classify_violation(rule.counterexample):
            case IncompleteCheck(assertion=assertion):
                _log.info(
                    "cvlr: %s came back violated on an assertion the prover generated (%s), so it "
                    "is not recorded as evidence about the program",
                    rule.name,
                    assertion.split(".")[0],
                )
            case PropertyViolation():
                # A capture failure must not fail the run. The verdict is already in hand.
                try:
                    await self._store.record(rule.path, explanation, rule.cex_dump)
                except Exception:
                    _log.exception("cvlr: failed to capture cex analysis for %s", rule.name)


class AuthorModel:
    """The author's model, including the tools the author's graph binds.

    A counterexample analysis continues the author's conversation. It has to
    use the author's tools, system prompt, and settings. Those come first in
    the request, and a difference misses the provider's prompt cache.

    The graph binds the model when it is built, after the tools that need it
    exist. The author calls :meth:`bind` once the graph exists.
    """

    def __init__(self) -> None:
        self._llm: LLM | None = None

    def bind(self, llm: LLM) -> None:
        self._llm = llm

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            raise RuntimeError("counterexample analysis ran before the author's graph was built")
        return self._llm


@dataclasses.dataclass(frozen=True)
class CexAnalysis:
    """Explains a violated rule and stores that explanation.

    Built per unit. The handler is built per call: it reads the author's
    current conversation, and it uses the author's model.
    """

    store: CexAnalysisStore
    model: AuthorModel = dataclasses.field(default_factory=AuthorModel)

    def handler(self, state: CvlrGenerationState) -> CexHandler:
        return TrivialFanoutCexHandler(self.model.llm, state)

    def callbacks(self) -> "_CaptureCallbacks":
        return _CaptureCallbacks(self.store)


@dataclasses.dataclass(frozen=True)
class VerifyDeps:
    """What the prover gate needs beyond the draft."""

    target: HarnessTarget
    submission: Submission
    #: The directory :func:`composer.spec.cvlr.prover.write_submission` writes into.
    #: Outside the tree. The confined build can write the tree, so the conf and
    #: the build script must not live there.
    submission_dir: Path
    prover_opts: ProverOptions
    stamper: ValidationStamper
    analysis: CexAnalysis

    def submission_for(self, state: CvlrGenerationState, rules: Sequence[str]) -> Submission:
        """The unit's submission, checking ``rules`` under the current prover settings.

        The settings come from state. A submission built from the run's starting
        copy would send the old settings while ``version_history`` recorded the
        new ones.
        """
        return dataclasses.replace(
            self.submission, rules=SelectRules(tuple(rules)), settings=state["prover_settings"]
        )


def _author_text(template: str, /, **values: object) -> str:
    return load_jinja_template(template, **values).rstrip("\n")


def _incomplete_kind(stopped: IncompleteCheck) -> str:
    match stopped:
        case LoopUnwound():
            return "loop"
        case OverflowUnproven():
            return "overflow"
        case _:
            return "other"


def _incomplete_note(incomplete: Mapping[str, IncompleteCheck]) -> str:
    ordered = sorted(incomplete.items())
    kinds = list(dict.fromkeys(_incomplete_kind(stopped) for _, stopped in ordered))
    return _author_text(
        "cvlr/incomplete_check.j2",
        rules=[{"name": name, "assertion": stopped.assertion} for name, stopped in ordered],
        advice="\n".join(_author_text("cvlr/incomplete_advice.j2", kind=kind) for kind in kinds),
    )


def _externals_note(externals: Sequence[str]) -> str | None:
    """Text for the author about functions this run treated as external, or None.

    ``externals`` is the Prover's own warning text. Not a gate. The starting
    configuration leaves most platform code external, and the author cannot add
    inlining directives. Where the program's own code makes the call, the message
    tells the author how to bound the value with a stand-in.

    Inlining that platform code is often worse. Stake activation loops once
    per epoch over a sysvar, in ``f64``, and the rule comes back vacuous on
    the unwinding assertion.
    """
    if not externals:
        return None
    _log.info("prover treated functions as external:\n%s", "\n".join(externals))
    return _author_text("cvlr/external_functions.j2", warnings="\n\n".join(externals))


def _unverdicted(declared: Sequence[str], status: Mapping[str, bool]) -> list[str]:
    """Declared rules the report gave no verdict for.

    The verdict roll-up only sees rules the report names. A missing rule would
    otherwise leave "every rule is accounted for" true.
    """
    return sorted(set(declared) - status.keys())


def _wrongly_expected(status: dict[str, bool], expected_failures: dict[CheckName, str]) -> list[str]:
    """Rules marked as expected failures that verified.

    A rule the author said exposed a bug, and which passes, means the bug is
    gone or the rule does not test for it.
    """
    return sorted(name for name in expected_failures if status.get(name) is True)


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
                await permit.stage(draft, self.state["summaries"], self.state["vfs"])
                run = await target.session.check(
                    manifest_dir=target.package_dir, features=target.features
                )
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
    """What the author is told when the built ``.so`` did not list its rules."""
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


async def _stage_and_submit(
    deps: VerifyDeps,
    state: CvlrGenerationState,
    draft: str,
    submission: Submission,
    *,
    callbacks: _RunAccounting,
    tool_call_id: str,
    choose: Callable[[set[str]], Submission | Command | str] | None = None,
    analyze: bool = True,
) -> CvlrOutcome | Command | str:
    """Stage ``draft``, build it, and submit the submission ``choose`` returns.

    The rule names are in the ``.so`` this build writes. ``choose`` sees that
    set and returns the submission to write, or a result that means nothing is
    written. A missing or unreadable section is returned here, and no conf is
    written.

    When ``analyze`` is true, the counterexample handler is built from
    ``deps.analysis`` after the conf is written, immediately before
    :func:`run_submission`. Building it reads the author's model, so a run that
    returns earlier never builds one. ``explain_vacuity`` passes false: that
    job reads an unsat core, and a violated rule is rendered as its status.

    One build permit is held until the Prover has uploaded. ``run_submission``
    rebuilds the crate into the shared target directory and uploads that
    ``.so``. A sibling must not build between this unit's gate build and that
    upload (:func:`composer.spec.cvlr.prover.prepare_submission`). The permit
    is released when the job's link arrives, which is after the upload, or
    when ``run_submission`` returns without a link. The same ``.so`` is not
    read again: the permit makes the prover's rebuild the same rules.

    The cloud job itself is not covered. Files a sibling stages during it are
    behind that sibling's feature.
    """
    async with deps.target.build_slot() as permit:
        await permit.stage(draft, state["summaries"], state["vfs"])
        build = await build_for_submission(deps.target.session, submission)
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
        chosen = (
            choose(names)
            if choose is not None
            else _submission_for_listed(submission, names)
        )
        if not isinstance(chosen, Submission):
            return chosen
        prepared = Prepared(
            build, await write_submission(deps.target.session, chosen, into=deps.submission_dir)
        )
        callbacks.release_on_upload(permit)
        outcome = await run_submission(
            deps.target.session,
            prepared,
            prover_opts=deps.prover_opts,
            callbacks=callbacks,
            cex=deps.analysis.handler(state) if analyze else None,
            tool_call_id=tool_call_id,
        )
    return outcome


def _submission_for_listed(submission: Submission, names: set[str]) -> Submission | str:
    """``submission`` narrowed to ``names``, or a note that there is nothing to send.

    Callers that already know which rules to check pass their own ``choose``.
    This is the one used when they do not.
    """
    if not names:
        return (
            "The build listed no rules, so nothing was submitted. A rule is a `#[rule]` "
            "function or a `cvlr_rules!` invocation."
        )
    return dataclasses.replace(submission, rules=SelectRules(tuple(names)))


@tool_display("Running the Solana Prover", "Prover")
class VerifyRules(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, VerifyDeps],
):
    """Build the program with the harness and check its rules with the Certora Solana Prover.

    This takes minutes and spends prover time. Get ``cargo_check`` green first.
    On success it stamps the current draft, which ``result`` requires.

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

            def choose(names: set[str]) -> Submission | Command | str:
                listed[:] = sorted(names)
                if not names:
                    return self._nothing_to_submit()
                # The conf names the rules this build listed. A conf with no `rule`
                # entry makes the cloud job end in FAILED, with no report. The
                # section holds this unit's rules: the build enables only this
                # unit's feature.
                return deps.submission_for(self.state, listed)

            capture = deps.analysis.callbacks()
            outcome = await _stage_and_submit(
                deps,
                self.state,
                draft,
                deps.submission,
                callbacks=capture,
                tool_call_id=self.tool_call_id,
                choose=choose,
            )
            if not isinstance(outcome, (BuildRejected, SubmissionFailed, Checked)):
                return outcome
            return self._report(outcome, deps, declared=tuple(listed))

    def _nothing_to_submit(self) -> Command | str:
        """The build listed no rules.

        If nothing is skipped, the draft is unfinished and is not stamped. If
        every property is skipped, the prover gate is satisfied: there is
        nothing to submit. ``result`` still requires every property to be
        skipped or mapped to a listed rule, and the judge still has to accept
        the skips. The empty list is stored with the stamp, so publish sees a
        listing rather than a draft that was never built.
        """
        if not self.state["skipped"]:
            return (
                "The build listed no rules and no property is skipped, so there is nothing to "
                "submit. A rule is a `#[rule]` function or a `cvlr_rules!` invocation."
            )
        with self.tool_deps() as deps:
            stamp = deps.stamper(self.state, tuning_history(self.state))
            return tool_state_update(
                self.tool_call_id,
                "The build listed no rules. Every property you have not skipped would need one, "
                "and you have skipped them all. Nothing was submitted and no prover time was "
                "spent. The prover gate is satisfied by that; the judge still has to accept your "
                "skip reasons, so make each one name what blocked it.",
                validations=stamp,
                last_verdicts=LastVerdicts(
                    status={},
                    incomplete=[],
                    unverdicted=[],
                    stamp=stamp,
                    rules=(),
                ),
            )

    def _inert_summaries(self, build: SbfRun) -> str | None:
        """A note naming summary directives this build's symbols do not match, or None.

        A summary is a regex over demangled names. One that matches nothing
        changes nothing, and the prover reports no diagnostic. A symbol the
        compiler inlined away is absent from the build, so no spelling of it
        matches.

        Returns None when the symbols cannot be read. An unreadable artifact is
        a different failure from a directive that matched nothing.
        """
        directives = tuple(d.pattern for d in self.state["summaries"])
        if not directives or not isinstance(build.verdict, Built):
            return None
        try:
            symbols = defined_functions(build.verdict.manifest.artifact)
        except (SymbolsUnavailable, ELFError, OSError, subprocess.CalledProcessError):
            _log.warning("could not read symbols to check summary directives", exc_info=True)
            return None
        missed = unmatched(directives, symbols)
        if not missed:
            return None
        return _author_text(
            "cvlr/inert_summaries.j2",
            missed_count=len(missed),
            total=len(directives),
            missed=[
                {"pattern": pattern, "suggestions": nearest(pattern, symbols)} for pattern in missed
            ],
        )

    def _report(
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
                # Nothing was submitted. The message is the compiler's text from
                # the verdict. Only a failed build has any. Both tiers report a
                # failure as `CompileFailed`.
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
                if (inert := self._inert_summaries(build)) is not None:
                    said.append(inert)
                return tool_return(self.tool_call_id, "\n\n".join(said))
            case Checked(build=build, report=report):
                status = report.rule_status
                incomplete = report.incomplete
                unexcused = unaccounted(status, expected, incomplete)
                unverdicted = _unverdicted(declared, status)
                surprising = _wrongly_expected(status, expected)
                lines = [report.result_str]
                if (inert := self._inert_summaries(build)) is not None:
                    lines.append(inert)
                if surprising:
                    lines.append(
                        _author_text("cvlr/expected_verified.j2", names=", ".join(surprising))
                    )
                if incomplete:
                    lines.append(_incomplete_note(incomplete))
                if unverdicted:
                    lines.append(_author_text("cvlr/no_verdict.j2", names=", ".join(unverdicted)))
                if unexcused:
                    lines.append(_author_text("cvlr/unaccounted.j2", names=", ".join(unexcused)))
                if (externals := _externals_note(report.external_functions)) is not None:
                    lines.append(externals)
                last = LastVerdicts(
                    status=status,
                    incomplete=sorted(incomplete),
                    unverdicted=unverdicted,
                    stamp=stamper(self.state, tuning_history(self.state)),
                    rules=tuple(sorted(declared)),
                )
                # A marking re-decides with this same predicate. The cleared stamp is what a
                # withdrawn marking writes. A run that is not accounted for leaves the previous
                # validations where they are.
                earned = stamp_under(last, expected)
                if earned == last.stamp:
                    lines.append("Every rule is accounted for. This draft is stamped.")
                return tool_state_update(
                    self.tool_call_id,
                    "\n\n".join(lines),
                    prover_link=report.link,
                    external_functions=report.external_functions,
                    last_verdicts=last,
                    **({"validations": earned} if earned == last.stamp else {}),
                )


class _DiagnosticAccounting(_RunAccounting):
    """Prover bookkeeping for a diagnostic run: its time is counted, its link is not recorded.

    The link recorded for a task is the one its report points at, and that has to stay the run the
    author's draft was stamped by rather than a rerun made to explain one of its rules.
    """

    @override
    def _record_link(self, link: str) -> None:
        pass


@dataclasses.dataclass(frozen=True)
class VacuityDeps:
    """What ``explain_vacuity`` needs beyond the gate's own dependencies."""

    verify: VerifyDeps
    analyzer: VacuityAnalyzer
    #: Deferred because constructing the client logs in.
    results: Callable[[], ProverOutputAPI]


@tool_display(lambda p: f"Explaining why `{p['rule']}` is vacuous", "Vacuity")
class ExplainVacuity(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[str, VacuityDeps],
):
    """Explain why a rule ``verify_rules`` reported vacuous (``SANITY_FAILED``).

    Reruns that one rule of your current draft with the vacuity check off and unsat cores on — about
    as long as an ordinary run — and has an analyst read the core against your harness and the
    program. The answer starts with the one question that decides what to do next: whether the
    rule's own assertion is in the core.

    * **Not in the core**: the constraints in it contradict each other with no help from the
      assertion — your assumptions, the handler, or a model's own assumptions — and the analysis
      names them.
    * **In the core**: the proof needed the assertion, so the assumptions are not what makes the rule
      vacuous, and weakening them will not help. Look at how the assertion or its precondition is
      stated.

    Use it on a rule that came back ``SANITY_FAILED``, before changing the rule. It stamps nothing.
    """

    rule: str = Field(description="The rule reported SANITY_FAILED, as your draft declares it.")

    @override
    async def run(self) -> str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        with self.tool_deps() as deps:
            verify = deps.verify
            submission = dataclasses.replace(
                verify.submission_for(self.state, (self.rule,)),
                purpose=CollectUnsatCore(),
                # Its own conf file: the unit's is part of the deliverable.
                stem=f"{verify.submission.stem}_unsat_core",
            )

            def choose(names: set[str]) -> Submission | str:
                # The `.so` just built, not a list stored from an earlier run.
                # A draft edited since that run is not judged against its names.
                if self.rule not in names:
                    listed = ", ".join(sorted(names)) or "none"
                    return f"The build listed no rule `{self.rule}`. It listed: {listed}."
                return submission

            outcome = await _stage_and_submit(
                verify,
                self.state,
                draft,
                submission,
                callbacks=_DiagnosticAccounting(),
                tool_call_id=self.tool_call_id,
                analyze=False,
                choose=choose,
            )
            if not isinstance(outcome, (BuildRejected, SubmissionFailed, Checked)):
                assert isinstance(outcome, str)
                return outcome
            match outcome:
                case BuildRejected():
                    return (
                        "The chain build failed, so nothing was submitted. Run verify_rules to see "
                        "the compiler's diagnostics."
                    )
                case SubmissionFailed(reason=reason):
                    return f"The diagnostic run did not produce results: {reason}"
                case Checked(report=report):
                    pass
            unproved = sorted(
                {
                    f"{path.rule}: {status}"
                    for path, status in report.raw_rule_status.items()
                    if path.rule == self.rule and status != "VERIFIED"
                }
            )
            if unproved:
                return (
                    f"With the vacuity check off, `{self.rule}` did not verify "
                    f"({'; '.join(unproved)}), so there is no proof and no unsat core to read. "
                    "Run verify_rules and read what it reports for the rule."
                )
            cores = await asyncio.to_thread(
                fetch_unsat_cores, deps.results(), report.link, self.rule
            )
            if not cores:
                return (
                    f"`{self.rule}` verified with the vacuity check off, but the run wrote no unsat "
                    f"core for it, so there is nothing to analyze. The run: {report.link}"
                )
            core = cores[0]
            finding = core_finding(core)
            analysis = await deps.analyzer.explain(
                rule=self.rule,
                finding=finding,
                core=core,
                harness=draft,
                conf=tunable_conf(self.state["prover_settings"]),
                vfs=self.state["vfs"],
                within_tool=self.tool_call_id,
            )
            return "\n\n".join(
                [finding.summary(), analysis.format(), f"The diagnostic run: {report.link}"]
            )


def gate_tools(
    target: HarnessTarget, deps: VerifyDeps, analyzer: VacuityAnalyzer
) -> list[BaseTool]:
    """The gate tools, and the tool that changes what they check.

    No tool here edits the program. The author gets ``code_editor`` and the
    edit-history tools, bound with the rest of its tools.
    """
    return [
        CargoCheck.bind(target).as_tool("cargo_check"),
        VerifyRules.bind(deps).as_tool("verify_rules"),
        ExplainVacuity.bind(VacuityDeps(deps, analyzer, results_api)).as_tool("explain_vacuity"),
        SummarizeForProver.bind(target.tuning).as_tool("summarize_for_prover"),
        AdjustProverConfig.as_tool("adjust_prover_config"),
    ]
