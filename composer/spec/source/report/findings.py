"""Synthesize Sherlock-``IssueIn``-shaped findings from violated rules.

For each violated rule (a `RuleVerdict` with ``outcome == Outcome.BAD``) this reshapes the
counterexample analysis the run already produced — looked up via the backend `EvidenceFetcher` — into
a `Finding`. One structured LLM call per violation, fed the *distilled* analysis rather than the raw
counterexample, so the expensive counterexample reasoning is not repeated. Best-effort per finding:
any failure drops that one finding, never the report.

v1 scope: prover-only (a foundry BAD is an author-declared demonstration, not a discovered bug).
Severity is LLM-assigned from the default Sherlock rubric via the Impact × Likelihood matrix in the
system template. ``IssueIn.locations`` is not produced here — a run knows only local paths and CVL-spec
lines, so the submission layer reconstructs source locations from the engagement scope + counterexample
(the accurate report-time locator is on ``FindingProvenance``: rule name, spec file, prover-run link).
"""
import asyncio
import logging
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from composer.templates.loader import load_jinja_template
from composer.spec.source.report.collect import EvidenceFetcher, RuleEvidence
from composer.spec.source.report.schema import (
    Finding, FindingProvenance, FormalizedProperty, IssueContent, Outcome,
    PropertyGroup, PropertyKey, ReportBackend, RuleRef, RuleVerdict,
)

_log = logging.getLogger(__name__)

Severity = Literal["critical", "high", "medium", "low", "informational"]

#: Bound the counterexample we inline (as prompt input and as ``proof_of_concept``).
_MAX_CEX_CHARS = 8000


class FindingDraft(BaseModel):
    """The LLM-authored prose + severity for one finding. Code fills locations / PoC / references /
    provenance around it. Mirrors the Sherlock ``IssueContent`` (minus the machine-filled fields)."""
    title: str = Field(max_length=200, description="One-line, issue-specific title.")
    severity: Severity = Field(description="critical | high | medium | low | informational.")
    severity_reasoning: str = Field(
        max_length=4000,
        description="1-3 sentences: the Impact tier, the Likelihood tier, and the matrix cell they select.",
    )
    summary: str = Field(max_length=2000, description="1-3 sentence tl;dr.")
    description: str = Field(max_length=50000, description="Full technical description grounded in the counterexample.")
    impact: str = Field(max_length=20000, description="Concrete consequence if exploited.")
    attack_path: str | None = Field(default=None, max_length=20000, description="Step-by-step exploit path, or null.")
    assumptions_and_uncertainties: str | None = Field(default=None, max_length=10000, description="Assumptions / uncertainties, or null.")


async def build_findings(
    *,
    contract_name: str,
    backend: ReportBackend,
    rules: list[RuleVerdict],
    properties: list[FormalizedProperty],
    groups: list[PropertyGroup],
    fetch_evidence: EvidenceFetcher | None,
    llm: BaseChatModel,
) -> list[Finding]:
    """One `Finding` per violated rule (concurrent, best-effort). Returns ``[]`` for a non-prover
    backend or when nothing is violated."""
    if backend != "prover":
        return []
    bad = [r for r in rules if r.outcome == Outcome.BAD]
    if not bad:
        return []

    # Reverse index a violated rule -> the properties it breaks -> their audit group (for prose context).
    props_by_ref: dict[RuleRef, list[FormalizedProperty]] = {}
    for p in properties:
        for ref in p.rule_refs:
            props_by_ref.setdefault(ref, []).append(p)
    group_by_key: dict[PropertyKey, PropertyGroup] = {}
    for g in groups:
        for k in g.members:
            group_by_key.setdefault(k, g)

    system = load_jinja_template("autoprove_report_findings_system.j2")
    bound = llm.with_structured_output(FindingDraft)

    async def _one(rule: RuleVerdict) -> Finding | None:
        try:
            ev = await fetch_evidence(rule.prover_link, rule.name) if fetch_evidence else None
            prop = _pick_property(props_by_ref.get(rule.ref, []))
            group = group_by_key.get(prop.key) if prop else None
            user = load_jinja_template(
                "autoprove_report_findings_prompt.j2",
                contract_name=contract_name,
                rule_name=rule.name,
                property_title=prop.title if prop else None,
                property_description=prop.description if prop else None,
                property_sort=prop.sort if prop else None,
                group_title=group.title if group else None,
                group_description=group.description if group else None,
                analysis=ev.analysis if ev else None,
                counterexample=_trim(ev.counterexample) if ev else None,
            )
            draft = await bound.ainvoke([SystemMessage(system), HumanMessage(user)])
            assert isinstance(draft, FindingDraft)
            return _compose(rule, draft, ev, group_slug=group.slug if group else None)
        except Exception:  # noqa: BLE001 — one finding failing must never fail the report
            _log.warning("report: finding synthesis failed for rule %r; skipping", rule.name, exc_info=True)
            return None

    findings = await asyncio.gather(*[_one(r) for r in bad])
    return [f for f in findings if f is not None]


def _pick_property(props: list[FormalizedProperty]) -> FormalizedProperty | None:
    """A violated rule may formalize several properties; pick a stable representative (first by
    title) to describe the finding. They describe the same violation, so the choice affects only
    which prose seeds the write-up."""
    return sorted(props, key=lambda p: p.title)[0] if props else None


def _trim(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= _MAX_CEX_CHARS else text[:_MAX_CEX_CHARS] + "\n…(truncated)"


def _compose(
    rule: RuleVerdict,
    draft: FindingDraft,
    ev: RuleEvidence | None,
    *,
    group_slug: str | None,
) -> Finding:
    return Finding(
        title=draft.title,
        severity=draft.severity,
        content=IssueContent(
            summary=draft.summary,
            description=draft.description,
            impact=draft.impact,
            attack_path=draft.attack_path,
            assumptions_and_uncertainties=draft.assumptions_and_uncertainties,
            proof_of_concept=_trim(ev.counterexample) if ev else None,
            references=[rule.prover_link] if rule.prover_link else None,
        ),
        provenance=FindingProvenance(
            rule_name=rule.name,
            spec_file=rule.spec_file,
            outcome=rule.outcome,
            group_slug=group_slug,
            prover_link=rule.prover_link,
            severity_reasoning=draft.severity_reasoning,
        ),
    )
