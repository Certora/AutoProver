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
from pathlib import Path
from pathlib import PurePosixPath
from typing import Annotated, Container, Literal, Mapping, Sequence, override

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from graphcore.graph import LLM, tool_return, tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithAsyncImplementation,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.state import ValidationStamper, make_validation_stamper
from composer.cargo.sbf import Built, PlatformToolsMissing, SbfRun
from composer.cargo.session import CargoSession, CompileFailed, Compiled
from composer.cargo.symbols import defined_functions, nearest, unmatched
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
from composer.spec.cvlr.conf import DEFAULT_FEATURE, SelectRules, tools_version
from composer.spec.cvlr.munge import (
    AlreadyMunged,
    EarlyPanic,
    FunctionAmbiguous,
    FunctionMunge,
    FunctionNotFound,
    MockFn,
    MungeKind,
    Munged,
    NotProjectSource,
    apply_munge,
    function_names,
    is_project_source,
)
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    CvlrOutcome,
    Submission,
    SubmissionFailed,
    submit,
)
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.state import (
    PROVER_VALIDATION_KEY,
    CvlrGenerationState,
    tuning_history,
)
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.types import CheckName
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class NotInWorkdir:
    """The path resolves outside this unit's copy of the project."""

    path: str


@dataclasses.dataclass(frozen=True)
class HarnessTarget:
    """Where a draft is staged so the crate compiles with it in.

    The harness is a module *inside* the crate, so "staging" is writing one file the scaffold already
    declared — not copying a spec next to a conf. ``module_path`` is absolute; the declaration in
    ``specs/mod.rs`` was written once for the whole run
    (:meth:`composer.spec.cvlr.harness.CvlrArtifactStore.declare_modules`).
    """

    session: CargoSession
    module_path: Path
    package: str
    #: The package's tuning files, which ``summarize_for_prover`` rewrites.
    tuning: TuningFiles

    def source_path(self, relative: str) -> Path | NotInWorkdir | NotProjectSource:
        """Resolve a workdir-relative path a munge may write to, or say why it may not.

        Two things have to hold and they are not the same one. The path must stay **inside** this
        unit's workdir, which is what keeps a munge from reaching the user's tree. And it must name
        the **project's own source**, which containment does not establish: confinement puts this
        unit's ``CARGO_HOME`` at ``<workdir>/.sandbox_cargo``, so every dependency's unpacked source
        is inside the workdir too, and a check that stopped at containment would let a munge rewrite
        Anchor for every crate in the graph.
        """
        root = self.session.workdir.resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            return NotInWorkdir(relative)
        inside = candidate.relative_to(root)
        if not is_project_source(PurePosixPath(inside)):
            return NotProjectSource(path=relative, directory=inside.parts[0])
        return candidate

    def stage(
        self,
        draft: str,
        summaries: Sequence[SummaryDirective] = (),
        munges: Sequence[FunctionMunge] = (),
    ) -> None:
        """Put the draft, the tuning directives and the source munges on disk, ready to build.

        All three from one place, because all three are inputs to the build: the prover reads the
        tuning file through the conf, and a munge changes the program it compiles. Each comes from
        already-merged state rather than from the tool that recorded one, so two concurrent calls
        cannot each write their own view of the list and lose the other's.

        Munges are re-applied from whatever is on disk rather than from a pristine copy, which is
        safe because :func:`~composer.spec.cvlr.munge.apply_munge` reports an attribute already
        present instead of adding a second one.
        """
        self.module_path.parent.mkdir(parents=True, exist_ok=True)
        self.module_path.write_text(draft)
        if summaries:
            self.tuning.write(tuple(summaries))
        for munge in munges:
            path = self.source_path(munge.path)
            if not isinstance(path, Path) or not path.is_file():
                # Recorded against a path that is no longer writable or no longer there. Not this
                # method's failure to report — the tool refused anything unwritable when it was
                # recorded, and a missing file the build is about to name with a span in it.
                _log.warning("cvlr: cannot apply munge to %s", munge.path)
                continue
            match apply_munge(path.read_text(), munge, DEFAULT_FEATURE):
                case Munged(source=source):
                    path.write_text(source)
                case other:
                    _log.info("cvlr: munge of %s not re-applied: %s", munge.edit_id, other)


