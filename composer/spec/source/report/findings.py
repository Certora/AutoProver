"""Write violated rules up as findings — the loop every backend shares.

For each BAD `RuleVerdict` this asks a model to write the issue up from the backend's
`RuleEvidence`. Evidence has one shape; backends differ in which fields they fill. A
`FindingsPolicy` is where the evidence comes from and what the model is told it means.

The rest is shared: walking BAD rows, properties and groups, collapsing stamped findings,
the proof of concept, concurrency, and composing the `Finding`. A backend that produces
no findings returns no policy and never reaches here.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.collect import EvidenceFetcher, RuleEvidence
from composer.spec.source.report.schema import (
    AuthoredContent, Finding, FindingProvenance, FormalizedProperty, ImpactLevel, IssueContent,
    LikelihoodLevel, Outcome, PropertyGroup, PropertyKey, RuleName, RuleRef, RuleVerdict,
    SeverityTier,
)
from composer.spec.types import FindingKey
from composer.templates.loader import load_jinja_template

type FindingIdentity = FindingKey | RuleRef

_log = logging.getLogger(__name__)

#: Cap on concurrent findings-synthesis LLM calls, so a violation-heavy run does not burst
#: the rate limit and drop write-ups.
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


class FindingDraft(AuthoredContent):
    """What a findings model must return: the authored sections, a title, and the two axes
    `severity_for` maps to a tier. The model never picks the tier."""
    title: str = Field(description="A one-line title naming the specific broken guarantee.")
    impact_level: ImpactLevel = Field(description=(
        "How severe the consequence is if exploited: 'high' (funds lost or stolen, protocol "
        "insolvency, permanently frozen assets, or unauthorized privileged control), 'medium' "
        "(limited, conditional, or recoverable loss, or temporary denial of service), 'low' (a minor "
        "deviation with no funds at risk), or 'none' (no real-world exploit path — a specification or "
        "code-quality observation)."
    ))
    likelihood_level: LikelihoodLevel = Field(description=(
        "How reachable the violation is: 'high' (any actor, no special preconditions), 'medium' "
        "(a specific but reachable state, ordering, or setup), or 'low' (privileged access, an unusual "
        "configuration, or a narrow window)."
    ))
    risk_reasoning: str = Field(description=(
        "One to three sentences justifying the impact and likelihood you assigned, grounded in the "
        "evidence you were given."
    ))


class FindingsSystemParams(TypedDict):
    """Context for ``autoprove_report_findings_system.j2``.

    One template for every backend: what the evidence is and how far it goes is the backend's
    claim; how severity is reached and which sections come back is the host's."""
    #: Backend prose: what its evidence is, what that does and does not establish, how to read
    #: its markers. Leads the message; the host contract follows it.
    domain: str


class FindingsPromptParams(TypedDict):
    """Context for every backend's findings user prompt: one violated rule and what is known
    about it.

    One shape for all of them. The Prover supplies its own template; Rust-authored backends
    share one host template. `build_findings` binds either.

    ``properties`` is every property the covered rule(s) formalize — jointly, or the union
    when several rows share one finding. This layer does not pick which actually broke."""
    contract_name: str
    rule_name: RuleName
    properties: list[FormalizedProperty]
    groups: list[PropertyGroup]
    evidence: list[RuleEvidence]
    #: Names of every covered check when several rows share one finding, including the
    #: subject. Empty for a one-row finding — the subject is ``rule_name`` alone.
    also_covers: list[RuleName]


_SYSTEM = TypedTemplate[FindingsSystemParams]("autoprove_report_findings_system.j2")


def proof_of_concept(evidence: list[RuleEvidence]) -> str | None:
    """Every instance's reproducer, labelled once there is more than one.

    Only what this run actually refuted. An expected-to-fail check this run did not hit has no
    proof of concept, and neither does an error that never reached a verdict. Never the
    accounting — that is about the run, not the program."""
    traces = [
        (e.label, e.counterexample) for e in evidence
        if e.counterexample and (e.ran is None or e.ran is Outcome.BAD)
    ]
    if len(traces) <= 1:
        return traces[0][1] if traces else None
    return "\n\n".join(f"# {label or 'counterexample'}\n{cex}" for label, cex in traces)


def finding_key(rule: RuleVerdict, evidence: list[RuleEvidence]) -> FindingIdentity:
    """The finding behind a row: the backend's `FindingKey` stamp, else this row's `RuleRef`.

    Rows that share a key are written up once. No stamp means no collapse. A stamp is compared
    as a raw string across the whole report — the same value on two components merges them —
    and is never inferred from matching evidence."""
    stamped = next((e.finding_key for e in evidence if e.finding_key is not None), None)
    return stamped if stamped is not None else rule.ref


