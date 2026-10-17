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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Mapping, Sequence, override

from elftools.common.exceptions import ELFError
from langchain_core.tools import BaseTool

from graphcore.graph import LLM, tool_return, tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.state import ValidationStamper
from composer.diagnostics.timing import get_run_summary
from composer.cargo.features import CargoFeature
from composer.cargo.sbf import Built, SbfRun
from composer.cargo.session import CargoSession, CompileFailed, Compiled
from composer.cargo.symbols import SymbolsUnavailable, defined_functions, nearest, unmatched
from composer.prover.core import (
    CexHandler,
    ProverCallbacks,
    ProverOptions,
    TrivialFanoutCexHandler,
    UnanalyzedCexHandler,
)
from composer.prover.ptypes import (
    IncompleteCheck,
    LoopUnwound,
    OverflowUnproven,
    PropertyViolation,
    RuleResult,
    classify_violation,
)
from composer.prover.conf import SelectRules
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    CvlrOutcome,
    Submission,
    SubmissionFailed,
    prepare_submission,
    run_submission,
)
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.author_tools import AdjustProverConfig, SummarizeForProver, unaccounted
from composer.spec.cvlr.state import (
    CvlrGenerationState,
    LastVerdicts,
    tuning_history,
)
from composer.spec.cvlr.tree import SharedTree, UnitEdits
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.types import CheckName
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)


class BuildPermit:
    """The run's build permit, held by one unit.

    A submission holds it through the Prover's local rebuild and upload. Every
    build of the crate writes the same ``.so``. The permit is released when
    that upload finishes, before the cloud job finishes.
    """

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._held = False

    async def acquire(self) -> None:
        await self._sem.acquire()
        self._held = True

    def release(self) -> None:
        """Release the permit. Releasing one that is already released does nothing."""
        if self._held:
            self._held = False
            self._sem.release()


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
        permit = BuildPermit(self.build_sem)
        await permit.acquire()
        try:
            yield permit
        finally:
            permit.release()

    async def stage(
        self,
        draft: str,
        summaries: Sequence[SummaryDirective] = (),
        overlay: Mapping[str, str] = {},
    ) -> None:
        """Make the tree match this unit's state.

        The draft, the summaries, and the source overlay are all inputs to the
        build. Each is taken from already-merged state, so two concurrent calls
        cannot each write their own view.

        The summaries file is rewritten from ``summaries`` even when that is
        empty. The composite the conf names has to exist, and a directive
        dropped from state has to leave the file. The overlay replaces this
        unit's edited files
        (:meth:`composer.spec.cvlr.tree.SharedTree.reconcile`).

        Call it while holding :meth:`build_slot`. That permit keeps two units
        out of the tree at once, and keeps a sibling's edits out of this build.
        """
        self.tuning.write(tuple(summaries))
        await self.tree.reconcile(
            self.unit.module,
            UnitEdits(module_path=self.module_path, draft=draft, overlay=overlay),
        )