class _CaptureCallbacks(ProverCallbacks):
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


@dataclasses.dataclass(frozen=True)
class CexAnalysis:
    """What turns a violated rule into a finding: something to explain it, somewhere to keep it.

    Built per run rather than per submission, but the handler itself has to be built per *call* —
    it reads the author's live conversation as context for the explanation, which is exactly what
    makes its account of a counterexample worth more than the trace alone.
    """

    llm: LLM
    store: CexAnalysisStore

    def handler(self, state: CvlrGenerationState) -> CexHandler:
        return TrivialFanoutCexHandler(self.llm, state)

    def callbacks(self) -> "_CaptureCallbacks":
        return _CaptureCallbacks(self.store)


@dataclasses.dataclass(frozen=True)
class VerifyDeps:
    """What the prover gate needs beyond the draft."""

    target: HarnessTarget
    submission: Submission
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


def _wrongly_expected(status: dict[str, bool], expected_failures: dict[CheckName, str]) -> list[str]:
    """Rules marked as expected failures that in fact verified.

    Reported because it is the more interesting direction: a rule the author believed exposed a bug
    and which passes means either the bug is not there or the rule does not test for it, and both
    want the author's attention before the run is called finished."""
    return sorted(name for name in expected_failures if status.get(name) is True)


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
            target.stage(draft, self.state["summaries"], self.state["munges"])
            run = await target.session.check(package=target.package, features=("certora",))
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
                    self.tool_call_id, f"Does not compile:\n{diagnostics}"
                )


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
                deps.target.stage(draft, self.state["summaries"], self.state["munges"])
                analysis = deps.analysis
                capture = analysis.callbacks() if analysis else None
                outcome = await submit(
                    deps.target.session,
                    # Name exactly the rules this draft declares. Not a refinement: a conf with no
                    # `rule` entry makes the cloud job end in FAILED, with no report and nothing on
                    # disk to read, so *every* submission this backend made failed until this line
                    # named them. Not `AllRules` either — the build is whole-crate, so "everything
                    # the artifact declares" is every unit's rules, and this unit would be graded on
                    # its siblings' drafts.
                    dataclasses.replace(deps.submission, rules=SelectRules(tuple(declared))),
                    prover_opts=deps.prover_opts,
                    callbacks=capture if capture is not None else ProverCallbacks(),
                    cex=(
                        analysis.handler(self.state) if analysis else UnanalyzedCexHandler()
                    ),
                    tool_call_id=self.tool_call_id,
                )
            return self._report(
                outcome, deps, capture.incomplete if capture is not None else {}
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

    def _inert_summaries(self, build: SbfRun, submission: Submission) -> str | None:
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
        version = tools_version(submission.base_conf)
        if version is None:
            return None
        try:
            symbols = defined_functions(build.verdict.manifest.artifact, tools_version=version)
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
        self, outcome: CvlrOutcome, deps: VerifyDeps, incomplete: Mapping[str, str]
    ) -> Command | str:
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
                    f"The chain build failed, so nothing was submitted:\n{said}",
                )
            case SubmissionFailed(build=build, reason=reason):
                said = [f"The prover run did not produce results: {reason}"]
                if (inert := self._inert_summaries(build, deps.submission)) is not None:
                    said.append(inert)
                return tool_return(self.tool_call_id, "\n\n".join(said))
            case Checked(build=build, report=report):
                status = report.rule_status
                unaccounted = _unaccounted(status, expected, incomplete)
                surprising = _wrongly_expected(status, expected)
                lines = [report.result_str]
                if (inert := self._inert_summaries(build, deps.submission)) is not None:
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
                if unaccounted:
                    lines.append(
                        f"Not accounted for: {', '.join(unaccounted)}. Fix the rule, or mark it "
                        "with expect_rule_failure and say why the failure is real."
                    )
                    return tool_state_update(
                        self.tool_call_id, "\n\n".join(lines), prover_link=report.link
                    )
                return tool_state_update(
                    self.tool_call_id,
                    "\n\n".join([*lines, "Every rule is accounted for. This draft is stamped."]),
                    prover_link=report.link,
                    validations=stamper(self.state, tuning_history(self.state)),
                )