@dataclass
class _Cluster:
    """One write-up: the first row, its evidence, and every row that shares its stamp."""
    rule: RuleVerdict
    evidence: list[RuleEvidence]
    covered: list[RuleVerdict]


@dataclass(frozen=True)
class FindingsPolicy:
    """Where one backend's evidence comes from, what it is, and how to ask for a write-up.

    ``domain`` and ``prompt`` are values (a string and a template). Only ``fetch_evidence``
    is a function, because only it does I/O."""

    fetch_evidence: EvidenceFetcher
    #: Backend half of the system message — see `FindingsSystemParams.domain`.
    domain: str
    #: User-message template, bound by `build_findings` from `FindingsPromptParams`.
    prompt: TypedTemplate[FindingsPromptParams]


async def build_findings(
    *,
    contract_name: str,
    rules: list[RuleVerdict],
    properties: list[FormalizedProperty],
    groups: list[PropertyGroup],
    policy: FindingsPolicy,
    llm: BaseChatModel,
) -> list[Finding]:
    """One `Finding` per violated rule (concurrent, best-effort); ``[]`` when nothing is violated.

    Fewer than one per rule when several rows share a finding stamp — see `finding_key`."""
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

    bound = llm.with_structured_output(FindingDraft)
    system = _SYSTEM.bind({"domain": policy.domain}).render_to(load_jinja_template)

    async def _evidence(rule: RuleVerdict) -> list[RuleEvidence] | None:
        """This rule's evidence, or None if fetching failed (the rule is then skipped)."""
        try:
            return await policy.fetch_evidence(rule.ref)
        except Exception:  # noqa: BLE001 — one rule failing must never fail the report
            _log.warning("report: evidence fetch failed for rule %r; skipping", rule.name,
                         exc_info=True)
            return None

    # Fetch every BAD row first so stamped findings collapse before any write-up is paid for.
    fetched = await asyncio.gather(*[_evidence(r) for r in bad])
    clusters: dict[FindingIdentity, _Cluster] = {}
    for rule, evidence in zip(bad, fetched):
        if evidence is None:
            continue
        key = finding_key(rule, evidence)
        cluster = clusters.get(key)
        if cluster is None:
            clusters[key] = _Cluster(rule, evidence, [rule])
        else:
            cluster.covered.append(rule)

    async def _one(cluster: _Cluster) -> Finding | None:
        rule, evidence, covered = cluster.rule, cluster.evidence, cluster.covered
        try:
            props: list[FormalizedProperty] = []
            seen: set[PropertyKey] = set()
            for r in covered:
                for p in props_by_ref.get(r.ref, []):
                    if p.key not in seen:
                        seen.add(p.key)
                        props.append(p)
            # A rule's properties may share a group (some may be in none); dedupe by slug, first-seen order.
            rule_groups = list({g.slug: g for p in props if (g := group_by_key.get(p.key)) is not None}.values())
            also_covers = list(dict.fromkeys(r.name for r in covered)) if len(covered) > 1 else []
            user = policy.prompt.bind({
                "contract_name": contract_name, "rule_name": rule.name, "properties": props,
                "groups": rule_groups, "evidence": evidence, "also_covers": also_covers,
            }).render_to(load_jinja_template)
            draft = await bound.ainvoke([SystemMessage(system), HumanMessage(user)])
            assert isinstance(draft, FindingDraft)
            return _compose(rule, draft, evidence, rule_groups, covered)
        except Exception:  # noqa: BLE001 — one finding failing must never fail the report
            _log.warning("report: finding synthesis failed for rule %r; skipping", rule.name, exc_info=True)
            return None

    sem = asyncio.Semaphore(_MAX_CONCURRENT_FINDING_CALLS)

    async def _bounded(cluster: _Cluster) -> Finding | None:
        async with sem:
            return await _one(cluster)

    findings = await asyncio.gather(*[_bounded(c) for c in clusters.values()])
    return [f for f in findings if f is not None]


def _compose(
    rule: RuleVerdict,
    draft: FindingDraft,
    evidence: list[RuleEvidence],
    groups: list[PropertyGroup],
    covered: list[RuleVerdict],
) -> Finding:
    links = list(dict.fromkeys(r.prover_link for r in covered if r.prover_link))
    return Finding(
        title=draft.title,
        severity=severity_for(draft.impact_level, draft.likelihood_level),
        content=IssueContent(
            **{f: getattr(draft, f) for f in AuthoredContent.model_fields},
            proof_of_concept=proof_of_concept(evidence),
            references=links or None,
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
            covers=[r.ref for r in covered] if len(covered) > 1 else [],
        ),
    )