class _RunAccounting(ProverCallbacks):
    """Records the prover link, the prover's runtime, and the wall clock.

    ``ProverCallbacks`` defaults do nothing. A backend that overrides only some
    events records none of these. ``summary.format()``, the per-task link, and
    ``job_info.json``'s ``prover_usage`` read them. The same events are what
    the EVM ``_SpecCallbacks`` record.

    Separate from :class:`_CaptureCallbacks` because a submission with no
    analysis store still spends prover time.
    """

    def __init__(self) -> None:
        super().__init__()
        self._started_mono: float | None = None
        self._upload_permit: BuildPermit | None = None

    def release_on_upload(self, permit: BuildPermit) -> None:
        """Release ``permit`` when the Prover reports the job's link.

        The link arrives after the local rebuild and upload. Until then a
        sibling's build could replace the ``.so`` this job uploads.
        """
        self._upload_permit = permit

    @override
    async def on_prover_run(self, args: list[str]) -> None:
        self._started_mono = time.perf_counter()

    @override
    async def on_prover_link(self, link: str) -> None:
        if self._upload_permit is not None:
            self._upload_permit.release()
        get_run_summary().record_prover_link(link)

    @override
    async def on_prover_runtime(self, ms: int) -> None:
        """The prover's start-to-end time, without queue wait.

        The wall clock in :meth:`on_prover_result` includes time spent waiting
        in the queue.
        """
        get_run_summary().record_prover_runtime(ms)

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        if self._started_mono is not None:
            get_run_summary().add_prover_call(time.perf_counter() - self._started_mono)
            self._started_mono = None


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
        self._incomplete: dict[str, IncompleteCheck] = {}

    @property
    def incomplete(self) -> Mapping[str, IncompleteCheck]:
        """Rules from the last run that stopped on an assertion the prover generated.

        The gate reports these separately from a rule whose own assertion
        failed. The counterexample does not survive into
        :class:`~composer.prover.core.ProverReport`, which carries statuses
        only, so the classification is kept here.
        """
        return self._incomplete

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        # This run replaces analyses for the rules it covers. Clearing them
        # before the handler writes new ones keeps a rule that failed earlier
        # and passes now out of the report. This callback runs before the
        # handler, so the records below are this run's.
        await super().on_prover_result(results)
        self._incomplete = {}
        for result in results.values():
            if result.status != "VIOLATED":
                continue
            match classify_violation(result.counterexample):
                case IncompleteCheck() as stopped:
                    self._incomplete[result.path.rule] = stopped
                case PropertyViolation():
                    pass
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
    #: The directory :func:`composer.spec.cvlr.prover.prepare_submission` writes into.
    #: Outside the tree. The confined build can write the tree, so the conf and
    #: the build script must not live there.
    submissions: Path
    prover_opts: ProverOptions
    stamper: ValidationStamper
    #: ``None`` runs the prover with no analysis. Plumbing tests and any caller
    #: with no LLM use that. A run that produces findings supplies this.
    analysis: CexAnalysis | None = None
    #: One submission at a time per unit. A second concurrent run would build
    #: into the same workdir and race the staged module. The tool refuses it.
    #: A caller that made two calls wanted two answers.
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


def _incomplete_advice(stopped: IncompleteCheck) -> str:
    match stopped:
        case LoopUnwound():
            return (
                "A loop needed more iterations than the bound unrolls. Constrain what determines "
                "the trip count, summarize the loop if it is not what your property is about, or "
                "skip the property and say what bound it needed."
            )
        case OverflowUnproven():
            return (
                "`Cannot overflow` is the sound-math pass failing to show that an expression it "
                "takes for a pointer stays below 2^64, almost always because a value the rule reads "
                "from an account (a balance, an amount) is free to sit just below u64::MAX. Bound "
                "the account-data values on that rule's path — `cvlr_assume!(x < 1 << 63)` is "
                "enough — and record each bound as an assumption. A property about the top of the "
                "range cannot take the bound: skip it and say so."
            )
        case _:
            return "The prover's own message says what it needed."


def _incomplete_note(incomplete: Mapping[str, IncompleteCheck]) -> str:
    named = "\n".join(
        f"  {name}: {stopped.assertion}" for name, stopped in sorted(incomplete.items())
    )
    advice = "\n".join(dict.fromkeys(_incomplete_advice(s) for _, s in sorted(incomplete.items())))
    return (
        "These rules stopped on an assertion the prover generated rather than one of yours, so "
        f"they never reached their property:\n{named}\n"
        f"Nothing here is a statement about the program. {advice}\n"
        "Do not mark one of these with expect_rule_failure: that claims a real defect, and none "
        "has been shown."
    )


