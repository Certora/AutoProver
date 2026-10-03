"""The author's gate tools: a cheap compile, and the prover run that stamps the draft.

Two tiers, exactly as ``docs/cvlr-backend-plan.md`` §5.1 designed them, and the difference in cost is
the reason both exist rather than one:

* ``cargo_check`` — the fast tier. Host target, confined, seconds. Not a required stamp: it is the
  tool the author should reach for after every edit, and gating on it as well as on the prover run
  would gate one fact twice.
* ``verify_rules`` — the slow tier and the gate. Builds for the chain, submits, and stamps the
  draft's digest when the run comes back with every rule accounted for. A build failure never
  reaches the prover (:class:`~composer.spec.cvlr.prover.BuildRejected`), so a compiler error arrives
  as a compiler error with a span in it rather than as a ``CertoraUserInputError`` from inside a
  submission that has already been paid for.

**Accounted for, not all green.** A rule the author has marked an expected failure is excluded from
the check, because on this backend a failing rule is usually a *finding* — the property is real and
the program violates it — and a gate that demanded green would push the author to weaken the rule
until the bug disappeared. The marking is what makes that an explicit, recorded claim instead.
"""

import asyncio
import dataclasses
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import (
    Annotated,
    AsyncIterator,
    Callable,
    Container,
    Literal,
    Mapping,
    Sequence,
    override,
)

from langchain_core.tools import BaseTool
from prover_output_utility import ProverOutputAPI
from pydantic import BaseModel, Discriminator, Field

from graphcore.graph import LLM, tool_return, tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithAsyncImplementation,
    WithImplementation,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.state import ValidationStamper, make_validation_stamper
from composer.diagnostics.timing import get_run_summary
from composer.cargo.features import CargoFeature
from composer.cargo.sbf import Built, PlatformToolsMissing, SbfRun
from composer.cargo.session import CargoSession, CompileFailed, Compiled
from composer.cargo.symbols import defined_functions, nearest, unmatched
from composer.prover.cloud import results_api
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
from composer.prover.conf import SelectRules, dump_conf
from composer.spec.cvlr.conf import (
    DEFAULT_FEATURE,
    PLATFORM_TOOLS_VERSION,
    CollectUnsatCore,
    OptimisticLoop,
    tunable_conf,
)
from composer.spec.cvlr.munge import (
    AlreadyMunged,
    EarlyPanic,
    FunctionAmbiguous,
    Munge,
    FunctionNotFound,
    MockFn,
    MungeKind,
    Munged,
    NotProjectSource,
    apply_munge,
)
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    CvlrOutcome,
    Prepared,
    Submission,
    SubmissionFailed,
    prepare_submission,
    run_submission,
)
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.rules import rule_names
from composer.authoring.state import merge_expected_failures
from composer.spec.cvlr.state import (
    PROVER_VALIDATION_KEY,
    CvlrGenerationState,
    LastVerdicts,
    tuning_history,
)
from composer.spec.cvlr.tree import NotInWorkdir, Reconciled, SharedTree, UnitEdits
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.spec.cvlr.vacuity import VacuityAnalyzer, core_finding
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.source.report_prover import fetch_unsat_cores
from composer.spec.types import CheckName
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)


class BuildPermit:
    """The run's build permit, held by one unit, which can give it up before its slot ends.

    A submission holds it past its own build: the Prover's local phase rebuilds the crate and uploads
    the ``.so``, and every build of the crate writes the same one. It is released as soon as that
    upload is done, rather than when the cloud job finishes.
    """

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._held = False

    async def acquire(self) -> None:
        await self._sem.acquire()
        self._held = True

    def release(self) -> None:
        """Give the permit up. Releasing one already given up does nothing."""
        if self._held:
            self._held = False
            self._sem.release()


