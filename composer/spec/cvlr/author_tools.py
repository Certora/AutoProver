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
from typing import Container, override

from pydantic import Field

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

    The stand-in returns an unconstrained value and writes only the locations
    ``returns`` names. Every other store the real function makes is dropped.

    The directive applies from the next build. Adding one invalidates the
    prover stamp. Re-run ``verify_rules``.
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
        description="What the summarized function returns, as `<location>:<kind>` — e.g. "
        "`(*i32)(r1+0):num`. A location is `r0` (the return register) or memory at an offset from "
        "a register, such as `(*i64)(r1+8)` for an out-pointer return. A kind is `num`, "
        "`ptr_heap` or `ptr_external`. Give only that text; the `#[type(` and `)]` around it are "
        "added for you. Omit to leave the return unconstrained.",
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


def _config_change(args: dict) -> str:
    parts: list[str] = []
    if args.get("loop_iter") is not None:
        parts.append(f"loop_iter={args['loop_iter']}")
    flag = args.get("optimistic_loop")
    if flag is not None:
        parts.append("optimistic_loop " + ("on" if flag else "off"))
    shown = ", ".join(parts) if parts else "no change"
    return f"Adjusting the prover config ({shown})"


@tool_display(_config_change, "Config")
class AdjustProverConfig(
    WithInjectedState[CvlrGenerationState], WithInjectedId, WithImplementation[Command | str]
):
    """Change this unit's loop bound or its ``optimistic_loop`` setting.

    No other prover setting can be changed here. Both apply to every rule in
    the unit. Omit a field to leave it. A call that sets neither is refused. A
    change invalidates the prover stamp. Re-run ``verify_rules``.
    """

    loop_iter: int | None = Field(
        default=None,
        description="How many times the prover unrolls a loop, at least 1. Omit to leave it. "
        "While `optimistic_loop` is off, the prover then asserts the loop has finished, and a loop "
        "that needs more iterations comes back VIOLATED on \"Unwinding condition in a loop\". "
        "While it is on, the prover assumes the loop has finished instead.",
    )
    optimistic_loop: bool | None = Field(
        default=None,
        description="True to assume every loop finishes within `loop_iter` iterations instead of "
        "asserting it, false to assert it again. Omit to leave it.",
    )
    why: str = Field(
        description="Which bounds you tried, what they reported, and why this setting is the "
        "remedy. The conf ships with the deliverable, and when `optimistic_loop` is turned on this "
        "is the reason stored with it."
    )

    @override
    def run(self) -> Command | str:
        if not self.why.strip():
            return (
                "A non-empty `why` is required. The conf ships with the deliverable, and a setting "
                "nobody explained is one a reader cannot weigh."
            )
        if self.loop_iter is None and self.optimistic_loop is None:
            return "No edits given."
        settings = self.state["prover_settings"]
        if self.loop_iter is not None:
            if self.loop_iter < 1:
                return f"A loop bound of {self.loop_iter} is not a bound; it has to be at least 1."
            if settings.loop_iter == self.loop_iter:
                return f"`loop_iter` is already {self.loop_iter}."
        if self.optimistic_loop is not None:
            on = self.optimistic_loop
            if (settings.optimistic_loop is not None) == on:
                return "`optimistic_loop` is already " + ("on" if on else "off") + " in this conf."
        if self.loop_iter is not None:
            settings = dataclasses.replace(settings, loop_iter=self.loop_iter)
        if self.optimistic_loop is not None:
            settings = dataclasses.replace(
                settings,
                optimistic_loop=(
                    OptimisticLoop(why=self.why) if self.optimistic_loop else None
                ),
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
