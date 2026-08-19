"""The Certora Prover's half of findings synthesis: where its evidence comes from, and what the
model is told that evidence is.

The shared loop is `composer.spec.source.report.findings`. What is here is only what makes a finding
a *Prover* finding — a prompt that says the Prover refuted the rule with a concrete counterexample,
and the root-cause analysis captured during the run to ground it in.
"""
from typing import TypedDict

from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.findings import (
    AssessedFindingDraft, EvidenceFetcher, FindingRequest, FindingsSynthesis, RuleEvidence, assessed,
)
from composer.spec.source.report.schema import FormalizedProperty, PropertyGroup
from composer.templates.loader import load_jinja_template


class FindingsSystemParams(TypedDict):
    """``autoprove_report_findings_system.j2`` takes no parameters — the instructions are static.
    Declared empty rather than skipped so the template is still covered by the strict-render fuzz
    test: adding a ``{{ ... }}`` without declaring it here then fails."""


class FindingsPromptParams(TypedDict):
    """The full, typed context of ``autoprove_report_findings_prompt.j2``. Every key is required."""
    contract_name: str
    rule_name: str
    properties: list[FormalizedProperty]
    groups: list[PropertyGroup]
    instances: list[RuleEvidence]


_FINDINGS_SYSTEM = TypedTemplate[FindingsSystemParams]("autoprove_report_findings_system.j2")
_FINDINGS_PROMPT = TypedTemplate[FindingsPromptParams]("autoprove_report_findings_prompt.j2")


def _prompt(req: FindingRequest) -> str:
    return _FINDINGS_PROMPT.bind({
        "contract_name": req.contract_name,
        "rule_name": req.rule.name,
        "properties": req.properties,
        "groups": req.groups,
        "instances": req.evidence,
    }).render_to(load_jinja_template)


def prover_findings(
    fetch_evidence: EvidenceFetcher,
) -> FindingsSynthesis[AssessedFindingDraft]:
    """The Prover's synthesis, around the run-scoped capture that holds its evidence.

    Severity is `assessed`: a counterexample is a concrete reachable state of the program itself, so
    the axes are a judgement the evidence can carry."""
    return FindingsSynthesis(
        draft=AssessedFindingDraft,
        fetch_evidence=fetch_evidence,
        system=_FINDINGS_SYSTEM.bind({}).render_to(load_jinja_template),
        prompt=_prompt,
        assess=assessed,
    )