@dataclasses.dataclass(frozen=True)
class HarnessTarget:
    """Where a draft is staged so the crate compiles with it in.

    The harness is a module *inside* the crate, so "staging" is writing one file the scaffold already
    declared — not copying a spec next to a conf. ``module_path`` is absolute; the declaration in
    ``specs/mod.rs`` was written once for the whole run
    (:meth:`composer.spec.cvlr.harness.CvlrArtifactStore.declare_modules`).

    Every unit shares ``tree`` and ``build_sem`` and has its own everything else
    (``docs/single-working-tree.md``). The unit's cargo feature is what keeps that safe: a build
    selects ``certora`` plus :attr:`~composer.spec.cvlr.harness.HarnessModule.feature`, so no other
    unit's module is compiled and no other unit's draft can break this gate.
    """

    session: CargoSession
    module_path: Path
    package: str
    #: The package's directory inside the tree. Carried because a munge has to know whether a path
    #: is inside the crate under verification: an edit outside it cannot be gated on this unit's
    #: feature, since the feature is declared on this package's manifest
    #: (``docs/who-edits-the-program.md`` §11.3).
    package_root: Path
    #: The package's tuning files, which ``summarize_for_prover`` rewrites.
    tuning: TuningFiles
    #: This unit's identity — the module name the tree keys edits by, and the cargo feature that
    #: selects it.
    unit: HarnessModule
    #: The run's one working copy, shared with every sibling unit.
    tree: SharedTree
    #: One permit for the whole run, held across staging, the local cargo invocation and, for a
    #: submission, the Prover's own rebuild and upload. Not across the cloud job: that waits for
    #: minutes, and the files a sibling rewrites meanwhile are behind its own feature.
    build_sem: asyncio.Semaphore

    @property
    def features(self) -> tuple[CargoFeature, ...]:
        """The feature set every build of this unit uses — the harness, plus this unit's module."""
        return (DEFAULT_FEATURE, self.unit.feature)

    @property
    def package_dir(self) -> Path:
        """The package's directory relative to the session's workdir, which is how a build is told
        which package to compile.
        """
        return self.package_root.relative_to(self.session.workdir)

    @asynccontextmanager
    async def build_slot(self) -> AsyncIterator[BuildPermit]:
        """Serialize staging and the cargo invocation that follows it.

        For a cargo check, concurrent runs against one ``target/`` would serialize on cargo's own
        build-directory lock anyway, and the per-unit feature is what keeps the shared tree correct
        (``docs/single-working-tree.md`` §2.4). What the permit buys there is a queue the host can
        see instead of a silent stall inside cargo, and not having several sandboxed builds parked
        holding grants.

        For a submission the permit is load-bearing. Every SBF build of the crate writes the same
        ``.so``, and the Prover's local phase rebuilds it and uploads it, so a sibling's build in
        between would replace it with one built for the sibling's feature. See
        :func:`_stage_and_submit`, which releases the permit through the yielded
        :class:`BuildPermit` once the upload is done.
        """
        permit = BuildPermit(self.build_sem)
        await permit.acquire()
        try:
            yield permit
        finally:
            permit.release()

    def pristine_source(self, relative: str) -> Path | NotInWorkdir | NotProjectSource:
        """The developer's copy of a file a munge names, or why the path is not one it may name.

        The tree's own answer (:meth:`composer.spec.cvlr.tree.SharedTree.pristine_of`), asked here
        so a munge is refused when it is *recorded* rather than only when it is replayed. One
        implementation, because a check the tool and the replay could disagree about is a check that
        lets an edit through on one path and not the other.

        Pristine rather than the tree's copy for the same reason: replay acts on the pristine
        source, so a tool that dry-runs an edit against a tree already carrying a sibling unit's
        must reach a different verdict from the replay that follows it.
        """
        return self.tree.pristine_of(relative)

    def in_package(self, pristine: Path) -> bool:
        """Whether a file :meth:`pristine_source` answered with belongs to the package under
        verification.

        Asked on the pristine side because that is where :meth:`pristine_source` points, while
        :attr:`package_root` names the package inside the tree — comparing one against the other
        finds every file of the package foreign to it.
        """
        pristine_package = self.tree.pristine / self.package_root.relative_to(self.tree.root)
        return pristine.resolve().is_relative_to(pristine_package.resolve())

    async def stage(
        self,
        draft: str,
        summaries: Sequence[SummaryDirective] = (),
        munges: Sequence[Munge] = (),
    ) -> Reconciled:
        """Make the tree agree with this unit's state, and report what could not be carried over.

        All three inputs from one place, because all three are inputs to the build: the prover reads
        the tuning file through the conf, and a munge changes the program it compiles. Each comes
        from already-merged state rather than from the tool that recorded one, so two concurrent
        calls cannot each write their own view of the list and lose the other's.

        Everything here is written **from state**, not edited in place. The summaries file is
        rewritten wholesale even when there are none — the composite the conf names has to exist, and
        a directive dropped from state has to disappear. The munges are replayed onto the pristine
        source by :meth:`composer.spec.cvlr.tree.SharedTree.reconcile`, which is what makes the tree
        derivable after a crash, a rewind, or a cache replay with no tree at all
        (``docs/single-working-tree.md`` §4).

        Call it while holding :meth:`build_slot`. It is the tree's only writer, and the permit is
        what keeps two units out of it at once.
        """
        self.tuning.write(tuple(summaries))
        return await self.tree.reconcile(
            self.unit.module,
            UnitEdits(
                module_path=self.module_path, draft=draft, munges=tuple(munges)
            ),
        )


class _RunAccounting(ProverCallbacks):
    """The run's prover bookkeeping: the link, the prover's own runtime, and the wall clock.

    ``ProverCallbacks``' defaults are no-ops, so a backend that overrides only the events it cares
    about silently opts out of all three — which is how every CVLR run so far reported zero prover
    time while spending most of its wall clock in the cloud. These are the ``_SpecCallbacks`` (EVM)
    set, and they are what ``summary.format()``, the per-task link and ``job_info.json``'s
    ``prover_usage`` are computed from.

    A base of its own rather than three more methods on :class:`_CaptureCallbacks`, because the two
    are needed independently: a submission with no analysis store still costs prover time.
    """

    def __init__(self) -> None:
        super().__init__()
        self._started_mono: float | None = None
        self._upload_permit: BuildPermit | None = None

    def release_on_upload(self, permit: BuildPermit) -> None:
        """Give ``permit`` up when the Prover reports the job's link, which is after its local
        rebuild and upload. Until then a sibling's build could replace the ``.so`` it uploads."""
        self._upload_permit = permit

    @override
    async def on_prover_run(self, args: list[str]) -> None:
        self._started_mono = time.perf_counter()

    @override
    async def on_prover_link(self, link: str) -> None:
        if self._upload_permit is not None:
            self._upload_permit.release()
        self._record_link(link)

    def _record_link(self, link: str) -> None:
        get_run_summary().record_prover_link(link)

    @override
    async def on_prover_runtime(self, ms: int) -> None:
        """The prover's own queue-free start-to-end time, which is the number a cost question is
        actually about — distinct from the wall clock in :meth:`on_prover_result`, which includes
        however long the job sat in the queue."""
        get_run_summary().record_prover_runtime(ms)

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        if self._started_mono is not None:
            get_run_summary().add_prover_call(time.perf_counter() - self._started_mono)
            self._started_mono = None


