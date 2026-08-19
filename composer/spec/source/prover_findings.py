"""The Certora Prover's half of findings synthesis: its evidence, its prompt, its risk assessment.

The shared loop is `composer.spec.source.report.findings`. What is here is only what makes a
finding a *Prover* finding — the captured counterexample analysis, a prompt that says the Prover
refuted the rule with a concrete counterexample, and a severity computed from the impact and
likelihood the model assessed against that counterexample.
"""
from dataclasses import dataclass
from typing import TypedDict

from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.findings import (
    AssessedFindingDraft, EvidenceFetcher, FindingRequest, FindingsSynthesis, assessed,
)
from composer.spec.source.report.schema import (
    FormalizedProperty, PropertyGroup, RuleRef, RuleVerdict,
)
from composer.templates.loader import load_jinja_template


@dataclass(frozen=True)
class RuleEvidence:
    """One failing instance of a violated rule: the Prover's root-cause explanation and a concrete
    counterexample, either of which may be absent. A parametric rule (``rule r(method f)``) fails once
    per binding, so a rule's evidence is a list of these; ``label`` names the instance ("" when the
    rule is not parametric)."""
    label: str = ""
    analysis: str | None = None
    counterexample: str | None = None


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


def _prompt(req: FindingRequest[RuleEvidence]) -> str:
    return _FINDINGS_PROMPT.bind({
        "contract_name": req.contract_name,
        "rule_name": req.rule.name,
        "properties": req.properties,
        "groups": req.groups,
        "instances": req.evidence,
    }).render_to(load_jinja_template)


def _proof_of_concept(instances: list[RuleEvidence]) -> str | None:
    """Every instance's counterexample, labelled once there is more than one — the report shows a
    single row per rule, so its PoC should cover all of that rule's failing instances."""
    traces = [(i.label, i.counterexample) for i in instances if i.counterexample]
    if len(traces) <= 1:
        return traces[0][1] if traces else None
    return "\n\n".join(f"# {label or 'counterexample'}\n{cex}" for label, cex in traces)


def _own_row(rule: RuleVerdict, _evidence: list[RuleEvidence]) -> RuleRef:
    """One row, one finding. A rule's evidence is its own instantiations — nothing here places a
    counterexample against a rule it was not captured for, so no two rows ever collapse."""
    return rule.ref


def prover_findings(
    fetch_evidence: EvidenceFetcher[RuleEvidence],
) -> FindingsSynthesis[RuleEvidence, AssessedFindingDraft]:
    """The Prover's synthesis, around the run-scoped capture that holds its evidence.

    Severity is `assessed`: a counterexample is a concrete reachable state of the program itself, so
    the axes are a judgement the evidence can carry."""
    return FindingsSynthesis(
        draft=AssessedFindingDraft,
        fetch_evidence=fetch_evidence,
        system=_FINDINGS_SYSTEM.bind({}).render_to(load_jinja_template),
        prompt=_prompt,
        assess=assessed,
        proof=_proof_of_concept,
        collapse=_own_row,
    )
