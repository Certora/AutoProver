"""The author's tools that change what the Prover checks.

* ``summarize_for_prover`` replaces a function with an unconstrained stand-in.
* ``adjust_prover_config`` changes the unit's prover settings.

Each one changes the unit's state, and each invalidates the prover stamp. ``verify_rules`` (:mod:`composer.spec.cvlr.verify`) is what
builds with that state and earns the stamp.
"""

from typing import override

from pydantic import Field

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithImplementation,
    WithInjectedId,
    WithInjectedState,
)

from composer.prover.conf import dump_conf
from composer.spec.cvlr.conf import SettingsEdit, tunable_conf
from composer.spec.cvlr.state import CvlrGenerationState
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.ui.tool_display import tool_display


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

    To change both settings, set both in one call. Do not make parallel calls:
    each reports the conf as if it were the only one, and of two that set the
    same field only one takes effect.
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
        default="",
        description="Required when changing `loop_iter` or turning `optimistic_loop` on. Which "
        "bounds you tried, what they reported, and why this setting is the remedy. Not written "
        "into the conf.",
    )

    @override
    def run(self) -> Command | str:
        if self.loop_iter is None and self.optimistic_loop is None:
            return "No edits given."
        if (self.loop_iter is not None or self.optimistic_loop is True) and not self.why.strip():
            return (
                "A non-empty `why` is required to change `loop_iter` or turn `optimistic_loop` on."
            )
        settings = self.state["prover_settings"]
        if self.loop_iter is not None:
            if self.loop_iter < 1:
                return f"A loop bound of {self.loop_iter} is not a bound; it has to be at least 1."
            if settings.loop_iter == self.loop_iter:
                return f"`loop_iter` is already {self.loop_iter}."
        if self.optimistic_loop is not None:
            on = self.optimistic_loop
            if settings.optimistic_loop == on:
                return "`optimistic_loop` is already " + ("on" if on else "off") + " in this conf."
        edit = SettingsEdit(loop_iter=self.loop_iter, optimistic_loop=self.optimistic_loop)
        return tool_state_update(
            self.tool_call_id,
            "Prover config updated; the prover stamp is invalidated, so re-run `verify_rules`.\n\n"
            f"```json\n{dump_conf(tunable_conf(edit.apply(settings)))}```",
            prover_settings=edit,
        )