class _CaptureCallbacks(_RunAccounting):
    """Keeps each violated rule's analysis where the report phase can find it.

    Two responsibilities, and the split between them is the point:

    * The *handler* explains every violated rule to the author. An unwound loop bound is worth
      explaining — the author can raise ``loop_iter`` or constrain the loop — so nothing is filtered
      out of that path.
    * This callback decides what becomes **evidence**. An :class:`IncompleteCheck` is not evidence
      about the program, so recording one would hand the findings synthesizer a counterexample and
      let it write up the prover's own limits as a bug in the code under verification.
    """

    def __init__(self, store: CexAnalysisStore) -> None:
        super().__init__()
        self._store = store
        self._incomplete: dict[str, str] = {}

    @property
    def incomplete(self) -> Mapping[str, str]:
        """The last run's rules that stopped on an assertion the prover generated, and which one.

        Read by the gate, which has to say something different about these than about a rule whose
        own assertion failed. Derived here rather than in ``_report`` because the counterexample it
        is derived from does not survive into :class:`~composer.prover.core.ProverReport`, which
        carries statuses only.
        """
        return self._incomplete

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        # This run supersedes what was captured for the rules it covers. Dropping their old records
        # before the handler writes fresh ones is what stops a rule that failed in an earlier
        # iteration and passes now from surviving into the report as a current failure. Fires before
        # the handler by contract, so what is recorded below is always this run's.
        await super().on_prover_result(results)
        self._incomplete = {}
        for result in results.values():
            if result.status != "VIOLATED":
                continue
            match classify_violation(result.counterexample):
                case IncompleteCheck(assertion=assertion):
                    self._incomplete[result.path.rule] = assertion
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
                # Never let a capture failure disturb the run: the verdict is already in hand and
                # the report can render without the explanation.
                try:
                    await self._store.record(rule.path, explanation, rule.cex_dump)
                except Exception:
                    _log.exception("cvlr: failed to capture cex analysis for %s", rule.name)


class AuthorModel:
    """The author's model as the author's graph binds it, tools included.

    A counterexample analysis continues the author's conversation, so it must be sent with the
    author's exact tools, system prompt and settings: those come first in the request, and any
    difference there makes the provider's prompt cache miss the whole conversation behind them. The
    graph binds the model only when it is built, which is after the tools that need it, so the author
    binds it here once the graph exists.
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
    """What turns a violated rule into a finding: something to explain it, somewhere to keep it.

    Built per unit rather than per submission, but the handler itself has to be built per *call* —
    it reads the author's live conversation as context for the explanation, which is exactly what
    makes its account of a counterexample worth more than the trace alone. It explains on the
    author's own model, for the cache, and so on the author's tier.
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
    #: The directory :func:`composer.spec.cvlr.prover.prepare_submission` writes into. Outside the
    #: tree, which the confined build can write.
    submissions: Path
    prover_opts: ProverOptions
    stamper: ValidationStamper
    #: ``None`` runs the prover with no analysis at all — the plumbing tests and any caller with no
    #: LLM. A run that means to produce findings has to supply this.
    analysis: CexAnalysis | None = None
    #: One submission at a time per unit. A second concurrent run would build into the same workdir
    #: and race the staged module; the tool refuses rather than serializing silently, because a
    #: caller that made two calls wanted two answers.
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


def _unaccounted(
    status: dict[str, bool],
    expected_failures: dict[CheckName, str],
    incomplete: Container[str] = (),
) -> list[str]:
    """Rules that failed and were not declared as expected failures.

    An incomplete check cannot be declared away, which is why the marking does not excuse one:
    ``expect_rule_failure`` claims the failure exposes a real defect, and a rule that stopped on the
    prover's own generated assertion never reached its property, so it has shown no defect at all.
    Letting the marking through would publish a finding-shaped claim with nothing behind it — and
    nothing behind it in a literal sense, since such a rule contributes no evidence either
    (:class:`_CaptureCallbacks`).
    """
    return sorted(
        name
        for name, ok in status.items()
        if not ok and (name not in expected_failures or name in incomplete)
    )


def _stamp_under(last: LastVerdicts, expected: dict[CheckName, str]) -> dict[str, str]:
    """The prover stamp ``last`` earns under the markings ``expected``: its own stamp when they
    account for every failure it reported, otherwise a cleared one."""
    if last.unverdicted or _unaccounted(last.status, expected, last.incomplete):
        return {key: "" for key in last.stamp}
    return last.stamp


