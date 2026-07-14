"""Human-facing rollup of a Rust backend's per-unit verdicts for the console / TUI.

The canonical results artifact is ``report.json`` (the shared report phase). But — as with the
CVL and Foundry backends — the console/TUI otherwise surface only a counts block, so a completed
run reads as "success" with no visible verdicts. This turns the per-unit verdicts baked into the
pipeline result (:attr:`RustFormalResult.verdicts`, published by ``validate``) into a compact
tally + per-unit listing, using the report's own outcome labels so the wording matches the HTML
report.

Backend-agnostic: the outcome wording is parametrized by the descriptor's ``backend_tag`` (was
hard-coded ``crucible`` when this lived in ``composer/crucible/results.py``), so any Rust app whose
results carry verdicts gets the same summary.
"""

from collections import Counter
from dataclasses import dataclass

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.rustapp.result import RustFormalResult
from composer.spec.source.report.render import outcome_label
from composer.spec.source.report.schema import Outcome, ReportBackend

# Tally display order — mirrors render.py's ``_OUTCOME_ORDER`` so the console and the HTML report
# list outcomes in the same sequence.
_ORDER = [Outcome.GOOD, Outcome.BAD, Outcome.TIMEOUT, Outcome.ERROR, Outcome.UNKNOWN]

# Per-verdict glyph — mirrors the TUI's lifecycle indicators (✓/✗) so a GOOD/BAD scans at a glance.
_GLYPH: dict[Outcome, str] = {
    Outcome.GOOD: "✓",
    Outcome.BAD: "✗",
    Outcome.TIMEOUT: "⧖",
    Outcome.ERROR: "!",
    Outcome.UNKNOWN: "?",
}


@dataclass(frozen=True)
class UnitVerdict:
    """One unit's outcome: its display name and the neutral ``Outcome``."""

    name: str
    outcome: Outcome


@dataclass(frozen=True)
class VerdictSummary:
    """The delivered units' verdicts, in pipeline order, plus the report backend tag for wording."""

    verdicts: list[UnitVerdict]
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


def _parse_outcome(raw: str) -> Outcome:
    try:
        return Outcome(raw)
    except ValueError:
        return Outcome.UNKNOWN


def summarize_verdicts(
    result: CorePipelineResult[RustFormalResult], backend_tag: ReportBackend
) -> VerdictSummary:
    """Extract the per-unit verdicts baked into a completed run's ``outcomes``.

    Only *delivered* units carry a verdict; give-ups / exceptions are already surfaced in
    ``result.failures`` and skipped here. Each per-invariant unit bakes a single verdict, so we
    read the one entry (falling back to UNKNOWN if a delivered result somehow carries none)."""
    verdicts: list[UnitVerdict] = []
    for o in result.outcomes:
        if not isinstance(o.result, Delivered):
            continue
        baked = o.result.result.verdicts
        outcome = (
            _parse_outcome(next(iter(baked.values()))["outcome"]) if baked else Outcome.UNKNOWN
        )
        verdicts.append(UnitVerdict(o.feat.display_name, outcome))
    return VerdictSummary(verdicts, backend_tag)


def format_verdict_lines(summary: VerdictSummary, *, indent: str = "  ") -> list[str]:
    """The ``Verdicts:`` tally line plus a per-unit listing, in the console counts-block style.
    Empty when no unit was delivered (the counts/failures block already conveys that)."""
    if not summary.verdicts:
        return []
    lines = [f"{indent}Verdicts:     {summary.tally}"]
    for v in summary.verdicts:
        lines.append(
            f"{indent}  {_GLYPH[v.outcome]} {v.name} — {outcome_label(summary.backend_tag, v.outcome)}"
        )
    return lines
