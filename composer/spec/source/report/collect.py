"""Collect the report's inputs from in-memory pipeline results + per-unit verdicts.

For each component (and the structural invariants) the report phase hands us the inferred
properties, the generation result (a `ReportableResult`: its skip list + property->unit mapping;
a `Curtailed` wrapper when the budget cut the generation short; ``None`` if the component gave up
or crashed), and a per-component run link. We split the properties into the ones a rule formalizes
(`FormalizedProperty`) and the formalization gaps (`SkippedClaim` / `GaveUpComponent`), route
budget-curtailed components into `CurtailedComponent` appendix records (their encodings and
verdicts are unreliable, so they are neither verdict-fetched nor grouped), and fetch per-unit
`Outcome`s via a backend-supplied `VerdictFetcher`. No on-disk dumps are read — the data is
already in memory.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from composer.authoring.state import SkippedProperty
from composer.spec.types import Curtailed, PropertyFormulation
from composer.spec.source.report.schema import (
    ComponentName, CurtailedComponent, CurtailedSkip, DraftedProperty, FormalizedProperty,
    GaveUpComponent, Outcome, PropertyTitle, RuleName, RuleRef, RuleVerdict, SkippedClaim,
)

_log = logging.getLogger(__name__)


class ReportableResult(Protocol):
    """The backend-agnostic view the report needs of a successful generation result. Both
    `GeneratedCVL` and `GeneratedFoundryTest` satisfy it: ``skipped`` are the properties the author
    declined, and ``property_checks()`` is the property->checks adapter (CVL rules /
    foundry tests — the underlying field names differ, hence the method rather than structural
    matching)."""
    skipped: list[SkippedProperty]

    def property_checks(self) -> list[tuple[PropertyTitle, list[RuleName]]]: ...

    @property
    def output_link(self) -> str | None:
        """The verification-run link for this result (prover job URL / local dir), or ``None`` for
        backends with no run service (foundry). Drives the report's ``run_link``."""
        ...


class Formalized[R: ReportableResult](Protocol):
    """The report's view of a generation result persisted to disk: the result, the project-relative
    path it was written to, the basename of the file its units live in (``autospec_<slug>.spec`` /
    ``invariants.spec`` / a ``.t.sol``) — the unit-identity fallback when a verdict carries no
    source location — and the verification-run link (``None`` for backends with no run service)."""
    @property
    def result(self) -> R: ...
    @property
    def deliverable(self) -> Path: ...
    @property
    def unit_file(self) -> str: ...
    @property
    def run_link(self) -> str | None: ...


@dataclass(frozen=True)
class ReportComponentInput[R: ReportableResult]:
    """One unit to collect: a component or the structural invariants. ``formalized`` carries the
    generation result and its unit file / run link; a `Curtailed` wrapper when the budget cut the
    generation short (its ``partial`` is the quarantined encoding, or ``None`` if nothing was
    published); or ``None`` when the component gave up or crashed — in which case no units were
    formalized, no file was written, and there is no run."""
    name: ComponentName
    props: list[PropertyFormulation]
    formalized: "Formalized[R] | Curtailed[Formalized[R]] | None"


@dataclass(frozen=True)
class Verdict:
    """One unit's rolled-up outcome within a single run, as produced by a `VerdictFetcher`."""
    outcome: Outcome
    line: int | None = None
    duration_seconds: float | None = None
    unit_file: str | None = None
    #: Human-readable explanation of a non-GOOD outcome (a counterexample / assertion message for
    #: a BAD, error text for an ERROR). Provenance/diagnostics only; ``None`` when the backend
    #: gives no detail (the prover/foundry fetchers don't).
    message: str | None = None

    def merge(self, other: "Verdict | None") -> "Verdict":
        """Combine two results for one unit within a run: higher-priority outcome wins,
        line/duration/unit_file/message kept from whichever side has them."""
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
            hi.message or lo.message,
        )


# Rollup priority when a unit has several results within a run: the most terminal outcome wins.
_OUTCOME_PRIORITY: dict[Outcome, int] = {
    Outcome.BAD: 5, Outcome.ERROR: 4, Outcome.TIMEOUT: 3, Outcome.UNKNOWN: 2, Outcome.GOOD: 1,
}

class VerdictFetcher[R: ReportableResult](Protocol):
    """Backend hook: given one delivered result, return its units' verdicts keyed by unit name. The
    prover impl calls ProverOutputUtility off-thread; the foundry impl reads the result's
    ran/expected tests. Only invoked for genuinely formalized inputs — gave-up and curtailed
    components have no verdicts to fetch."""
    async def __call__(self, formalized: Formalized[R], /) -> dict[RuleName, Verdict]:
        ...


def _curtailed_component[R: ReportableResult](
    name: ComponentName,
    props: list[PropertyFormulation],
    c: Curtailed["Formalized[R]"],
) -> CurtailedComponent:
    """Partition a curtailed component's inferred properties by what its partial result (if any)
    says happened to them: claimed-encoded (``drafted``, unverified), explicitly ``skipped``, or
    ``unattempted``. With no published partial, everything is unattempted."""
    if c.partial is None:
        return CurtailedComponent(
            component=name, detail=c.detail, unattempted=list(props),
        )
    res = c.partial.result
    skip_reasons = {s.property_title: s.reason for s in res.skipped}
    mapping = dict(res.property_checks())
    drafted: list[DraftedProperty] = []
    skipped: list[CurtailedSkip] = []
    unattempted: list[PropertyFormulation] = []
    for p in props:
        if p.title in skip_reasons:
            skipped.append(CurtailedSkip(reason=skip_reasons[p.title], **p.model_dump()))
        elif p.title in mapping:
            drafted.append(DraftedProperty(
                units=[u for u in mapping[p.title] if u.strip()], **p.model_dump(),
            ))
        else:
            unattempted.append(p)
    return CurtailedComponent(
        component=name,
        artifact=str(c.partial.deliverable),
        run_link=c.partial.run_link,
        detail=c.detail,
        drafted=drafted,
        skipped=skipped,
        unattempted=unattempted,
    )