def _externals_note(externals: Sequence[str]) -> str | None:
    """Text for the author about functions this run treated as external, or None.

    Not a gate. The starting configuration leaves most platform code external,
    and the author cannot add inlining directives. Where the program's own
    code makes the call, the message tells the author how to bound the value
    with a stand-in.

    Inlining that platform code is often worse. Stake activation loops once
    per epoch over a sysvar, in ``f64``, and the rule comes back vacuous on
    the unwinding assertion.
    """
    if not externals:
        return None
    _log.info("prover treated %d function(s) as external: %s", len(externals), ", ".join(externals))
    listed = "\n".join(f"  {name}" for name in externals)
    return (
        "The prover treated these functions as external, neither analyzed nor summarized:\n"
        f"{listed}\n"
        "An external call returns an arbitrary value and writes nothing else, so any state it "
        "would change keeps its old value, and one that returns its result through memory may "
        "never succeed, which leaves the code after it unreachable. None of this shows in a "
        "verdict. You cannot add inlining directives.\n"
        "If a rule's property depends on the value one of these computes, and the program's own "
        "code makes the call, you can bound the value instead of losing it. Ask `code_editor` to "
        "extract the call into a function of its own (an extraction), then to replace that "
        "function with a stand-in you write (`mock_fn`) that returns `nondet()` values constrained "
        "by what the real function guarantees, such as a result no larger than its input. Use "
        "only bounds the real function provably keeps: a tighter one can pass a rule the program "
        "fails. Name the stand-in in `rule_subjects`. Otherwise say so in the rule's commentary, "
        "drive the program's own code below the call, or skip the property."
    )


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
            async with target.build_slot():
                await target.stage(draft, self.state["summaries"], self.state["vfs"])
                run = await target.session.check(
                    manifest_dir=target.package_dir, features=target.features
                )
        match run.verdict:
            case Compiled():
                declared = rule_names(draft)
                names = ", ".join(declared) if declared else "none"
                return tool_return(
                    self.tool_call_id,
                    f"Compiled in {run.duration_ms}ms. Rules declared: {names}.",
                )
            case CompileFailed(diagnostics=diagnostics):
                return tool_return(
                    self.tool_call_id,
                    f"Does not compile:\n{diagnostics}",
                )


def _submission_for(
    deps: VerifyDeps, state: CvlrGenerationState, rules: Sequence[str]
) -> Submission:
    """The unit's submission, checking ``rules`` under the current prover settings.

    The settings come from state. A submission built from the run's starting
    copy would send the old settings while ``version_history`` recorded the
    new ones.
    """
    return dataclasses.replace(
        deps.submission, rules=SelectRules(tuple(rules)), settings=state["prover_settings"]
    )


