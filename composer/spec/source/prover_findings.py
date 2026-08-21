"""The Certora Prover's half of findings synthesis.

The shared loop is `composer.spec.source.report.findings`. This module supplies the Prover
prompt (a concrete counterexample refuted the rule) and the run-scoped analysis store.
"""
from typing import TypedDict

from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.collect import EvidenceFetcher
from composer.spec.source.report.findings import FindingsPolicy, FindingsPromptParams
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
