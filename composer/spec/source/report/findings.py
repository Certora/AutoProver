"""Synthesize findings from violated rules — the loop every backend shares.

For each violated rule (a `RuleVerdict` with ``outcome == Outcome.BAD``) this asks a model to write
the issue up. What counts as evidence, how the model is asked for it, and how the risk is assessed
are the *backend's* — a `FindingsSynthesis` carries all three. What is shared is everything around
them: walking the BAD rules, resolving each one's properties and the audit groups they sit in,
bounding the concurrency, keeping one failed write-up from costing the rest, and composing the
`Finding` the report persists.

A backend that produces no findings returns no `FindingsSynthesis` and never reaches here.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

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
    """Map an assessed (impact, likelihood) pair to a severity. ``none`` impact -> informational.

    For a backend whose evidence supports that judgement. One whose does not says so by assessing
    a constant instead — see `Assessment`."""
    if impact == "none":
        return "informational"
    return _SEVERITY_MATRIX[(impact, likelihood)]


class FindingDraft(AuthoredContent):
    """The least a findings model must return: the authored sections, plus a title.

    A backend subclasses this to ask for more — the prover adds the impact and likelihood axes its
    severity is computed from — and names the subclass in its `FindingsSynthesis`, so the model is
    only ever asked for what that backend's evidence can support."""
    title: str = Field(description="A one-line title naming the specific broken guarantee.")


@dataclass(frozen=True)
class Assessment:
    """A backend's risk verdict on one draft: the severity, and the record of how it was reached.

    The three optional fields are provenance, not inputs — they say what the severity *rests on*.
    A backend that assesses risk from the model's axes records them; one that assigns a constant
    leaves them empty rather than fabricating a rating nothing produced."""
    severity: SeverityTier
    impact: ImpactLevel | None = None
    likelihood: LikelihoodLevel | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class FindingRequest[E]:
    """One violated rule and everything known about it, for a backend to render a prompt from.

    ``properties`` is *every* property the rule formalizes, not a pre-picked one: a rule may jointly
    formalize several and only the evidence says which actually broke, which is the model's job to
    determine rather than this layer's to guess."""
    contract_name: str
    rule: RuleVerdict
    properties: list[FormalizedProperty]
    groups: list[PropertyGroup]
    evidence: list[E]


#: Every captured failing instance of a violated rule, or ``[]`` when the backend has none for it.
#: Keyed by `RuleRef` — ``(file, name)``, how the report identifies a row — because a name alone
#: does not: one deliverable can hold several components' checks, and two authors given the same
#: property write the same name.
type EvidenceFetcher[E] = Callable[[RuleRef], Awaitable[list[E]]]

#: The user message for one rule's write-up. The backend owns it because the prompt is a claim about
#: what the evidence *is* — "the Certora Prover found a concrete counterexample" is true of one
#: backend's evidence and false of another's.
type FindingPrompt[E] = Callable[[FindingRequest[E]], str]


@dataclass(frozen=True)
class FindingsSynthesis[E, D: FindingDraft]:
    """How one backend turns its violated rules into written findings.

    Generic over its evidence ``E`` and the draft ``D`` it asks the model for, so neither is a union
    of every backend's needs: the prover's captured counterexample analysis and a fuzzer's crash
    metadata have almost nothing in common, and a struct holding both would leave half its fields
    meaningless whichever backend filled it."""

    #: The structured-output schema the model answers in.
    draft: type[D]
    fetch_evidence: EvidenceFetcher[E]
    #: System message. Constant across this backend's rules — the per-rule context is `prompt`.
    system: str
    prompt: FindingPrompt[E]
    #: Takes the evidence as well as the draft: what a severity rests on is not always something
    #: the model said — a declared finding's ground is the author's reason, which is evidence.
    assess: Callable[[D, list[E]], Assessment]
    #: The finding's ``proof_of_concept`` from its evidence, or None when the evidence is not one.
    proof: Callable[[list[E]], str | None]


async def build_findings[E, D: FindingDraft](
    *,
    contract_name: str,
    rules: list[RuleVerdict],
    properties: list[FormalizedProperty],
    groups: list[PropertyGroup],
    synthesis: FindingsSynthesis[E, D],
    llm: BaseChatModel,
) -> list[Finding]:
    """One `Finding` per violated rule (concurrent, best-effort); ``[]`` when nothing is violated."""
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

    bound = llm.with_structured_output(synthesis.draft)

    async def _one(rule: RuleVerdict) -> Finding | None:
        try:
            evidence = await synthesis.fetch_evidence(rule.ref)
            props = props_by_ref.get(rule.ref, [])
            # A rule's properties may share a group (some may be in none); dedupe by slug, first-seen order.
            rule_groups = list({g.slug: g for p in props if (g := group_by_key.get(p.key)) is not None}.values())
            user = synthesis.prompt(FindingRequest(
                contract_name=contract_name, rule=rule, properties=props,
                groups=rule_groups, evidence=evidence,
            ))
            draft = await bound.ainvoke([SystemMessage(synthesis.system), HumanMessage(user)])
            assert isinstance(draft, synthesis.draft)
            return _compose(rule, draft, synthesis, evidence, rule_groups)
        except Exception:  # noqa: BLE001 — one finding failing must never fail the report
            _log.warning("report: finding synthesis failed for rule %r; skipping", rule.name, exc_info=True)
            return None

    sem = asyncio.Semaphore(_MAX_CONCURRENT_FINDING_CALLS)

    async def _bounded(rule: RuleVerdict) -> Finding | None:
        async with sem:
            return await _one(rule)

    findings = await asyncio.gather(*[_bounded(r) for r in bad])
    return [f for f in findings if f is not None]


def _compose[E, D: FindingDraft](
    rule: RuleVerdict,
    draft: D,
    synthesis: FindingsSynthesis[E, D],
    evidence: list[E],
    groups: list[PropertyGroup],
) -> Finding:
    risk = synthesis.assess(draft, evidence)
    return Finding(
        title=draft.title,
        severity=risk.severity,
        content=IssueContent(
            **{f: getattr(draft, f) for f in AuthoredContent.model_fields},
            proof_of_concept=synthesis.proof(evidence),
            references=[rule.prover_link] if rule.prover_link else None,
        ),
        provenance=FindingProvenance(
            rule_name=rule.name,
            spec_file=rule.spec_file,
            outcome=rule.outcome,
            group_slugs=[g.slug for g in groups],
            prover_link=rule.prover_link,
            impact=risk.impact,
            likelihood=risk.likelihood,
            risk_reasoning=risk.reasoning,
        ),
    )
