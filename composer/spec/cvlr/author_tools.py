"""The author's tools that change what the Prover checks.

* ``summarize_for_prover`` replaces a function with an unconstrained stand-in.
* ``adjust_prover_config`` changes the unit's prover settings.

Each one changes the unit's state, and each invalidates the prover stamp. ``verify_rules`` (:mod:`composer.spec.cvlr.verify`) is what
builds with that state and earns the stamp.
"""

import dataclasses
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
from composer.spec.cvlr.conf import tunable_conf
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
    """Change this unit's prover settings.

    The list is closed. ``loop_iter`` decides how far the prover unrolls a loop
    and does not change what a green verdict means. ``optimistic_loop`` does,
    and it applies to every rule in the unit.

    Omit a field to leave it. A call that sets neither is refused. A change
    invalidates the prover stamp. Re-run ``verify_rules``.
    """

    loop_iter: int | None = Field(
        default=None,
        description="The new loop bound, or omit to leave it. While `optimistic_loop` is off, the "
        "Prover unrolls an unbounded loop this many times and then asserts that the loop has "
        "finished. A rule that verifies at this bound is verified. A rule that cannot comes back "
        "VIOLATED on \"Unwinding condition in a loop\". Set it as low as the property allows. Cost "
        "grows exponentially, and above 3 or 4 it grows faster than the answer is usually worth. "
        "Raise it after bounding whatever determines the trip count, and after deciding whether "
        "the loop is in the property's way.",
    )
    optimistic_loop: bool | None = Field(
        default=None,
        description="True to assume loops finish within the bound, false to assert it again, or "
        "omit to leave the setting. This is the setting that changes what a verdict means. With it "
        "on, the Prover does not check that the unroll bound was enough, so a violation on a later "
        "iteration is never found and the rule reports VERIFIED. Every rule in the submission is "
        "affected. Use it only for a loop whose trip count the analysis cannot fix: raising the "
        "bound does not terminate, because the reported iteration goes up with the bound, and no "
        "summary directive can name the loop. Check that first by moving the bound and reading "
        "what comes back. A loop that discharges at 3 is not this case. If the property is about "
        "the loop, use `record_skip` and name the loop. With the setting on, the rest of the "
        "handler is reachable again. Say in `why` which bounds you tried and what they reported.",
    )
    why: str = Field(
        default="",
        description="Required when turning `optimistic_loop` on. What you tried first, and why "
        "assuming every loop finishes within `loop_iter` is sound for this batch. Not written into "
        "the conf. A loop-bound change does not use it.",
    )

    @override
    def run(self) -> Command | str:
        if self.loop_iter is None and self.optimistic_loop is None:
            return "No edits given."
        if self.optimistic_loop is True and not self.why.strip():
            return "A non-empty `why` is required to turn `optimistic_loop` on."
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
        if self.loop_iter is not None:
            settings = dataclasses.replace(settings, loop_iter=self.loop_iter)
        if self.optimistic_loop is not None:
            settings = dataclasses.replace(settings, optimistic_loop=self.optimistic_loop)
        return tool_state_update(
            self.tool_call_id,
            "Prover config updated; the prover stamp is invalidated, so re-run `verify_rules`.\n\n"
            f"```json\n{dump_conf(tunable_conf(settings))}```",
            prover_settings=settings,
        )
