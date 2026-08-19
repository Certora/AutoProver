"""Findings synthesis for a Rust wheel's results: what its runs observed, and how to ask for a
write-up of it.

The shared loop is `composer.spec.source.report.findings`; what is here is the part that reads a
*wheel's* results. Every field it works from is wire — `Verdict.detail`, `Verdict.accounting`,
`Verdict.finding`, the result's declared failures — so nothing in this module knows what any
particular backend checks or what its evidence means.

Those are the wheel's to say, and it says them in `FindingsPolicy`: the domain half of the system
prompt (what its evidence *is*), and how severity is reached. A wheel that declares no policy
produces no findings — see `AppDescriptor.findings`.
"""
from collections.abc import Hashable
from dataclasses import dataclass
from typing import TypedDict

from composer.pipeline.ptypes import ComponentOutcome, Delivered
from composer.rustapp.descriptor import AssessedSeverity, FindingsPolicy, FixedSeverity
from composer.rustapp.result import RustFormalResult
from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.findings import (
    AssessedFindingDraft, Assessment, FindingDraft, FindingRequest, FindingsSynthesis, assessed,
)
from composer.spec.source.report.schema import (
    FormalizedProperty, Outcome, PropertyGroup, RuleRef, RuleVerdict, SeverityTier,
)
from composer.spec.system_model import FeatureUnit
from composer.spec.types import CheckName
from composer.templates.loader import load_jinja_template

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]

#: What a wheel's synthesis looks like once its declared severity policy has picked the draft the
#: model answers in. A union rather than one type parameterized by `FindingDraft`: `assessed` reads
#: axes only an `AssessedFindingDraft` has, so the two branches are not interchangeable.
type RustSynthesis = (
    FindingsSynthesis[CheckObservation, FindingDraft]
    | FindingsSynthesis[CheckObservation, AssessedFindingDraft]
)


@dataclass(frozen=True)
class CheckObservation:
    """What one run observed about one check, kept split by where each part came from.

    The split is the point. A declared check reports ``BAD`` whether or not this run reproduced it,
    so the reported outcome cannot say which case a reader is looking at — only ``ran`` and
    ``declared`` together can, and a write-up that confuses "the run found this" with "the author
    says this is broken" is worse than none."""

    component: str
    check: CheckName
    #: The wheel's own outcome, before the author's declaration is folded in: ``BAD`` here means
    #: this run produced a counterexample.
    ran: Outcome
    #: The run's evidence about the *program*: what it found and whatever reproduces it, or the
    #: error text behind a run that reached no verdict. Absent for a check nothing was found against.
    counterexample: str | None
    #: The run's evidence about *itself*: what it spent against its budget, how far it reached, and
    #: whether this check was exercised at all. What makes a green row worth anything, and what a
    #: proof of concept must not be padded with.
    accounting: str | None
    #: Why the author declared a failure here to be expected, when they did. Present means the row
    #: is a claim the author made, not only something the run tripped over.
    declared: str | None
    #: Which finding this row belongs to, when the wheel concluded one thing about several checks at
    #: once (`Verdict.finding`). Opaque — only ever compared.
    finding: str | None


class RustFindingsSystemParams(TypedDict):
    """The full, typed context of ``autoprove_report_findings_rust_system.j2``."""
    #: The wheel's own prose: what its evidence is and what its markers mean.
    domain: str
    #: The tier every finding gets under a fixed-severity policy, or ``None`` when the model is
    #: asked to assess. Drives which of the two mutually exclusive rating instructions is given.
    fixed_severity: SeverityTier | None


class RustFindingsPromptParams(TypedDict):
    """The full, typed context of ``autoprove_report_findings_rust_prompt.j2``."""
    contract_name: str
    check_name: str
    properties: list[FormalizedProperty]
    groups: list[PropertyGroup]
    observations: list[CheckObservation]
    #: Other checks this same evidence was reported against — empty unless the wheel could not place
    #: it, in which case naming them is most of what the finding has to say.
    also_covers: list[str]


_RUST_SYSTEM = TypedTemplate[RustFindingsSystemParams]("autoprove_report_findings_rust_system.j2")
_RUST_PROMPT = TypedTemplate[RustFindingsPromptParams]("autoprove_report_findings_rust_prompt.j2")