def gate_tools(target: HarnessTarget, deps: VerifyDeps) -> list[BaseTool]:
    """The gate tools and the one tool that changes what they check, named as the prompt refers to
    them."""
    return [
        CargoCheck.bind(target).as_tool("cargo_check"),
        VerifyRules.bind(deps).as_tool("verify_rules"),
        SummarizeForProver.bind(target.tuning).as_tool("summarize_for_prover"),
        MungeFunction.bind(target).as_tool("munge_function"),
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
    function and assumes anything could happen inside it, so summarizing the code your property is
    actually about produces a rule that passes having checked nothing — with no trace in the
    harness. Summarize only what your properties do not depend on. Never summarize a handler, or a
    function that computes a value you assert over.

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


class MungeEarlyPanic(BaseModel):
    """Rewrite every `?` in the function to `.unwrap()`."""

    kind: Literal["early_panic"] = "early_panic"


class MungeMockFn(BaseModel):
    """Replace the function with a stand-in you have written."""

    kind: Literal["mock_fn"] = "mock_fn"
    stand_in: str = Field(
        description="Path to the replacement, spelled as the munged file can reach it — a function "
        "the program already defines, or a `pub fn` in your own harness module named as "
        "`crate::certora::specs::<this module>::<fn>`. It must already exist and must have the same "
        "signature as the function it replaces."
    )


#: The munge kinds, as the author selects between them. Discriminated because they carry different
#: fields, which is the same reason ``RuleSubject`` is
#: (:mod:`composer.spec.cvlr.state`) — `mock_fn` needs a target and `early_panic` takes nothing.
type MungeChoice = Annotated[MungeEarlyPanic | MungeMockFn, Field(discriminator="kind")]


def _kind_of(choice: MungeChoice) -> MungeKind:
    match choice:
        case MungeEarlyPanic():
            return EarlyPanic()
        case MungeMockFn(stand_in=stand_in):
            return MockFn(stand_in=stand_in)


@tool_display(lambda p: f"Munging `{p['function']}`", "Munge")
class MungeFunction(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Put a verification-only attribute on one of the *program's own* functions.

    A **munge** is a modification of the code under verification, applied under the ``certora``
    feature so the deployed build is untouched. Reach for one when the block is in the program
    rather than in your rule or the conf — and only for the two kinds below, which are what
    practitioners actually use.

    ``early_panic`` rewrites every ``?`` in the function to ``.unwrap()``. That is the same choice as
    ``.unwrap()``-versus-``.is_ok()`` in a rule, reached one level down: a ``?`` *inside* the program,
    below the handler you call, whose error construction the pointer analysis refuses ([3308]). It
    does **not** make an acceptance property statable — it removes the failure path rather than
    exposing one.

    ``mock_fn`` replaces the function with a stand-in. Unlike ``summarize_for_prover``, which havocs
    the return, a mock computes one — so a property downstream of a mocked function still means
    something. You must have written the stand-in somewhere the munged file can reach.

    **This changes the program, and the report says so.** Every munge is carried into the
    deliverable with its ``why``, because a property proved against munged source is a property of
    the munged program. Applying one invalidates the prover stamp: re-run ``verify_rules``.

    If what you need is neither of these, do not improvise a third. Skip the property and say which
    kind of change it would have needed — that is a decision for a person.
    """

    path: str = Field(
        description="The file to munge, relative to the workspace root — e.g. "
        "`programs/vault/src/state/reserve.rs`."
    )
    function: str = Field(
        description="The name of the function, as its `fn` line spells it. Not a path: the file "
        "plus the name is what identifies it, and two functions of one name in a file is refused "
        "rather than guessed at."
    )
    munge: MungeChoice = Field(description="Which of the two attributes to apply")
    why: str = Field(
        description="Why the prover cannot analyze this function as written — quote the error — and "
        "why this attribute is sound for the properties in this batch. It goes into the report; it "
        "is the only account a reader gets of what the program was changed to."
    )

    @override
    async def run(self) -> Command | str:
        if not self.why.strip():
            return (
                "A non-empty `why` is required. A munge changes the program under verification, so "
                "an unexplained one leaves the report claiming a property of code nobody can "
                "account for."
            )
        with self.tool_deps() as target:
            match target.source_path(self.path):
                case NotInWorkdir():
                    return (
                        f"{self.path} resolves outside this project. Munge the program's own "
                        f"source, with a path relative to the workspace root."
                    )
                case NotProjectSource(directory=directory):
                    return (
                        f"{self.path} is under `{directory}`, which is not this project's source. "
                        f"A munge changes the program under verification; `{directory}` holds "
                        f"build output or the dependency sources cargo resolved, and modifying a "
                        f"dependency changes its behaviour for every crate in the graph — "
                        f"including the ones your property is about. If the code in the way is a "
                        f"dependency's, that is a `summarize_for_prover` or a `record_skip`."
                    )
                case Path() as resolved:
                    pass
            if not resolved.is_file():
                return f"{self.path} is not a file in this project."
            record = FunctionMunge(
                path=self.path,
                function=self.function,
                kind=_kind_of(self.munge),
                why=self.why,
            )
            if any(m.edit_id == record.edit_id for m in self.state["munges"]):
                return f"{self.function} in {self.path} already carries that attribute."
            # A dry run against what is on disk, so a name that matches nothing is a tool error the
            # author can act on rather than a build failure two minutes later.
            match apply_munge(resolved.read_text(), record, DEFAULT_FEATURE):
                case FunctionNotFound(nearby=nearby):
                    suggestion = (
                        f" This file does define: {', '.join(nearby)}." if nearby else ""
                    )
                    return (
                        f"{self.path} defines no function named {self.function}.{suggestion}"
                    )
                case FunctionAmbiguous(lines=lines):
                    return (
                        f"{self.path} defines {self.function} {len(lines)} times (lines "
                        f"{', '.join(str(n) for n in lines)}). Munging the wrong one compiles and "
                        f"changes nothing you can see, so this is refused: move the function, or "
                        f"munge a caller that is unambiguous."
                    )
                case AlreadyMunged(line=line):
                    return (
                        f"{self.path}:{line} already carries that attribute, so nothing changed."
                    )
                case Munged(line=line):
                    pass
        # Only this record: the reducer merges, and `stage` is what writes it, so the tool never
        # races a sibling call over the same file.
        return tool_state_update(
            self.tool_call_id,
            f"Recorded a {record.kind.attribute()} munge on {self.path}:{line} ({self.function}). "
            f"It takes effect on the next build, and it invalidated the prover stamp — run "
            f"cargo_check and then verify_rules.",
            munges=[record],
        )


@tool_display(lambda p: f"Expecting rule `{p['rule_name']}` to fail", None)
class ExpectRuleFailure(WithAsyncImplementation[Command], WithInjectedId):
    """Declare that a rule is *meant* to fail because the program violates the property.

    This is how a real finding is recorded rather than argued away. The rule stays in the harness,
    the prover keeps reporting the violation, and ``verify_rules`` stops treating it as unfinished
    work. Use it only when you have read the counterexample and believe the defect is real.
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
        return tool_state_update(
            self.tool_call_id,
            f"Recorded: {self.rule_name} is expected to fail.",
            expected_failures={CheckName(self.rule_name): self.reason},
        )


@tool_display(lambda p: f"Expecting rule `{p['rule_name']}` to pass", None)
class ExpectRulePassage(WithAsyncImplementation[Command], WithInjectedId):
    """Withdraw an ``expect_rule_failure`` marking, putting the rule back under the gate."""

    rule_name: str = Field(description="The name of the rule that should verify after all")

    @override
    async def run(self) -> Command:
        return tool_state_update(
            self.tool_call_id,
            f"Withdrawn: {self.rule_name} must verify.",
            expected_failures={CheckName(self.rule_name): ""},
        )
