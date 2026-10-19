"""The gate tools: a host compile, and the prover run that stamps the draft.

* ``cargo_check`` compiles on the host target, confined, in seconds. It is not
  a required stamp. A prover run builds first and reports a compiler error
  without submitting (:class:`~composer.spec.cvlr.prover.BuildRejected`).
* ``verify_rules`` builds for the chain, submits, and stamps the draft when
  every rule it declares verified.
"""

import asyncio
import dataclasses
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Mapping, override

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
from composer.cargo.session import CargoSession, CompileFailed, Compiled
from composer.prover.core import (
    CexHandler,
    ProverCallbacks,
    ProverOptions,
    TrivialFanoutCexHandler,
    UnanalyzedCexHandler,
)
from composer.prover.ptypes import (
    IncompleteCheck,
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
from composer.spec.cvlr.state import CvlrGenerationState
from composer.spec.cvlr.tree import SharedTree, UnitEdits
from composer.spec.source.cex_capture import CexAnalysisStore
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

    async def stage(self, draft: str) -> None:
        """Write ``draft`` into this unit's module.

        Call it while holding :meth:`build_slot`. That permit keeps two units
        out of the tree at once.
        """
        await self.tree.reconcile(
            self.unit.module,
            UnitEdits(module_path=self.module_path, draft=draft),
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

    The handler explains every violated rule. This callback records evidence.
    An :class:`~composer.prover.ptypes.IncompleteCheck` is not evidence about
    the program. Recording one would let a later pass treat the prover's limit
    as a bug in the program.
    """

    def __init__(self, store: CexAnalysisStore) -> None:
        super().__init__()
        self._store = store
        self._incomplete: dict[str, IncompleteCheck] = {}

    @property
    def incomplete(self) -> Mapping[str, IncompleteCheck]:
        """Rules from the last run that stopped on an assertion the prover generated.

        The counterexample does not survive into
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
                    assertion,
                )
            case PropertyViolation():
                # A capture failure must not fail the run. The verdict is already in hand.
                try:
                    await self._store.record(rule.path, explanation, rule.cex_dump)
                except Exception:
                    _log.exception("cvlr: failed to capture cex analysis for %s", rule.name)


class AuthorModel:
    """The model a counterexample analysis continues a conversation on.

    The graph that owns the conversation binds it once that graph exists.
    Analysis before then has no model to call.
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

    Built per unit. The handler is built per call: it reads the current
    conversation and uses the bound model.
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
                await target.stage(draft)
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


def _submission_for(deps: VerifyDeps, rules: tuple[str, ...] | list[str]) -> Submission:
    """The unit's submission, checking ``rules`` under the submission's conf.

    A conf with no ``rule`` entry makes the cloud job end in FAILED, with no
    report.
    """
    return dataclasses.replace(deps.submission, rules=SelectRules(tuple(rules)))


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
    """Stage ``draft``, build it, and submit it.

    ``state`` is the unit's graph state. The draft is the only input staged.

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
        await deps.target.stage(draft)
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
    When every rule the draft declares verified, it stamps that draft.
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
                    _submission_for(deps, declared),
                    callbacks=capture if capture is not None else _RunAccounting(),
                    cex=analysis.handler(self.state) if analysis else UnanalyzedCexHandler(),
                    tool_call_id=self.tool_call_id,
                )
            return self._report(outcome, deps, declared=declared)

    def _report(
        self,
        outcome: CvlrOutcome,
        deps: VerifyDeps,
        *,
        declared: tuple[str, ...] | list[str],
    ) -> Command | str:
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
            case SubmissionFailed(reason=reason):
                return tool_return(
                    self.tool_call_id,
                    f"The prover run did not produce results: {reason}",
                )
            case Checked(report=report):
                status = report.rule_status
                not_verified = [name for name in declared if status.get(name) is not True]
                if not_verified:
                    return tool_state_update(
                        self.tool_call_id,
                        f"{report.result_str}\n\nNot verified: {', '.join(not_verified)}.",
                        prover_link=report.link,
                    )
                return tool_state_update(
                    self.tool_call_id,
                    f"{report.result_str}\n\nEvery declared rule verified. This draft is stamped.",
                    prover_link=report.link,
                    validations=deps.stamper(self.state),
                )


def gate_tools(target: HarnessTarget, deps: VerifyDeps) -> list[BaseTool]:
    """The tools that compile a draft and submit it.

    Neither tool edits the program.
    """
    return [
        CargoCheck.bind(target).as_tool("cargo_check"),
        VerifyRules.bind(deps).as_tool("verify_rules"),
    ]
