"""A Rust backend's half of findings synthesis: what a fuzzing campaign observed, and how to ask
for it to be written up.

The shared loop is `composer.spec.source.report.findings`. What is here is what makes a finding a
*fuzzer's* finding: evidence that is a campaign's own report of one check, a prompt that says a
fuzzer found a crash rather than that a prover refuted a rule, and a severity that stays
``informational`` because nothing in this pipeline has established that any of it is exploitable.
"""
from dataclasses import dataclass
from typing import TypedDict

from composer.pipeline.ptypes import ComponentOutcome, Delivered
from composer.rustapp.result import RustFormalResult
from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.findings import (
    Assessment, FindingDraft, FindingRequest, FindingsSynthesis,
)
from composer.spec.source.report.schema import (
    FormalizedProperty, Outcome, PropertyGroup, RuleRef,
)
from composer.spec.system_model import FeatureUnit
from composer.spec.types import CheckName
from composer.templates.loader import load_jinja_template

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]


@dataclass(frozen=True)
class FuzzEvidence:
    """What one campaign observed about one check, kept split by where each part came from.

    The split is the point. A declared check reports ``BAD`` whether or not this run reproduced it,
    so the reported outcome cannot say which case a reader is looking at — only ``ran`` and
    ``declared`` together can, and a write-up that confuses "the fuzzer found this" with "the author
    says this is broken" is worse than none."""

    component: str
    check: CheckName
    #: The wheel's own outcome, before the author's declaration is folded in: ``BAD`` here means
    #: this campaign produced a counterexample.
    ran: Outcome
    #: The campaign's text for this check — its counterexample with the reproducing sequence and any
    #: ``SUSPECT HARNESS BUG`` marker when it found one, and what the campaign spent either way.
    #:
    #: One string because the wheel sends one: ``Verdict.detail`` is the counterexample with the
    #: campaign accounting appended, and nothing on this side can tell where one ends. Splitting
    #: them is a wire change (`Verdict` would carry the accounting separately), not a parse.
    detail: str | None
    #: Why the author declared a failure here to be the finding, when they did. Present means the
    #: row is a claim the author made, not only something the run tripped over.
    declared: str | None


class FuzzFindingsSystemParams(TypedDict):
    """``autoprove_report_findings_fuzz_system.j2`` takes no parameters — the instructions are
    static. Declared empty rather than skipped so the template is still covered by the strict-render
    fuzz test: adding a ``{{ ... }}`` without declaring it here then fails."""


class FuzzFindingsPromptParams(TypedDict):
    """The full, typed context of ``autoprove_report_findings_fuzz_prompt.j2``."""
    contract_name: str
    check_name: str
    properties: list[FormalizedProperty]
    groups: list[PropertyGroup]
    observations: list[FuzzEvidence]


_FUZZ_SYSTEM = TypedTemplate[FuzzFindingsSystemParams]("autoprove_report_findings_fuzz_system.j2")
_FUZZ_PROMPT = TypedTemplate[FuzzFindingsPromptParams]("autoprove_report_findings_fuzz_prompt.j2")


def observations(outcomes: RustOutcomes) -> dict[RuleRef, FuzzEvidence]:
    """Every delivered check, keyed as the report keys its rows: ``(file, name)``.

    Rebuilt rather than looked up: ``collect`` uses the verdict's file, falling back to the
    component artifact, and a callout-mode wheel gives every component that same fallback — so the
    check name alone does not identify a row.

    First component naming a key wins, as in ``collect``: where two of them do collapse onto one
    row, the evidence here has to be the same run's as the message there."""
    observed: dict[RuleRef, FuzzEvidence] = {}
    for o in outcomes:
        if not isinstance(o.result, Delivered):
            continue
        res = o.result.result
        for check, verdict in res.verdicts.items():
            ref = (verdict.unit_file or o.result.unit_file, check)
            observed.setdefault(ref, FuzzEvidence(
                component=o.feat.display_name,
                check=check,
                ran=verdict.outcome,
                detail=verdict.detail,
                declared=res.expected_failures.get(check),
            ))
    return observed


def _prompt(req: FindingRequest[FuzzEvidence]) -> str:
    return _FUZZ_PROMPT.bind({
        "contract_name": req.contract_name,
        "check_name": req.rule.name,
        "properties": req.properties,
        "groups": req.groups,
        "observations": req.evidence,
    }).render_to(load_jinja_template)


def _assess(_draft: FindingDraft, evidence: list[FuzzEvidence]) -> Assessment:
    """Always ``informational``, with no impact or likelihood recorded.

    A campaign establishes that an assertion can be made to fail, not that anyone can profit from
    it: it runs against a fixture the author wrote, from a state the fixture set up, and a crash on
    a failed precondition looks exactly like a crash on a real one. Nothing here has assessed
    exploitability, and a fabricated ``high`` on a finding a reader trusts is worse than a blank.

    The reasoning is the author's declaration where there is one, so a reader can tell a finding the
    author documented from one the campaign tripped over without reading the evidence."""
    return Assessment(
        severity="informational",
        reasoning=next((e.declared for e in evidence if e.declared), None),
    )


def _proof(evidence: list[FuzzEvidence]) -> str | None:
    """The campaign's own text, and only where this run actually produced a violation.

    A declared check the run did not reproduce has no proof of concept — its ground is the author's
    reading, which rides ``provenance.risk_reasoning`` instead."""
    crashes = [e.detail for e in evidence if e.ran is Outcome.BAD and e.detail]
    return "\n\n".join(crashes) if crashes else None


def fuzz_findings(outcomes: RustOutcomes) -> FindingsSynthesis[FuzzEvidence, FindingDraft]:
    """This run's synthesis, around the campaign observations its own results carry."""
    observed = observations(outcomes)

    async def fetch(ref: RuleRef) -> list[FuzzEvidence]:
        found = observed.get(ref)
        return [found] if found is not None else []

    return FindingsSynthesis(
        draft=FindingDraft,
        fetch_evidence=fetch,
        system=_FUZZ_SYSTEM.bind({}).render_to(load_jinja_template),
        prompt=_prompt,
        assess=_assess,
        proof=_proof,
    )
