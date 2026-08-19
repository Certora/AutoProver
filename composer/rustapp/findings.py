"""Findings synthesis for a Rust wheel's results: where its evidence comes from, and how to ask for
a write-up of it.

The shared loop is `composer.spec.source.report.findings`, and `RuleEvidence` is its shape. What is
here is the part that reads a *wheel's* results into it — every field it works from is wire
(`Verdict.detail`, `Verdict.accounting`, `Verdict.finding`, the result's declared failures), so
nothing in this module knows what any particular backend checks or what its evidence means.

Those are the wheel's to say, and it says them in `FindingsPolicy`: the domain half of the system
prompt (what its evidence *is*), and how severity is reached. A wheel that declares no policy
produces no findings — see `AppDescriptor.findings`.
"""
from typing import TypedDict

from composer.pipeline.ptypes import ComponentOutcome, Delivered
from composer.rustapp.descriptor import AssessedSeverity, FindingsPolicy, FixedSeverity
from composer.rustapp.result import RustFormalResult
from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.findings import (
    AssessedFindingDraft, Assessment, FindingDraft, FindingRequest, FindingsSynthesis, RuleEvidence,
    assessed,
)
from composer.spec.source.report.schema import (
    FormalizedProperty, PropertyGroup, RuleRef, SeverityTier,
)
from composer.spec.system_model import FeatureUnit
from composer.templates.loader import load_jinja_template

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]

#: What a wheel's synthesis looks like once its declared severity policy has picked the draft the
#: model answers in. A union rather than one type parameterized by `FindingDraft`: `assessed` reads
#: axes only an `AssessedFindingDraft` has, so the two branches are not interchangeable.
type RustSynthesis = FindingsSynthesis[FindingDraft] | FindingsSynthesis[AssessedFindingDraft]


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
    observations: list[RuleEvidence]
    #: Other checks this same evidence was reported against — empty unless the wheel could not place
    #: it, in which case naming them is most of what the finding has to say.
    also_covers: list[str]


_RUST_SYSTEM = TypedTemplate[RustFindingsSystemParams]("autoprove_report_findings_rust_system.j2")
_RUST_PROMPT = TypedTemplate[RustFindingsPromptParams]("autoprove_report_findings_rust_prompt.j2")


def observations(outcomes: RustOutcomes) -> dict[RuleRef, RuleEvidence]:
    """Every delivered check as evidence, keyed as the report keys its rows: ``(file, name)``.

    Rebuilt rather than looked up: ``collect`` uses the verdict's file, falling back to the
    component artifact, and a callout-mode wheel gives every component that same fallback — so the
    check name alone does not identify a row.

    First component naming a key wins, as in ``collect``: where two of them do collapse onto one
    row, the evidence here has to be the same run's as the message there.

    ``analysis`` stays unset — a wheel reports what its run found, not a reading of why the check
    broke, and a reader must be able to tell the two apart."""
    observed: dict[RuleRef, RuleEvidence] = {}
    for o in outcomes:
        if not isinstance(o.result, Delivered):
            continue
        res = o.result.result
        for check, verdict in res.verdicts.items():
            ref = (verdict.unit_file or o.result.unit_file, check)
            observed.setdefault(ref, RuleEvidence(
                label=o.feat.display_name,
                counterexample=verdict.detail,
                ran=verdict.outcome,
                accounting=verdict.accounting,
                declared=res.expected_failures.get(check),
                finding=verdict.finding,
            ))
    return observed


def _prompt(req: FindingRequest) -> str:
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
    def assess(_draft: FindingDraft, evidence: list[RuleEvidence]) -> Assessment:
        return Assessment(
            severity=tier,
            reasoning=next((e.declared for e in evidence if e.declared), None),
        )
    return assess


def rust_findings(outcomes: RustOutcomes, policy: FindingsPolicy | None) -> RustSynthesis | None:
    """This run's synthesis, around the observations its own results carry — or ``None`` for a wheel
    that declared no findings policy, which produces no findings at all."""
    if policy is None:
        return None
    observed = observations(outcomes)

    async def fetch(ref: RuleRef) -> list[RuleEvidence]:
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
            assess=assessed,
        )
    return FindingsSynthesis(
        draft=FindingDraft, fetch_evidence=fetch, system=system, prompt=_prompt,
        assess=_fixed(severity.tier),
    )
