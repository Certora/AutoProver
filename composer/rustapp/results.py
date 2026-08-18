"""Human-facing rollup of a Rust backend's per-check verdicts for the console / TUI.

The canonical results artifact is ``report.json`` (the shared report phase). But — as with the
CVL and Foundry backends — the console/TUI otherwise surface only a counts block, so a completed
run reads as "success" with no visible verdicts. This turns the per-check verdicts baked into the
pipeline result (:meth:`RustFormalResult.reported_verdicts`) into a compact tally + listing,
using the report's own outcome labels so a declared finding reads the same here as in the HTML.

Backend-agnostic: the outcome wording is parametrized by the descriptor's ``backend_tag``, so any
Rust app whose results carry verdicts gets the same summary.
"""

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.rustapp.result import RustFormalResult
from composer.spec.source.report.render import outcome_glyph, outcome_label
from composer.spec.source.report.schema import Outcome, ReportBackend

# ``RowName``: what a verdict row is called in the console/TUI listing. Phantom-typed like
# ``CheckName`` / ``PropertyTitle`` / ``ComponentName`` so it is a sibling of all three — the
# row is whatever :meth:`RustFormalResult.display_name` (or a component's display name) chose
# to show, and is never looked up as one of them. Defined here because the row is a display
# concept of this rollup, not an identity field of the analyzed system.
if TYPE_CHECKING:
    class RowName(str): ...
else:
    RowName = str

# Tally display order — mirrors render.py's ``_OUTCOME_ORDER`` so the console and the HTML report
# list outcomes in the same sequence.
_ORDER = [Outcome.GOOD, Outcome.BAD, Outcome.TIMEOUT, Outcome.ERROR, Outcome.UNKNOWN]


@dataclass(frozen=True)
class CheckVerdict:
    """One check's outcome: its display name and the neutral ``Outcome``."""

    #: Whatever :meth:`RustFormalResult.display_name` chose to call the row — a property's
    #: title, a check's name, or a component's display name. A :class:`RowName` so it belongs
    #: to none of those namespaces and is never looked up as one of them.
    name: RowName
    outcome: Outcome


@dataclass(frozen=True)
class VerdictSummary:
    """The delivered checks' verdicts, in pipeline order, plus the report backend tag for wording."""

    verdicts: list[CheckVerdict]
    backend_tag: ReportBackend

    @property
    def counts(self) -> dict[Outcome, int]:
        """Occurrence count per outcome, in display order, omitting absent outcomes."""
        c = Counter(v.outcome for v in self.verdicts)
        return {o: c[o] for o in _ORDER if c.get(o)}

    @property
    def tally(self) -> str:
        """A one-line ``"10 No counterexample, 1 Counterexample"`` summary (backend labels)."""
        return ", ".join(
            f"{n} {outcome_label(self.backend_tag, o)}" for o, n in self.counts.items()
        )


def summarize_verdicts(
    result: CorePipelineResult[RustFormalResult], backend_tag: ReportBackend
) -> VerdictSummary:
    """Extract the per-check verdicts baked into a completed run's ``outcomes``.

    One row per *check*, not per component: a component's gate run bakes a verdict per check it
    covered, and all of them are rows (reading only the first would report one check where five
    ran). Rows are named by :meth:`RustFormalResult.display_name`.

    Only *delivered* components carry verdicts; give-ups / exceptions are already surfaced in
    ``result.failures`` and skipped here. A delivered component that bakes none at all (a
    run-service-backed wheel, which reports through ``fetch_verdicts`` instead) still contributes
    one UNKNOWN row, so the listing accounts for every delivered component."""
    verdicts: list[CheckVerdict] = []
    for o in result.outcomes:
        if not isinstance(o.result, Delivered):
            continue
        formalized = o.result.result
        if not formalized.verdicts:
            verdicts.append(CheckVerdict(RowName(o.feat.display_name), Outcome.UNKNOWN))
            continue
        verdicts.extend(
            CheckVerdict(RowName(formalized.display_name(name)), reported.outcome)
            for name, reported in formalized.reported_verdicts().items()
        )
    return VerdictSummary(verdicts, backend_tag)


def format_verdict_lines(summary: VerdictSummary, *, indent: str = "  ") -> list[str]:
    """The ``Verdicts:`` tally line plus a per-check listing, in the console counts-block style.
    Empty when nothing was delivered (the counts/failures block already conveys that)."""
    if not summary.verdicts:
        return []
    lines = [f"{indent}Verdicts:     {summary.tally}"]
    for v in summary.verdicts:
        lines.append(
            f"{indent}  {outcome_glyph(v.outcome)} {v.name} — "
            f"{outcome_label(summary.backend_tag, v.outcome)}"
        )
    return lines