@dataclass(frozen=True)
class RuleEvidence:
    """One failing instance of a violated rule: what a run observed about it, split by where each
    part came from.

    A backend fills the parts it has and leaves the rest ``None``. Absence always means "this run
    recorded nothing of that kind" — never that it recorded something empty — so the fields mean the
    same thing whichever backend produced them, and a prompt can say what is missing rather than
    guessing."""

    #: Which instance this is, where a rule can fail more than once: a parametric binding
    #: (``rule r(method f)``) for the prover, the component for a per-component backend. ``""``
    #: when the rule fails only once.
    label: str = ""
    #: The backend's own account of *why* the check failed, where it produces one — a root-cause
    #: explanation rather than the raw failure.
    analysis: str | None = None
    #: What reproduces the failure: a counterexample trace, a crashing input, an assertion message.
    counterexample: str | None = None
    #: The run's own outcome for this check, before any authored declaration is folded into the
    #: outcome the report shows. ``None`` for a backend that captures evidence only for failures,
    #: where it would add nothing; where a check can be reported BAD on the author's say-so, this is
    #: the only thing that says whether the run actually reproduced it.
    ran: Outcome | None = None
    #: The run's evidence about *itself*: what it spent against its budget, how far it reached, and
    #: whether this check was exercised at all. What makes a result weigh something — and what a
    #: proof of concept must not be padded with.
    accounting: str | None = None
    #: Why the author declared a failure here to be expected, when they did. Present means the row
    #: is a claim the author made, not only something the run tripped over.
    declared: str | None = None
    #: Which *finding* this belongs to, when one piece of evidence condemns several checks at once.
    #: Opaque — only ever compared, never read into. ``None`` is evidence standing on its own.
    finding: str | None = None


#: Every captured failing instance of a violated rule, or ``[]`` when the backend has none for it.
#: Keyed by `RuleRef` — ``(file, name)``, how the report identifies a row — because a name alone
#: does not: one deliverable can hold several components' checks, and two authors given the same
#: property write the same name.
type EvidenceFetcher = Callable[[RuleRef], Awaitable[list[RuleEvidence]]]


async def collect[R: ReportableResult](
    inputs: list[ReportComponentInput[R]],
    *,
    fetch_verdicts: VerdictFetcher[R],
) -> tuple[
    list[FormalizedProperty], list[RuleVerdict], list[SkippedClaim], list[GaveUpComponent],
    list[CurtailedComponent], int,
]:
    """Assemble the report inputs.

    Returns ``(formalized_properties, rules, skipped, gave_up_components, curtailed_components,
    dropped_orphan_count)``. Rules are identified by ``(unit_file, name)``: a single definition
    seen through several runs (e.g. a structural invariant imported into a component spec)
    collapses to one entry. Orphan units — reported by the backend but referenced by no property —
    are dropped and counted. Verdicts are fetched concurrently via the backend `fetch_verdicts`
    hook, for delivered inputs only: a curtailed component's verification state is unreliable by
    construction, so nothing is fetched for it.
    """
    async def _verdicts(inp: ReportComponentInput[R]) -> dict[RuleName, Verdict]:
        if inp.formalized is None or isinstance(inp.formalized, Curtailed):
            return {}
        return await fetch_verdicts(inp.formalized)

    verdict_maps = await asyncio.gather(*[_verdicts(inp) for inp in inputs])

    properties: list[FormalizedProperty] = []
    skipped: list[SkippedClaim] = []
    gave_up: list[GaveUpComponent] = []
    curtailed: list[CurtailedComponent] = []
    rules_by_key: dict[RuleRef, RuleVerdict] = {}
    referenced: set[RuleRef] = set()

    for inp, verdicts in zip(inputs, verdict_maps):
        if inp.formalized is None:
            # Gave up or crashed: the whole component is a formalization gap.
            gave_up.append(GaveUpComponent(component=inp.name, properties=inp.props))
            continue
        if isinstance(inp.formalized, Curtailed):
            curtailed.append(_curtailed_component(inp.name, inp.props, inp.formalized))
            continue
        res = inp.formalized.result
        unit_file = inp.formalized.unit_file
        run_link = inp.formalized.run_link
        skip_reasons = {s.property_title: s.reason for s in res.skipped}
        mapping = dict(res.property_checks())

        def _ref(unit_name: RuleName) -> RuleRef:
            v = verdicts.get(unit_name)
            return ((v.unit_file if v and v.unit_file else unit_file), unit_name)

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
            key = (v.unit_file or unit_file, unit_name)
            if key not in rules_by_key:
                rules_by_key[key] = RuleVerdict(
                    name=unit_name, spec_file=key[0], outcome=v.outcome, line=v.line,
                    duration_seconds=v.duration_seconds, prover_link=run_link, message=v.message,
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
    return properties, rules, skipped, gave_up, curtailed, dropped_orphans
