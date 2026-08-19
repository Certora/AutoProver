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
    Assessed, FindingsPromptParams, FindingsSynthesis, Fixed, RuleEvidence, SeverityFrom,
)
from composer.spec.source.report.schema import RuleRef, SeverityTier
from composer.spec.system_model import FeatureUnit
from composer.templates.loader import load_jinja_template

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]


class RustFindingsSystemParams(TypedDict):
    """The full, typed context of ``autoprove_report_findings_rust_system.j2``."""
    #: The wheel's own prose: what its evidence is and what its markers mean.
    domain: str
    #: The tier every finding gets under a fixed-severity policy, or ``None`` when the model is
    #: asked to assess. Drives which of the two mutually exclusive rating instructions is given.
    fixed_severity: SeverityTier | None


_RUST_SYSTEM = TypedTemplate[RustFindingsSystemParams]("autoprove_report_findings_rust_system.j2")
_RUST_PROMPT = TypedTemplate[FindingsPromptParams]("autoprove_report_findings_rust_prompt.j2")


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


def _severity(declared: AssessedSeverity | FixedSeverity) -> SeverityFrom:
    """The wheel's declared policy as the host's. Both sides model it as one choice rather than a
    schema and a rating rule that could disagree; this is only the wire crossing."""
    return Assessed() if isinstance(declared, AssessedSeverity) else Fixed(tier=declared.tier)


def rust_findings(outcomes: RustOutcomes, policy: FindingsPolicy | None) -> FindingsSynthesis | None:
    """This run's synthesis, around the observations its own results carry — or ``None`` for a wheel
    that declared no findings policy, which produces no findings at all."""
    if policy is None:
        return None
    observed = observations(outcomes)

    async def fetch(ref: RuleRef) -> list[RuleEvidence]:
        found = observed.get(ref)
        return [found] if found is not None else []

    severity = _severity(policy.severity)
    return FindingsSynthesis(
        fetch_evidence=fetch,
        system=_RUST_SYSTEM.bind({
            "domain": policy.system,
            "fixed_severity": severity.tier if isinstance(severity, Fixed) else None,
        }).render_to(load_jinja_template),
        prompt=_RUST_PROMPT,
        severity=severity,
    )