def _externals_note(externals: Sequence[str]) -> str | None:
    """What the author is told about the functions this run treated as external, if there were any.

    Not a gate: an external call is how the starting configuration keeps most of a platform's code
    out of the analysis, and nothing in the author's action space inlines one. What the author can
    do is know where its verdicts stop meaning what they say, and, where the program's own code
    makes the call, bound the value with a stand-in.

    A bound is often the better remedy even where inlining is possible: platform code such as stake
    activation loops once per epoch over a sysvar read by syscall, in ``f64``, and analyzed it
    leaves a rule vacuous on the unwinding assertion.
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
        "extract the call into a function of its own (`extract_function`), then to replace that "
        "function with a stand-in you write (`mock_fn`) that returns `nondet()` values constrained "
        "by what the real function guarantees, such as a result no larger than its input. Use "
        "only bounds the real function provably keeps: a tighter one can pass a rule the program "
        "fails. Name the stand-in in `rule_subjects`. Otherwise say so in the rule's commentary, "
        "drive the program's own code below the call, or skip the property."
    )


def _unverdicted(declared: Sequence[str], status: Mapping[str, bool]) -> list[str]:
    """Declared rules the report gave no verdict for.

    Counted against the draft, not ignored: the verdict roll-up only sees rules the report names,
    so a rule missing from it would otherwise leave "every rule is accounted for" true.
    """
    return sorted(set(declared) - status.keys())


def _wrongly_expected(status: dict[str, bool], expected_failures: dict[CheckName, str]) -> list[str]:
    """Rules marked as expected failures that in fact verified.

    Reported because it is the more interesting direction: a rule the author believed exposed a bug
    and which passes means either the bug is not there or the rule does not test for it, and both
    want the author's attention before the run is called finished."""
    return sorted(name for name in expected_failures if status.get(name) is True)


def _drift_note(reconciled: Reconciled) -> str:
    """What to tell the author when a munge in state did not reach the file.

    Surfaced rather than logged. A munge is replayed onto the *pristine* project, which can have
    moved since the munge was recorded — a resumed run, a cache replay, or a developer editing
    underneath the run — and the failure is otherwise mute: the build succeeds, the report still
    carries a source-edit record, and the property was checked against code the record does not
    describe. ``docs/single-working-tree.md`` §4.3 is the argument for this being a return value.
    """
    if not reconciled.drifted:
        return ""
    lines = "\n".join(f"  - {d.describe()}" for d in reconciled.drifted)
    return (
        f"\n\nWARNING: {len(reconciled.drifted)} recorded munge(s) could not be applied to the "
        f"program source, so this build does not contain them:\n{lines}\n"
        "The source they name has changed since they were recorded. Re-record them against the "
        "code as it is now, or drop the rules that depended on them."
    )


@tool_display("Compiling the harness", "Compile")
class CargoCheck(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Compile the current draft against the program, on the host target.

    Fast (seconds) and free — call it after every edit. It catches an unknown macro, a wrong
    signature or a misused derive, which is the class of mistake worth catching before paying for a
    prover run. It does *not* catch what only the chain build can, so it is not a substitute for
    ``verify_rules``.
    """

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        with self.tool_deps() as target:
            async with target.build_slot():
                reconciled = await target.stage(
                    draft, self.state["summaries"], self.state["munges"]
                )
                run = await target.session.check(
                    manifest_dir=target.package_dir, features=target.features
                )
        match run.verdict:
            case Compiled():
                declared = rule_names(draft)
                names = ", ".join(declared) if declared else "none"
                return tool_return(
                    self.tool_call_id,
                    f"Compiled in {run.duration_ms}ms. Rules declared: {names}."
                    + _drift_note(reconciled),
                )
            case CompileFailed(diagnostics=diagnostics):
                return tool_return(
                    self.tool_call_id,
                    f"Does not compile:\n{diagnostics}" + _drift_note(reconciled),
                )


def _submission_for(
    deps: VerifyDeps, state: CvlrGenerationState, rules: Sequence[str]
) -> Submission:
    """The unit's submission, checking ``rules`` under the author's current prover settings.

    The settings come from state, not from ``deps``: the conf is the author's to change, and a
    submission built from the run's starting copy would send the old settings while
    ``version_history`` recorded the new ones.
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
) -> tuple[Reconciled, CvlrOutcome]:
    """Stage ``draft`` with the unit's summaries and munges, build it, and submit it.

    One build permit covers all of it until the Prover has uploaded. ``run_submission`` rebuilds the
    crate into the target directory every build shares and uploads that ``.so``, so a sibling must
    not build between this unit's gate build and that upload
    (:func:`composer.spec.cvlr.prover.prepare_submission`). The permit is released when the job's
    link arrives, which is after the upload, or when ``run_submission`` returns without one. The
    cloud job's minutes are not covered: the files a sibling stages meanwhile are behind its own
    feature (``docs/single-working-tree.md`` §2.4).
    """
    async with deps.target.build_slot() as permit:
        reconciled = await deps.target.stage(draft, state["summaries"], state["munges"])
        prepared = await prepare_submission(
            deps.target.session, submission, into=deps.submissions
        )
        if isinstance(prepared, BuildRejected):
            return reconciled, prepared
        callbacks.release_on_upload(permit)
        outcome = await run_submission(
            deps.target.session,
            prepared,
            prover_opts=deps.prover_opts,
            callbacks=callbacks,
            cex=cex,
            tool_call_id=tool_call_id,
        )
    return reconciled, outcome