async def _stage_and_submit(
    deps: VerifyDeps,
    state: CvlrGenerationState,
    draft: str,
    submission: Submission,
    *,
    callbacks: _RunAccounting,
    cex: CexHandler,
    tool_call_id: str,
) -> CvlrOutcome:
    """Stage ``draft`` with the unit's summaries and edits, build it, and submit it.

    One build permit is held until the Prover has uploaded. ``run_submission``
    rebuilds the crate into the shared target directory and uploads that
    ``.so``. A sibling must not build between this unit's gate build and that
    upload (:func:`composer.spec.cvlr.prover.prepare_submission`). The permit
    is released when the job's link arrives, which is after the upload, or
    when ``run_submission`` returns without a link.

    The cloud job itself is not covered. Files a sibling stages during it are
    behind that sibling's feature.
    """
    async with deps.target.build_slot() as permit:
        await deps.target.stage(draft, state["summaries"], state["vfs"])
        prepared = await prepare_submission(
            deps.target.session, submission, into=deps.submissions
        )
        if isinstance(prepared, BuildRejected):
            return prepared
        callbacks.release_on_upload(permit)
        outcome = await run_submission(
            deps.target.session,
            prepared,
            prover_opts=deps.prover_opts,
            callbacks=callbacks,
            cex=cex,
            tool_call_id=tool_call_id,
        )
    return outcome


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
        declared = rule_names(draft)
        if not declared:
            return (
                "Your draft declares no rules, so there is nothing to submit. A rule is a "
                "`#[rule]` function or a `cvlr_rules!` invocation."
            )
        with self.tool_deps() as deps:
            if deps.lock.locked():
                return (
                    "A prover run for this unit is already in flight. Wait for it rather than "
                    "starting a second one."
                )
            async with deps.lock:
                analysis = deps.analysis
                capture = analysis.callbacks() if analysis else None
                # Name the rules this draft declares. A conf with no `rule` entry
                # makes the cloud job end in FAILED, with no report. Naming every
                # rule in the artifact would also be wrong: the build compiles
                # this unit's module, and the artifact declares every unit's rules.
                outcome = await _stage_and_submit(
                    deps,
                    self.state,
                    draft,
                    _submission_for(deps, self.state, declared),
                    callbacks=capture if capture is not None else _RunAccounting(),
                    cex=analysis.handler(self.state) if analysis else UnanalyzedCexHandler(),
                    tool_call_id=self.tool_call_id,
                )
            return self._report(
                outcome,
                deps,
                capture.incomplete if capture is not None else {},
                declared=declared,
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
        lines = [
            f"{len(missed)} of your {len(directives)} summary directive(s) match no symbol in this "
            "build, so they had no effect:"
        ]
        for pattern in missed:
            lines.append(f"  {pattern}")
            for suggestion in nearest(pattern, symbols):
                lines.append(f"      this build does define: {suggestion}")
        lines.append(
            "A symbol absent from the build is usually one the compiler inlined away, and no "
            "spelling of it will match. Summarize a symbol that is there — the callee one level "
            "out is the usual answer."
        )
        return "\n".join(lines)

    def _report(
        self,
        outcome: CvlrOutcome,
        deps: VerifyDeps,
        incomplete: Mapping[str, IncompleteCheck],
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
                unexcused = unaccounted(status, expected, incomplete)
                unverdicted = _unverdicted(declared, status)
                surprising = _wrongly_expected(status, expected)
                lines = [report.result_str]
                if (inert := self._inert_summaries(build)) is not None:
                    lines.append(inert)
                if surprising:
                    lines.append(
                        "These rules are marked as expected failures but VERIFIED: "
                        f"{', '.join(surprising)}. Either the defect is not there, or the rule does "
                        "not test for it — resolve that before publishing."
                    )
                if incomplete:
                    lines.append(_incomplete_note(incomplete))
                if unverdicted:
                    lines.append(
                        f"No verdict came back for: {', '.join(unverdicted)}. The prover's report "
                        "does not name them, so they have not been checked, and this draft cannot "
                        "be stamped until they are. Run verify_rules again; if they are still "
                        "missing, the run's link is where to look."
                    )
                if unexcused:
                    lines.append(
                        f"Not accounted for: {', '.join(unexcused)}. Fix the rule, or mark it "
                        "with expect_rule_failure and say why the failure is real."
                    )
                if (externals := _externals_note(report.external_functions)) is not None:
                    lines.append(externals)
                last = LastVerdicts(
                    status=status,
                    incomplete=sorted(incomplete),
                    unverdicted=unverdicted,
                    stamp=stamper(self.state, tuning_history(self.state)),
                )
                if unexcused or unverdicted:
                    return tool_state_update(
                        self.tool_call_id,
                        "\n\n".join(lines),
                        prover_link=report.link,
                        external_functions=report.external_functions,
                        last_verdicts=last,
                    )
                return tool_state_update(
                    self.tool_call_id,
                    "\n\n".join([*lines, "Every rule is accounted for. This draft is stamped."]),
                    prover_link=report.link,
                    external_functions=report.external_functions,
                    last_verdicts=last,
                    validations=last.stamp,
                )


def gate_tools(target: HarnessTarget, deps: VerifyDeps) -> list[BaseTool]:
    """The gate tools, and the tool that changes what they check.

    No tool here edits the program. The author gets ``code_editor`` and the
    edit-history tools, bound with the rest of its tools.
    """
    return [
        CargoCheck.bind(target).as_tool("cargo_check"),
        VerifyRules.bind(deps).as_tool("verify_rules"),
        SummarizeForProver.bind(target.tuning).as_tool("summarize_for_prover"),
        AdjustProverConfig.as_tool("adjust_prover_config"),
    ]
