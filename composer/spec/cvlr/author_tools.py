"""The author's tools that change what the prover gate checks, and how a marking re-decides the stamp.

* ``summarize_for_prover`` replaces a function with an unconstrained stand-in.
* ``adjust_prover_config`` changes the unit's prover settings.
* ``expect_rule_failure`` and ``expect_rule_passage`` mark a rule as a finding
  or put it back under the gate.

Each one changes the unit's state, and each invalidates or re-decides the
prover stamp. ``verify_rules`` (:mod:`composer.spec.cvlr.verify`) is what
builds with that state and earns the stamp.
"""

import dataclasses
from typing import Annotated, Container, Literal, override

from pydantic import BaseModel, Discriminator, Field

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithAsyncImplementation,
    WithImplementation,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.state import (
    ValidationStamper,
    make_validation_stamper,
    merge_expected_failures,
)
from composer.prover.conf import dump_conf
from composer.spec.cvlr.conf import OptimisticLoop, tunable_conf
from composer.spec.cvlr.state import (
    PROVER_VALIDATION_KEY,
    CvlrGenerationState,
    LastVerdicts,
    tuning_history,
)
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.spec.types import CheckName
from composer.ui.tool_display import tool_display


def unaccounted(
    status: dict[str, bool],
    expected_failures: dict[CheckName, str],
    incomplete: Container[str] = (),
) -> list[str]:
    """Rules that failed and are not excused by an expected-failure marking.

    An incomplete check is not excused. ``expect_rule_failure`` claims a real
    defect, and a rule that stopped on the prover's assertion never reached
    its property.
    """
    return sorted(
        name
        for name, ok in status.items()
        if not ok and (name not in expected_failures or name in incomplete)
    )


def stamp_under(last: LastVerdicts, expected: dict[CheckName, str]) -> dict[str, str]:
    """The prover stamp ``last`` earns under the markings ``expected``: its own stamp when they
    account for every failure it reported, otherwise a cleared one."""
    if last.unverdicted or unaccounted(last.status, expected, last.incomplete):
        return {key: "" for key in last.stamp}
    return last.stamp


def prover_stamper() -> ValidationStamper:
    return make_validation_stamper(PROVER_VALIDATION_KEY)


@tool_display(lambda p: f"Summarizing `{p['symbol_pattern']}` for the prover", "Summary")
class SummarizeForProver(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, TuningFiles],
):
    """Tell the prover to replace a function with an unconstrained stand-in.

    Use this when a rule gets no verdict because the pointer analysis refused
    something on its path. The usual case is [3308] on an Anchor
    ``#[error_code]`` enum formatting its ``#[msg]`` string, which any
    ``require!`` or ``?`` in a called handler can reach. Summarizing that
    formatting code makes the rest of the handler analyzable.

    A summary is unsound, and the prover does not report that. The call returns
    an unconstrained value and writes only the locations ``returns`` names.
    Every other store the real function makes is dropped. Summarizing code a
    property is about can make the rule pass having checked nothing, with no
    trace in the harness. Summarize only code the properties do not depend on.
    Do not summarize a handler, a function that writes state you assert over,
    or one that computes a value you assert over.

    Adding one invalidates the prover stamp. The previous run's verdicts were
    about a different build. Re-run ``verify_rules``.
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
            # Refused, not recorded. A summary in a file the conf does not name
            # changes nothing, and the tool would report success.
            return (
                f"This project has no {', '.join(absent)}, so the prover is not reading any tuning "
                "file and a summary would have no effect. Report this rather than working around it."
            )
        directive = SummaryDirective(
            pattern=self.symbol_pattern, why=self.why, returns=self.returns
        )
        if any(d.pattern == directive.pattern for d in self.state["summaries"]):
            return f"{directive.pattern} is already summarized."
        # Record only this directive. The state reducer merges, and the build
        # path writes the tuning file. Writing the file here would race another call.
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
        description="The new bound. While `optimistic_loop` is off, the Prover unrolls an unbounded "
        "loop this many times and then asserts that the loop has finished. A rule that verifies "
        "at this bound is verified. A rule that cannot comes back VIOLATED on \"Unwinding "
        "condition in a loop\". Set it as low as the property allows. Cost grows exponentially, "
        "and above 3 or 4 it grows faster than the answer is usually worth. Raise it after "
        "bounding whatever determines the trip count, and after deciding whether the loop is in "
        "the property's way."
    )


class SetOptimisticLoop(BaseModel):
    """Assume every loop finishes within the bound, instead of asserting that it does."""

    type: Literal["optimistic_loop"]
    enabled: bool = Field(
        description="True to assume loops finish within the bound, false to assert it again. "
        "This is the setting that changes what a verdict means. With it on, the Prover does not "
        "check that the unroll bound was enough, so a violation on a later iteration is never "
        "found and the rule reports VERIFIED. Every rule in the submission is affected. Use it "
        "only for a loop whose trip count the analysis cannot fix: raising the bound does not "
        "terminate, because the reported iteration goes up with the bound, and no summary "
        "directive can name the loop. Check that first by moving the bound and reading what "
        "comes back. A loop that discharges at 3 is not this case. If the property is about the "
        "loop, use `record_skip` and name the loop. With the setting on, the rest of the handler "
        "is reachable again. Say in `why` which bounds you tried and what they reported. A reader "
        "weighs every verdict in the unit against that account."
    )


type CvlrConfigEdit = Annotated[
    SetLoopIter | SetOptimisticLoop, Discriminator("type")
]


@tool_display(lambda p: f"Adjusting the prover config ({len(p['edits'])} edit(s))", "Config")
class AdjustProverConfig(
    WithInjectedState[CvlrGenerationState], WithInjectedId, WithImplementation[Command | str]
):
    """Change a prover setting for this unit's submissions.

    The conf is in the system prompt. This changes it. The list of settings is
    closed. ``loop_iter`` decides how far the prover unrolls a loop. It does
    not change what a green verdict means.

    ``optimistic_loop`` does. With it on, the prover does not check that the
    unroll bound was enough, so a violation on a later iteration is not
    reported and the rule can come back verified. It applies to every rule in
    the unit. Use it only when the trip count cannot be fixed by bounding
    inputs, editing the loop, or raising the bound: the bound goes up and the
    reported iteration goes up with it, and no summary directive can name the
    loop. A loop that discharges at a small bound is not this case. When the
    property is about the loop itself, ``record_skip`` naming the loop is the
    better answer. Say in ``why`` which bounds were tried and what they
    reported.

    ``rule_sanity`` and the memory-model flags are not settings here.

    Try the rule's own remedies before a setting change. A timeout often means
    an unbounded operand, a step that belongs in a lemma, or a loop whose trip
    count nothing constrains.

    Edits apply together or not at all, and the result is the full conf. A
    change invalidates the prover stamp. Re-run ``verify_rules``.
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
    stamp = stamp_under(last, merge_expected_failures(state["expected_failures"], marking))
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
    """Record that a rule is meant to fail because the program violates the property.

    The rule stays in the harness and the prover keeps reporting the violation.
    ``verify_rules`` stops treating it as unfinished. Use it only after reading
    the counterexample and concluding the defect is real. When the marking
    accounts for every failure of the last run, the draft is stamped from that
    run, with no new submission.
    """

    rule_name: str = Field(description="The name of the rule expected to fail")
    reason: str = Field(
        description="Why the failure is a genuine defect in the program rather than in the rule"
    )

    @override
    async def run(self) -> Command:
        # An empty reason removes the marking. Do not record one. An unexplained
        # expected failure looks like a broken rule.
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