def observations(outcomes: RustOutcomes) -> dict[RuleRef, CheckObservation]:
    """Every delivered check, keyed as the report keys its rows: ``(file, name)``.

    Rebuilt rather than looked up: ``collect`` uses the verdict's file, falling back to the
    component artifact, and a callout-mode wheel gives every component that same fallback — so the
    check name alone does not identify a row.

    First component naming a key wins, as in ``collect``: where two of them do collapse onto one
    row, the evidence here has to be the same run's as the message there."""
    observed: dict[RuleRef, CheckObservation] = {}
    for o in outcomes:
        if not isinstance(o.result, Delivered):
            continue
        res = o.result.result
        for check, verdict in res.verdicts.items():
            ref = (verdict.unit_file or o.result.unit_file, check)
            observed.setdefault(ref, CheckObservation(
                component=o.feat.display_name,
                check=check,
                ran=verdict.outcome,
                counterexample=verdict.detail,
                accounting=verdict.accounting,
                declared=res.expected_failures.get(check),
                finding=verdict.finding,
            ))
    return observed


def _prompt(req: FindingRequest[CheckObservation]) -> str:
    return _RUST_PROMPT.bind({
        "contract_name": req.contract_name,
        "check_name": req.rule.name,
        "properties": req.properties,
        "groups": req.groups,
        "observations": req.evidence,
        "also_covers": list(req.also_covers),
    }).render_to(load_jinja_template)


def _fixed(tier: SeverityTier):
    """Every finding gets ``tier``, with no impact or likelihood recorded — for a wheel that
    declared nothing in its pipeline assesses exploitability.

    The axes stay blank rather than defaulted: they are provenance, and a rating nothing produced is
    worse than none on a finding a reader trusts. The reasoning is the author's declaration where
    there is one, so a reader can tell a finding the author documented from one the run tripped over
    without reading the evidence."""
    def assess(_draft: FindingDraft, evidence: list[CheckObservation]) -> Assessment:
        return Assessment(
            severity=tier,
            reasoning=next((e.declared for e in evidence if e.declared), None),
        )
    return assess


def _proof(evidence: list[CheckObservation]) -> str | None:
    """The counterexample alone, and only where this run actually produced a violation.

    Not the accounting: what a run spent is a claim about the run, and padding a proof of concept
    with it leaves a reader unable to see where the evidence ends. A declared check the run did not
    reproduce has no proof of concept at all — its ground is the author's reading, which rides
    ``provenance.risk_reasoning`` instead."""
    crashes = [e.counterexample for e in evidence if e.ran is Outcome.BAD and e.counterexample]
    return "\n\n".join(crashes) if crashes else None


def _one_finding(rule: RuleVerdict, evidence: list[CheckObservation]) -> Hashable:
    """The wheel's own finding key where it set one, else the row's identity.

    A wheel whose run can conclude something it cannot attribute to a single check fans that
    conclusion out over every check it covered and stamps them as one finding. Grouping on the key
    is what keeps that one write-up rather than one per row, each guessing a different check —
    which for a component's whole property set is dozens of them.

    Nothing here infers the relation from the evidence: rows fanned out from one conclusion look
    exactly like several checks that failed the same way, and those are two different facts about
    the program. Unstamped means the row stands on its own."""
    stamped = next((e.finding for e in evidence if e.finding is not None), None)
    return stamped if stamped is not None else rule.ref


def rust_findings(outcomes: RustOutcomes, policy: FindingsPolicy | None) -> RustSynthesis | None:
    """This run's synthesis, around the observations its own results carry — or ``None`` for a wheel
    that declared no findings policy, which produces no findings at all."""
    if policy is None:
        return None
    observed = observations(outcomes)

    async def fetch(ref: RuleRef) -> list[CheckObservation]:
        found = observed.get(ref)
        return [found] if found is not None else []

    severity = policy.severity
    system = _RUST_SYSTEM.bind({
        "domain": policy.system,
        "fixed_severity": severity.tier if isinstance(severity, FixedSeverity) else None,
    }).render_to(load_jinja_template)

    if isinstance(severity, AssessedSeverity):
        return FindingsSynthesis(
            draft=AssessedFindingDraft, fetch_evidence=fetch, system=system, prompt=_prompt,
            assess=assessed, proof=_proof, collapse=_one_finding,
        )
    return FindingsSynthesis(
        draft=FindingDraft, fetch_evidence=fetch, system=system, prompt=_prompt,
        assess=_fixed(severity.tier), proof=_proof, collapse=_one_finding,
    )
