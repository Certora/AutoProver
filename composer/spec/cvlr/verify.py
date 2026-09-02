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
from typing import Sequence, override

from langchain_core.tools import BaseTool
from pydantic import Field

from graphcore.graph import tool_return, tool_state_update
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
from composer.prover.core import CexHandler, ProverCallbacks, ProverOptions
from composer.spec.cvlr.conf import SelectRules, tools_version
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
from composer.spec.types import CheckName
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)


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

    def stage(self, draft: str, summaries: Sequence[SummaryDirective] = ()) -> None:
        """Put the draft and the tuning directives on disk, ready to build.

        Both, from one place, because both are inputs to the build and the prover reads the tuning
        file through the conf. The summaries come from already-merged state rather than from the tool
        that recorded one, so two concurrent ``summarize_for_prover`` calls cannot each write their
        own view of the list and lose the other's.
        """
        self.module_path.parent.mkdir(parents=True, exist_ok=True)
        self.module_path.write_text(draft)
        if summaries:
            self.tuning.write(tuple(summaries))


@dataclasses.dataclass(frozen=True)
class VerifyDeps:
    """What the prover gate needs beyond the draft."""

    target: HarnessTarget
    submission: Submission
    prover_opts: ProverOptions
    stamper: ValidationStamper
    cex: CexHandler | None = None
    #: One submission at a time per unit. A second concurrent run would build into the same workdir
    #: and race the staged module; the tool refuses rather than serializing silently, because a
    #: caller that made two calls wanted two answers.
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


def _unaccounted(status: dict[str, bool], expected_failures: dict[CheckName, str]) -> list[str]:
    """Rules that failed and were not declared as expected failures."""
    return sorted(name for name, ok in status.items() if not ok and name not in expected_failures)


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
            target.stage(draft, self.state["summaries"])
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
                deps.target.stage(draft, self.state["summaries"])
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
                    callbacks=ProverCallbacks(),
                    cex=deps.cex,
                    tool_call_id=self.tool_call_id,
                )
            return self._report(outcome, deps)

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

    def _report(self, outcome: CvlrOutcome, deps: VerifyDeps) -> Command | str:
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
                unaccounted = _unaccounted(status, expected)
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