@tool_display("Running the Solana Prover", "Prover")
class VerifyRules(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, VerifyDeps],
):
    """Build the program with your harness and check its rules with the Certora Solana Prover.

    Minutes, and it costs real prover time — get ``cargo_check`` green first. On success this stamps
    your current draft, which is one of the two things ``result`` requires.

    A rule that fails is not automatically a problem with your rule: if you believe it exposes a real
    defect, mark it with ``expect_rule_failure`` and say why. Rules so marked are excluded from this
    gate, so the run can be complete with a genuine violation still in it.
    """

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        declared = rule_names(draft)
        if not declared:
            return self._nothing_to_submit()
        with self.tool_deps() as deps:
            if deps.lock.locked():
                return (
                    "A prover run for this unit is already in flight. Wait for it rather than "
                    "starting a second one."
                )
            async with deps.lock:
                analysis = deps.analysis
                capture = analysis.callbacks() if analysis else None
                # Name exactly the rules this draft declares. Not a refinement: a conf with no
                # `rule` entry makes the cloud job end in FAILED, with no report and nothing on disk
                # to read, so *every* submission this backend made failed until this line named
                # them. Not every rule either — a build compiles this unit's module and the artifact
                # declares every unit's rules, so this unit would be graded on its siblings' drafts.
                reconciled, outcome = await _stage_and_submit(
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
                drift=_drift_note(reconciled),
            )

    def _nothing_to_submit(self) -> Command | str:
        """A draft with no rules: either unfinished, or a unit whose every property is blocked.

        The second case has to be able to finish. A unit that skips everything — the honest outcome
        when the prover cannot analyze the handler its properties are about — declares no rules, so
        it could never earn this stamp, so ``result`` refused it and ``give_up`` was the only way
        out. That reported a considered "nothing here is formalizable, and here is why" as a
        failure, which is both wrong and the shape the skip guidance had just started encouraging.

        Stamping is safe because it is not the only gate: ``result`` still requires every property to
        be either skipped or mapped to a declared rule, and the judge still has to accept the skips.
        A draft with no rules and no skips is the unfinished case and gets nothing.
        """
        if not self.state["skipped"]:
            return (
                "Your draft declares no rules and no property is skipped, so there is nothing to "
                "check. A rule is a `#[rule]` function or a `cvlr_rules!` invocation."
            )
        with self.tool_deps() as deps:
            return tool_state_update(
                self.tool_call_id,
                "No rules to submit — every property you have not skipped would need one, and you "
                "have skipped them all. Nothing was submitted and no prover time was spent. The "
                "prover gate is satisfied by that; the judge still has to accept your skip "
                "reasons, so make each one name what blocked it.",
                validations=deps.stamper(self.state, tuning_history(self.state)),
            )

    def _inert_summaries(self, build: SbfRun) -> str | None:
        """A note naming the summary directives this build's symbols do not match.

        The failure it reports is total silence. A summary is a regex over demangled symbol names, so
        one that names a symbol the build does not define changes nothing and produces no
        diagnostic — the run reports the error it reported before. An end-to-end run wrote five
        variants of one directive hunting for a spelling that took, and *all five* missed: the
        program's own ``VaultError::Display`` had been inlined out of existence, so no spelling would
        have worked, and the symbol it needed was Anchor's ``ErrorCode::Display``, which survives.

        Best-effort, and silent when the symbols cannot be read: an unreadable artifact must not
        become "your directives matched nothing", which is a different problem with a different fix.
        """
        directives = tuple(d.pattern for d in self.state["summaries"])
        if not directives or not isinstance(build.verdict, Built):
            return None
        try:
            symbols = defined_functions(
                build.verdict.manifest.artifact, tools_version=PLATFORM_TOOLS_VERSION
            )
        except (PlatformToolsMissing, OSError):
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
        incomplete: Mapping[str, str],
        *,
        declared: Sequence[str],
        drift: str = "",
    ) -> Command | str:
        """What the gate says, with any replay drift appended.

        ``drift`` rides on every branch rather than only the green one: a munge that did not reach
        the build is as much a part of why a rule failed as of what a passing rule means."""
        stamper = deps.stamper
        expected = self.state["expected_failures"]
        match outcome:
            case BuildRejected(build=build):
                # Never a submission, so nothing was spent. The compiler's own text is the most
                # actionable thing the author can be handed — and it is on the verdict rather than
                # the run, because only a failed build has any.
                # The slow tier reports a failure as the same `CompileFailed` the fast tier
                # does, so the author reads one shape of compiler output whichever tier caught it.
                said = (
                    build.verdict.diagnostics
                    if isinstance(build.verdict, CompileFailed)
                    else "no diagnostics were captured"
                )
                return tool_return(
                    self.tool_call_id,
                    f"The chain build failed, so nothing was submitted:\n{said}" + drift,
                )
            case SubmissionFailed(build=build, reason=reason):
                said = [f"The prover run did not produce results: {reason}"]
                if (inert := self._inert_summaries(build)) is not None:
                    said.append(inert)
                return tool_return(self.tool_call_id, "\n\n".join(said) + drift)
            case Checked(build=build, report=report):
                status = report.rule_status
                unaccounted = _unaccounted(status, expected, incomplete)
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
                    named = "\n".join(
                        f"  {name}: {assertion}" for name, assertion in sorted(incomplete.items())
                    )
                    lines.append(
                        "These rules stopped on an assertion the prover generated rather than one "
                        f"of yours, so they never reached their property:\n{named}\n"
                        "Nothing here is a statement about the program. Constrain what determines "
                        "the trip count, summarize the loop if it is not what your property is "
                        "about, or skip the property and say what bound it needed. Do not mark one "
                        "of these with expect_rule_failure: that claims a real defect, and none "
                        "has been shown."
                    )
                if unverdicted:
                    lines.append(
                        f"No verdict came back for: {', '.join(unverdicted)}. The prover's report "
                        "does not name them, so they have not been checked, and this draft cannot "
                        "be stamped until they are. Run verify_rules again; if they are still "
                        "missing, the run's link is where to look."
                    )
                if unaccounted:
                    lines.append(
                        f"Not accounted for: {', '.join(unaccounted)}. Fix the rule, or mark it "
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
                if unaccounted or unverdicted:
                    return tool_state_update(
                        self.tool_call_id,
                        "\n\n".join(lines) + drift,
                        prover_link=report.link,
                        external_functions=report.external_functions,
                        last_verdicts=last,
                    )
                return tool_state_update(
                    self.tool_call_id,
                    "\n\n".join([*lines, "Every rule is accounted for. This draft is stamped."])
                    + drift,
                    prover_link=report.link,
                    external_functions=report.external_functions,
                    last_verdicts=last,
                    validations=last.stamp,
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
        declared = rule_names(draft)
        if self.rule not in declared:
            return (
                f"Your draft declares no rule `{self.rule}`. It declares: "
                f"{', '.join(declared) or 'none'}."
            )
        with self.tool_deps() as deps:
            verify = deps.verify
            if verify.lock.locked():
                return (
                    "A prover run for this unit is already in flight. Wait for it rather than "
                    "starting a second one."
                )
            async with verify.lock:
                submission = dataclasses.replace(
                    _submission_for(verify, self.state, (self.rule,)),
                    purpose=CollectUnsatCore(),
                    # Its own conf file: the unit's is part of the deliverable.
                    stem=f"{verify.submission.stem}_unsat_core",
                )
                reconciled, outcome = await _stage_and_submit(
                    verify,
                    self.state,
                    draft,
                    submission,
                    callbacks=_DiagnosticAccounting(),
                    cex=UnanalyzedCexHandler(),
                    tool_call_id=self.tool_call_id,
                )
            match outcome:
                case BuildRejected():
                    return (
                        "The chain build failed, so nothing was submitted. Run verify_rules to see "
                        "the compiler's diagnostics." + _drift_note(reconciled)
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
                within_tool=self.tool_call_id,
            )
            return "\n\n".join(
                [finding.summary(), analysis.format(), f"The diagnostic run: {report.link}"]
            )


def gate_tools(
    target: HarnessTarget, deps: VerifyDeps, analyzer: VacuityAnalyzer
) -> list[BaseTool]:
    """The gate tools and the one tool that changes what they check, named as the prompt refers to
    them.

    ``munge_function`` is deliberately absent. Editing the program under verification belongs to one
    entity and it is not the author (:mod:`composer.spec.cvlr.editor`,
    ``docs/who-edits-the-program.md`` §4); what the author gets instead is ``code_editor`` and
    ``revert_munge``, bound where the rest of its tools are."""
    return [
        CargoCheck.bind(target).as_tool("cargo_check"),
        VerifyRules.bind(deps).as_tool("verify_rules"),
        ExplainVacuity.bind(VacuityDeps(deps, analyzer, results_api)).as_tool("explain_vacuity"),
        SummarizeForProver.bind(target.tuning).as_tool("summarize_for_prover"),
        AdjustProverConfig.as_tool("adjust_prover_config"),
    ]


def prover_stamper() -> ValidationStamper:
    return make_validation_stamper(PROVER_VALIDATION_KEY)


@tool_display(lambda p: f"Summarizing `{p['symbol_pattern']}` for the prover", "Summary")
class SummarizeForProver(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, TuningFiles],
):
    """Tell the prover to replace a function with an unconstrained stand-in instead of analyzing it.

    This is the remedy for a rule that comes back with **no verdict** because the pointer analysis
    refused something on one of its paths — [3308] on an Anchor program's ``#[error_code]`` enum
    formatting its ``#[msg]`` string is the common case, and it is reached by any ``require!`` or
    ``?`` in a handler you call. Summarizing that formatting code makes the rest of the handler
    analyzable.

    **A summary is unsound, and it does not fail loudly.** The prover stops reasoning about the
    function: the call returns an unconstrained value and writes nothing beyond the locations
    ``returns`` names, so every other store the real function makes is dropped. Summarizing the code your property is actually about can produce a
    rule that passes having checked nothing — state the function should have changed keeps its old
    value — with no trace in the harness. Summarize only what your properties do not depend on.
    Never summarize a handler, a function that writes state you assert over, or one that computes a
    value you assert over.

    Adding one invalidates the prover stamp, because the previous run's verdicts were about a
    different build: re-run ``verify_rules`` afterwards.
    """

    symbol_pattern: str = Field(
        description="A regex over demangled symbols, spelled the way the tuning files do and "
        "normally anchored — e.g. `^<vault::VaultError as core::fmt::Display>::fmt$`. The prover "
        "error names the symbol; copy it from there rather than guessing."
    )
    why: str = Field(
        description="Why analyzing it fails, and why replacing it with a stand-in is sound for the "
        "properties in this batch. This is written into the project's tuning file and carried into "
        "the report — it is the only account a reader gets of what was assumed."
    )
    returns: str | None = Field(
        default=None,
        description="The `#[type(...)]` body, without the wrapper, when the summarized function's "
        "return value needs a shape — e.g. `(*i32)(r1+0):num`. Omit for an unconstrained return, "
        "which is right for a function whose result nothing asserts over.",
    )

    @override
    async def run(self) -> Command | str:
        if not self.why.strip():
            return (
                "A non-empty `why` is required. A summary makes the prover stop reasoning about a "
                "function, so an unexplained one is indistinguishable from a rule that proves "
                "nothing."
            )
        with self.tool_deps() as tuning:
            absent = tuning.missing()
        if absent:
            # The scaffold owns these files, so this is a scaffold failure surfacing late. Refused
            # rather than recorded, because a summary in a file the conf does not name changes
            # nothing and the author would read success and get an identical [3308].
            return (
                f"This project has no {', '.join(absent)}, so the prover is not reading any tuning "
                "file and a summary would have no effect. Report this rather than working around it."
            )
        directive = SummaryDirective(
            pattern=self.symbol_pattern, why=self.why, returns=self.returns
        )
        if any(d.pattern == directive.pattern for d in self.state["summaries"]):
            return f"{directive.pattern} is already summarized."
        # Only this directive: the state reducer merges, and the build path is what writes the
        # tuning file. A tool that wrote the file itself would be racing its own siblings.
        return tool_state_update(
            self.tool_call_id,
            f"Recorded a summary for {directive.pattern}. It takes effect on the next build, and it "
            "invalidated the prover stamp — re-run verify_rules.",
            summaries=[directive],
        )


class SetLoopIter(BaseModel):
    """Raise or lower how many times the prover unrolls a statically unbounded loop."""

    type: Literal["loop_iter"]
    iterations: int = Field(
        description="The new bound. The Prover unrolls an unbounded loop this many times and then "
        "*asserts* the loop has finished — while `optimistic_loop` is off, which is the default, a "
        "rule that verifies at this bound is verified, and one that cannot comes back VIOLATED on "
        "\"Unwinding condition in a loop\" rather than passing quietly. Set it as low as the "
        "property allows: cost grows "
        "exponentially, and above 3 or 4 it grows faster than the answer is usually worth. Raising "
        "it is the last of the three remedies, after bounding whatever determines the trip count "
        "and after asking whether the loop is in your property's way at all."
    )


class SetOptimisticLoop(BaseModel):
    """Assume every loop finishes within the bound, instead of asserting that it does."""

    type: Literal["optimistic_loop"]
    enabled: bool = Field(
        description="True to assume loops finish, false to go back to asserting it. **This is the "
        "one setting here that changes what a verdict means.** With it on, the Prover stops "
        "checking that the unroll bound was enough, so a violation reachable only on a later "
        "iteration is never found and the rule reports VERIFIED anyway. Every rule in the "
        "submission is affected, not the one you were fighting. "
        "It is for exactly one situation: a loop whose trip count the analysis cannot fix, where "
        "raising the bound provably does not terminate — the bound goes up and the reported "
        "iteration goes up with it — and where the loop is somewhere no summary directive can name, "
        "so it cannot be constrained or munged either. Establish that first, by moving the bound and "
        "reading what comes back; a loop that discharges at 3 is not this case. The honest "
        "alternative when it is this case is `record_skip` naming the loop, and that is the better "
        "answer when the property is about the loop itself. What this buys is the properties that "
        "are not: with it on, the rest of the handler is reachable again. Say in `why` which "
        "bounds you tried and what they reported — a reader has to weigh every verdict in the unit "
        "against it."
    )


type CvlrConfigEdit = Annotated[
    SetLoopIter | SetOptimisticLoop, Discriminator("type")
]


@tool_display(lambda p: f"Adjusting the prover config ({len(p['edits'])} edit(s))", "Config")
class AdjustProverConfig(
    WithInjectedState[CvlrGenerationState], WithInjectedId, WithImplementation[Command | str]
):
    """Change a prover setting for this unit's submissions.

    The conf is in your system prompt; this changes it. The list is short and closed. The loop bound
    is **sound**: it decides how the prover spends its time, never what a green verdict means.

    `optimistic_loop` is the exception, and it is here because the sound ladder has a gap rather
    than because the rule was relaxed: a trip count the analysis cannot fix is answered by none of
    bounding the inputs, munging the loop, or raising the bound. Turning it on makes every verdict
    in this unit conditional on an assumption nothing checked, so it is the last thing to reach for
    and the first thing a reader of the deliverable will want explained. What stays out of your
    hands entirely is a `rule_sanity` downgrade, which stops vacuity being reported, and the
    memory-model flags, which are unsound by name and measurably fix nothing.

    **Reach for this after your own remedies, not before them.** A timeout is usually telling you
    something about the rule: an unbounded operand, an algebraic step that belongs in a lemma, a
    loop whose trip count nothing constrains. A setting that makes the symptom go away without
    answering that leaves a proof nobody understands as well, which is a worse thing to ship than a
    slow one.

    Edits apply together or not at all, and the result is the full conf. Changing it invalidates the
    prover stamp, because the previous run's verdicts were obtained under different settings: re-run
    `verify_rules` afterwards.
    """

    edits: list[CvlrConfigEdit] = Field(description="The changes to apply, as one atomic batch.")
    why: str = Field(
        description="What you tried first and why this setting is the remedy. The conf is written "
        "into the deliverable, so this is the account a reader gets of why it differs from the "
        "starting one."
    )

    @override
    def run(self) -> Command | str:
        if not self.why.strip():
            return (
                "A non-empty `why` is required. The conf ships with the deliverable, and a setting "
                "nobody explained is one a reader cannot weigh."
            )
        if not self.edits:
            return "No edits given."
        settings = self.state["prover_settings"]
        for edit in self.edits:
            match edit:
                case SetLoopIter(iterations=n):
                    if n < 1:
                        return f"A loop bound of {n} is not a bound; it has to be at least 1."
                    if settings.loop_iter == n:
                        return f"`loop_iter` is already {n}."
                    settings = dataclasses.replace(settings, loop_iter=n)
                case SetOptimisticLoop(enabled=on):
                    if (settings.optimistic_loop is not None) == on:
                        return (
                            "`optimistic_loop` is already "
                            + ("on" if on else "off")
                            + " in this conf."
                        )
                    settings = dataclasses.replace(
                        settings, optimistic_loop=OptimisticLoop(why=self.why) if on else None
                    )
        return tool_state_update(
            self.tool_call_id,
            "Prover config updated; the prover stamp is invalidated, so re-run `verify_rules`.\n\n"
            f"```json\n{dump_conf(tunable_conf(settings))}```",
            prover_settings=settings,
        )


def _remarked(
    state: CvlrGenerationState, tool_call_id: str, said: str, marking: dict[CheckName, str]
) -> Command:
    """Apply ``marking`` and re-decide the prover stamp against the last run's verdicts."""
    last = state.get("last_verdicts")
    if last is None:
        return tool_state_update(tool_call_id, said, expected_failures=marking)
    stamp = _stamp_under(last, merge_expected_failures(state["expected_failures"], marking))
    current = prover_stamper()(state, tuning_history(state))
    held = state["validations"].get(PROVER_VALIDATION_KEY) == current[PROVER_VALIDATION_KEY]
    if stamp == current and not held:
        said += (
            " Every failure the last prover run reported is now accounted for, so this draft is "
            "stamped without another run."
        )
    elif stamp != current and held:
        said += (
            " The last prover run's failures are no longer all accounted for, so this draft's "
            "prover stamp is withdrawn."
        )
    return tool_state_update(tool_call_id, said, expected_failures=marking, validations=stamp)


@tool_display(lambda p: f"Expecting rule `{p['rule_name']}` to fail", None)
class ExpectRuleFailure(
    WithInjectedState[CvlrGenerationState], WithAsyncImplementation[Command], WithInjectedId
):
    """Declare that a rule is *meant* to fail because the program violates the property.

    This is how a real finding is recorded rather than argued away. The rule stays in the harness,
    the prover keeps reporting the violation, and ``verify_rules`` stops treating it as unfinished
    work. Use it only when you have read the counterexample and believe the defect is real. When
    the marking accounts for the last failure of the last run, the draft is stamped as that run
    would have stamped it, with no need to run again.
    """

    rule_name: str = Field(description="The name of the rule expected to fail")
    reason: str = Field(
        description="Why the failure is a genuine defect in the program rather than in the rule"
    )

    @override
    async def run(self) -> Command:
        # The merge reads an empty reason as "remove the marking", so an empty one must not get
        # through — an unexplained expected failure is indistinguishable from a broken rule.
        if not self.reason.strip():
            return tool_state_update(
                self.tool_call_id,
                "A non-empty reason is required when marking a rule as expected to fail.",
            )
        return _remarked(
            self.state,
            self.tool_call_id,
            f"Recorded: {self.rule_name} is expected to fail.",
            {CheckName(self.rule_name): self.reason},
        )


@tool_display(lambda p: f"Expecting rule `{p['rule_name']}` to pass", None)
class ExpectRulePassage(
    WithInjectedState[CvlrGenerationState], WithAsyncImplementation[Command], WithInjectedId
):
    """Withdraw an ``expect_rule_failure`` marking, putting the rule back under the gate. If the
    last run reported the rule failing, the draft's prover stamp goes with it."""

    rule_name: str = Field(description="The name of the rule that should verify after all")

    @override
    async def run(self) -> Command:
        return _remarked(
            self.state,
            self.tool_call_id,
            f"Withdrawn: {self.rule_name} must verify.",
            {CheckName(self.rule_name): ""},
        )
