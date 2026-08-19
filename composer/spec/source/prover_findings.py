"""The Certora Prover's half of findings synthesis: where its evidence comes from, and what the
model is told that evidence is.

The shared loop is `composer.spec.source.report.findings`. What is here is only what makes a finding
a *Prover* finding — a prompt that says the Prover refuted the rule with a concrete counterexample,
and the root-cause analysis captured during the run to ground it in.
"""
from typing import TypedDict

from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.findings import (
    EvidenceFetcher, FindingsPolicy, FindingsPromptParams,
)
from composer.templates.loader import load_jinja_template


class ProverDomainParams(TypedDict):
    """``autoprove_report_findings_prover_domain.j2`` takes no parameters — the prose is static.
    Declared empty rather than skipped so the template is still covered by the strict-render fuzz
    test: adding a ``{{ ... }}`` without declaring it here then fails."""


_PROVER_DOMAIN = TypedTemplate[ProverDomainParams]("autoprove_report_findings_prover_domain.j2")
_WRITE_UP_PROMPT = TypedTemplate[FindingsPromptParams]("autoprove_report_findings_prompt.j2")


def prover_findings(fetch_evidence: EvidenceFetcher) -> FindingsPolicy:
    """The Prover's findings policy, around the run-scoped capture that holds its evidence."""
    return FindingsPolicy(
        fetch_evidence=fetch_evidence,
        domain=_PROVER_DOMAIN.bind({}).render_to(load_jinja_template),
        prompt=_WRITE_UP_PROMPT,
    )
