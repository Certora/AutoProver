"""Synthesize findings from violated rules.

For each violated rule (a `RuleVerdict` with ``outcome == Outcome.BAD``) this asks an LLM to write up
the issue, grounded in the rule's counterexample analysis (captured during the run and looked up via
the backend's `EvidenceFetcher`) and the properties the rule formalizes. The model assesses the impact
and likelihood; this module computes the severity from them via a fixed matrix — the LLM never picks a
severity directly. Findings are produced only when the backend supplies an evidence fetcher; each
finding is best-effort, so a synthesis failure drops that one finding rather than the report.
"""
import asyncio
import logging
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from composer.spec.gen_types import TypedTemplate
from composer.templates.loader import load_jinja_template
from composer.spec.source.report.collect import EvidenceFetcher, RuleEvidence
from composer.spec.source.report.schema import (
    AuthoredContent, Finding, FindingProvenance, FormalizedProperty, ImpactLevel, IssueContent,
    LikelihoodLevel, Outcome, PropertyGroup, PropertyKey, RuleRef, RuleVerdict, SeverityTier,
)

_log = logging.getLogger(__name__)

#: Cap on concurrent findings-synthesis LLM calls (one per violated rule), so a violation-heavy run
#: doesn't burst dozens of heavy-model requests at once (which rate limits would turn into dropped
#: findings).
_MAX_CONCURRENT_FINDING_CALLS = 8

# Impact × Likelihood -> severity. ``none`` impact (no real-world exploit path) is informational,
# handled in `severity_for` rather than the table. Low impact caps at ``low`` regardless of likelihood:
# a trivially-triggerable but low-impact break is still only low (a cosmetic issue shouldn't read
# medium just because anyone can hit it).
_SEVERITY_MATRIX: dict[tuple[ImpactLevel, LikelihoodLevel], SeverityTier] = {
    ("high", "high"): "critical", ("high", "medium"): "high", ("high", "low"): "medium",
    ("medium", "high"): "high", ("medium", "medium"): "medium", ("medium", "low"): "low",
    ("low", "high"): "low", ("low", "medium"): "low", ("low", "low"): "low",
}


def severity_for(impact: ImpactLevel, likelihood: LikelihoodLevel) -> SeverityTier:
    """Map an assessed (impact, likelihood) pair to a severity. ``none`` impact -> informational."""
    if impact == "none":
        return "informational"
    return _SEVERITY_MATRIX[(impact, likelihood)]


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


class FindingDraft(AuthoredContent):
    """Write up a single confirmed vulnerability finding for a formal-verification rule that the
    Certora Prover refuted with a concrete counterexample."""
    title: str = Field(description="A one-line title naming the specific broken guarantee.")
    impact_level: ImpactLevel = Field(description=(
        "How severe the consequence is if exploited: 'high' (funds lost or stolen, protocol "
        "insolvency, permanently frozen assets, or unauthorized privileged control), 'medium' "
        "(limited, conditional, or recoverable loss, or temporary denial of service), 'low' (a minor "
        "deviation with no funds at risk), or 'none' (no real-world exploit path — a specification or "
        "code-quality observation)."
    ))
    likelihood_level: LikelihoodLevel = Field(description=(
        "How reachable the counterexample is: 'high' (any actor, no special preconditions), 'medium' "
        "(a specific but reachable state, ordering, or setup), or 'low' (privileged access, an unusual "
        "configuration, or a narrow window)."
    ))
    risk_reasoning: str = Field(description=(
        "One to three sentences justifying the impact and likelihood you assigned, grounded in the "
        "counterexample."
    ))


async def build_findings(
    *,
    contract_name: str,
    rules: list[RuleVerdict],
    properties: list[FormalizedProperty],
    groups: list[PropertyGroup],
    fetch_evidence: EvidenceFetcher | None,
    llm: BaseChatModel,
) -> list[Finding]:
    """One `Finding` per violated rule (concurrent, best-effort). Findings are produced only when the
    backend supplies an evidence fetcher; returns ``[]`` otherwise or when nothing is violated."""
    if fetch_evidence is None:
        return []
    bad = [r for r in rules if r.outcome == Outcome.BAD]
    if not bad:
        return []

    # A violated rule -> every property it formalizes (a rule may jointly formalize several) -> the
    # audit groups those properties sit in. Properties partition into groups (coverage-validated), so
    # `group_by_key` has one entry per key.
    props_by_ref: dict[RuleRef, list[FormalizedProperty]] = {}
    for p in properties:
        for ref in p.rule_refs:
            props_by_ref.setdefault(ref, []).append(p)
    group_by_key: dict[PropertyKey, PropertyGroup] = {k: g for g in groups for k in g.members}

    system = _FINDINGS_SYSTEM.bind({}).render_to(load_jinja_template)
    bound = llm.with_structured_output(FindingDraft)

    async def _one(rule: RuleVerdict) -> Finding | None:
        try:
            instances = await fetch_evidence(rule.name)
            # Every rule in the report is referenced by >=1 property (collect drops orphans), so pass
            # ALL of them: the rule may jointly formalize several, and only the counterexample says
            # which one actually broke — that is the LLM's job, not ours to pre-guess.
            props = props_by_ref.get(rule.ref, [])
            # A rule's properties may share a group (some may be in none); dedupe by slug, first-seen order.
            rule_groups = list({g.slug: g for p in props if (g := group_by_key.get(p.key)) is not None}.values())
            user = _FINDINGS_PROMPT.bind({
                "contract_name": contract_name,
                "rule_name": rule.name,
                "properties": props,
                "groups": rule_groups,
                "instances": instances,
            }).render_to(load_jinja_template)
            draft = await bound.ainvoke([SystemMessage(system), HumanMessage(user)])
            assert isinstance(draft, FindingDraft)
            return _compose(rule, draft, instances, rule_groups)
        except Exception:  # noqa: BLE001 — one finding failing must never fail the report
            _log.warning("report: finding synthesis failed for rule %r; skipping", rule.name, exc_info=True)
            return None

    sem = asyncio.Semaphore(_MAX_CONCURRENT_FINDING_CALLS)

    async def _bounded(rule: RuleVerdict) -> Finding | None:
        async with sem:
            return await _one(rule)

    findings = await asyncio.gather(*[_bounded(r) for r in bad])
    return [f for f in findings if f is not None]


def _proof_of_concept(instances: list[RuleEvidence]) -> str | None:
    """Every instance's counterexample, labelled once there is more than one — the report shows a
    single row per rule, so its PoC should cover all of that rule's failing instances."""
    traces = [(i.label, i.counterexample) for i in instances if i.counterexample]
    if len(traces) <= 1:
        return traces[0][1] if traces else None
    return "\n\n".join(f"# {label or 'counterexample'}\n{cex}" for label, cex in traces)


def _compose(
    rule: RuleVerdict,
    draft: FindingDraft,
    instances: list[RuleEvidence],
    groups: list[PropertyGroup],
) -> Finding:
    return Finding(
        title=draft.title,
        severity=severity_for(draft.impact_level, draft.likelihood_level),
        content=IssueContent(
            **{f: getattr(draft, f) for f in AuthoredContent.model_fields},
            proof_of_concept=_proof_of_concept(instances),
            references=[rule.prover_link] if rule.prover_link else None,
        ),
        provenance=FindingProvenance(
            rule_name=rule.name,
            spec_file=rule.spec_file,
            outcome=rule.outcome,
            group_slugs=[g.slug for g in groups],
            prover_link=rule.prover_link,
            impact=draft.impact_level,
            likelihood=draft.likelihood_level,
            risk_reasoning=draft.risk_reasoning,
        ),
    )
