"""Collect the report's inputs from in-memory pipeline results + per-unit verdicts.

For each component (and the structural invariants) the report phase hands us the inferred
properties, the generation result (a `ReportableResult`: its skip list + property->unit mapping;
``None`` if the component gave up or crashed), and a per-component run link. We split the
properties into the ones a rule formalizes (`FormalizedProperty`) and the formalization gaps
(`SkippedClaim` / `GaveUpComponent`), and fetch per-unit `Outcome`s via a backend-supplied
`VerdictFetcher`. No on-disk dumps are read — the data is already in memory.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from composer.spec.cvl_generation import SkippedProperty
from composer.spec.prop import PropertyFormulation
from composer.spec.source.report.schema import (
    ComponentName, FormalizedProperty, GaveUpComponent, Outcome, PropertyTitle, RuleName, RuleRef,
    RuleVerdict, SkippedClaim,
)

_log = logging.getLogger(__name__)


class ReportableResult(Protocol):
    """The backend-agnostic view the report needs of a successful generation result. Both
    `GeneratedCVL` and `GeneratedFoundryTest` satisfy it: ``skipped`` are the properties the author
    declined, and ``property_units()`` is the property->formalizing-units adapter (CVL rules /
    foundry tests — the underlying field names differ, hence the method rather than structural
    matching)."""
    skipped: list[SkippedProperty]

    def property_units(self) -> list[tuple[PropertyTitle, list[RuleName]]]: ...


@dataclass(frozen=True)
class ReportComponentInput[R: ReportableResult]:
    """One unit to collect: a component or the structural invariants. ``unit_file`` is the basename
    of the file the units live in (``autospec_<slug>.spec`` / ``invariants.spec`` / a ``.t.sol``) —
    the unit-identity fallback when a verdict carries no source location. ``result`` is the in-memory
    generation outcome, or ``None`` when the component gave up / crashed (no units formalized).
    ``run_link`` is this component's verification-run link, if any (prover URL / local dir)."""
    name: ComponentName
    unit_file: str
    props: list[PropertyFormulation]
    result: R | None
    run_link: str | None


@dataclass(frozen=True)
class Verdict:
    """One unit's rolled-up outcome within a single run, as produced by a `VerdictFetcher`."""
    outcome: Outcome
    line: int | None = None
    duration_seconds: float | None = None
    unit_file: str | None = None

    def merge(self, other: "Verdict | None") -> "Verdict":
        """Combine two results for one unit within a run: higher-priority outcome wins,
        line/duration/unit_file kept from whichever side has them."""
        if other is None:
            return self
        hi, lo = (
            (self, other)
            if _OUTCOME_PRIORITY.get(self.outcome, 0) >= _OUTCOME_PRIORITY.get(other.outcome, 0)
            else (other, self)
        )
        return Verdict(
            hi.outcome,
            hi.line if hi.line is not None else lo.line,
            hi.duration_seconds if hi.duration_seconds is not None else lo.duration_seconds,
            hi.unit_file or lo.unit_file,
        )


# Rollup priority when a unit has several results within a run: the most terminal outcome wins.
_OUTCOME_PRIORITY: dict[Outcome, int] = {
    Outcome.BAD: 5, Outcome.ERROR: 4, Outcome.TIMEOUT: 3, Outcome.UNKNOWN: 2, Outcome.GOOD: 1,
}


type VerdictFetcher[R: ReportableResult] = Callable[
    [ReportComponentInput[R]], Awaitable[dict[RuleName, Verdict]]
]
"""Backend hook: given one collected input, return its units' verdicts keyed by unit name. The
prover impl calls ProverOutputUtility off-thread; the foundry impl reads the result's ran/expected
tests. A component with no result (gave up) yields ``{}``."""


async def collect[R: ReportableResult](
    inputs: list[ReportComponentInput[R]],
    *,
    fetch_verdicts: VerdictFetcher[R],
) -> tuple[list[FormalizedProperty], list[RuleVerdict], list[SkippedClaim], list[GaveUpComponent], int]:
    """Assemble the report inputs.

    Returns ``(formalized_properties, rules, skipped, gave_up_components, dropped_orphan_count)``.
    Rules are identified by ``(unit_file, name)``: a single definition seen through several runs
    (e.g. a structural invariant imported into a component spec) collapses to one entry. Orphan
    units — reported by the backend but referenced by no property — are dropped and counted.
    Verdicts are fetched concurrently via the backend `fetch_verdicts` hook.
    """
    verdict_maps = await asyncio.gather(*[fetch_verdicts(inp) for inp in inputs])

    properties: list[FormalizedProperty] = []
    skipped: list[SkippedClaim] = []
    gave_up: list[GaveUpComponent] = []
    rules_by_key: dict[RuleRef, RuleVerdict] = {}
    referenced: set[RuleRef] = set()

    for inp, verdicts in zip(inputs, verdict_maps):
        if inp.result is None:
            # Gave up or crashed: the whole component is a formalization gap.
            gave_up.append(GaveUpComponent(component=inp.name, properties=inp.props))
            continue
        res = inp.result
        skip_reasons = {s.property_title: s.reason for s in res.skipped}
        mapping = dict(res.property_units())

        def _ref(unit_name: str) -> RuleRef:
            v = verdicts.get(unit_name)
            return ((v.unit_file if v and v.unit_file else inp.unit_file), unit_name)

        for prop in inp.props:
            if prop.title in skip_reasons:
                skipped.append(SkippedClaim(
                    component=inp.name, reason=skip_reasons[prop.title], **prop.model_dump()
                ))
            elif prop.title in mapping:
                refs = [_ref(un) for un in mapping[prop.title] if un.strip()]
                referenced.update(refs)
                properties.append(FormalizedProperty(
                    component=inp.name, rule_refs=refs, **prop.model_dump()
                ))
            else:
                # The completion validator guarantees skipped-or-mapped; a residue means the
                # property/skip/mapping disagree. Drop rather than invent a record.
                _log.warning(
                    "report: property %r in %s is neither skipped nor mapped; dropping",
                    prop.title, inp.name,
                )

        # Register every unit the backend reported (first run naming a (unit_file, name) wins).
        for unit_name, v in verdicts.items():
            key = (v.unit_file or inp.unit_file, unit_name)
            if key not in rules_by_key:
                rules_by_key[key] = RuleVerdict(
                    name=unit_name, spec_file=key[0], outcome=v.outcome, line=v.line,
                    duration_seconds=v.duration_seconds, prover_link=inp.run_link,
                )

    # A referenced unit with no verdict still needs an (UNKNOWN) entry to render.
    for ref in referenced:
        if ref not in rules_by_key:
            rules_by_key[ref] = RuleVerdict(name=ref[1], spec_file=ref[0])

    rules = sorted(
        (rv for key, rv in rules_by_key.items() if key in referenced),
        key=lambda r: r.ref,
    )
    dropped_orphans = sum(1 for key in rules_by_key if key not in referenced)
    return properties, rules, skipped, gave_up, dropped_orphans
