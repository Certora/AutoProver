"""Human-facing rollup of Crucible fuzzing verdicts for the console / TUI.

The canonical results artifact is ``report.json`` (built by the shared report phase
and rendered to HTML on demand via ``autoprove-report-render``). But — as with the CVL
and Foundry backends — the console/TUI otherwise surface only a counts block
(Instructions / Properties / Failures), so a completed run reads as "success" with no
visible verdicts. This module turns the per-invariant verdicts baked into the pipeline
result (``RustFormalResult.verdicts``, published by the Rust decider) into a compact
tally + per-invariant listing.

It follows the existing counts-block conventions: the same aligned ``Label:`` shape as
Foundry's ``Tests written:`` line, a per-item listing like the ``Failures`` block, and
the report's own crucible outcome labels (``render.outcome_label``) so the wording
matches the HTML report ("No counterexample" / "Counterexample").
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.rustapp.result import RustFormalResult
from composer.spec.source.report.render import outcome_label
from composer.spec.source.report.schema import Outcome

# Tally display order — mirrors render.py's ``_OUTCOME_ORDER`` so the console and the
# HTML report list outcomes in the same sequence.
_ORDER = [Outcome.GOOD, Outcome.BAD, Outcome.TIMEOUT, Outcome.ERROR, Outcome.UNKNOWN]

# Per-verdict glyph — mirrors the TUI's lifecycle indicators (✓/✗) so a GOOD/BAD scans
# at a glance in the plain-text block.
_GLYPH: dict[Outcome, str] = {
    Outcome.GOOD: "✓",
    Outcome.BAD: "✗",
    Outcome.TIMEOUT: "⧖",
    Outcome.ERROR: "!",
    Outcome.UNKNOWN: "?",
}


@dataclass(frozen=True)
class InvariantVerdict:
    """One invariant's fuzzing outcome: its display name and the neutral ``Outcome``."""

    name: str
    outcome: Outcome


@dataclass(frozen=True)
class VerdictSummary:
    """The delivered invariants' verdicts, in pipeline order."""

    verdicts: list[InvariantVerdict]

    @property
    def counts(self) -> dict[Outcome, int]:
        """Occurrence count per outcome, in display order, omitting absent outcomes."""
        c = Counter(v.outcome for v in self.verdicts)
        return {o: c[o] for o in _ORDER if c.get(o)}

    @property
    def tally(self) -> str:
        """A one-line ``"10 No counterexample, 1 Counterexample"`` summary (crucible labels)."""
        return ", ".join(
            f"{n} {outcome_label('crucible', o)}" for o, n in self.counts.items()
        )


def _parse_outcome(raw: str) -> Outcome:
    try:
        return Outcome(raw)
    except ValueError:
        return Outcome.UNKNOWN


def summarize_verdicts(result: CorePipelineResult[RustFormalResult]) -> VerdictSummary:
    """Extract the per-invariant verdicts baked into a completed run's ``outcomes``.

    Only *delivered* invariants carry a verdict; give-ups / exceptions are already
    surfaced in ``result.failures`` and are skipped here. Each per-invariant unit bakes
    a single verdict, so we read the one entry (falling back to UNKNOWN if a delivered
    result somehow carries none)."""
    verdicts: list[InvariantVerdict] = []
    for o in result.outcomes:
        if not isinstance(o.result, Delivered):
            continue
        baked = o.result.result.verdicts
        outcome = (
            _parse_outcome(next(iter(baked.values()))["outcome"])
            if baked
            else Outcome.UNKNOWN
        )
        verdicts.append(InvariantVerdict(o.feat.display_name, outcome))
    return VerdictSummary(verdicts)


def format_verdict_lines(summary: VerdictSummary, *, indent: str = "  ") -> list[str]:
    """The ``Verdicts:`` tally line plus a per-invariant listing, in the console
    counts-block style. Empty when no invariant was delivered (nothing to show — the
    counts/failures block already conveys that)."""
    if not summary.verdicts:
        return []
    lines = [f"{indent}Verdicts:     {summary.tally}"]
    for v in summary.verdicts:
        lines.append(
            f"{indent}  {_GLYPH[v.outcome]} {v.name} — {outcome_label('crucible', v.outcome)}"
        )
    return lines
