"""Synthesize findings from violated rules — the loop every backend shares.

For each violated rule (a `RuleVerdict` with ``outcome == Outcome.BAD``) this asks a model to write
the issue up, grounded in the `RuleEvidence` the backend hands back for it.

Evidence has one shape for every backend. Backends differ in what they can *fill* — the prover has a
root-cause analysis and a counterexample trace, a fuzzing wheel has a crashing input and an account
of what its run covered — but "instance, explanation, reproducer, and what the run itself did" is not
a claim about any one of them. What is genuinely per-backend is where that evidence is *fetched*
from and what the model is told it means; a `FindingsPolicy` carries those — as values, not hooks,
save the fetcher.

The rest is shared outright: walking the BAD rules, resolving each one's properties and the audit
groups they sit in, collapsing rows that are one finding, binding the prompt, building the proof of
concept, bounding the concurrency, keeping one failed write-up from costing the rest, and composing
the `Finding` the report persists.

A backend that produces no findings returns no `FindingsPolicy` and never reaches here.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import ClassVar, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.schema import (
    AuthoredContent, Finding, FindingProvenance, FormalizedProperty, ImpactLevel, IssueContent,
    LikelihoodLevel, Outcome, PropertyGroup, PropertyKey, RuleName, RuleRef, RuleVerdict,
    SeverityTier,
)
from composer.templates.loader import load_jinja_template

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
    """What a findings model must return: the authored sections, a title, and the two axes
    `severity_for` maps to a tier.

    The model never picks the tier, only the axes, so the severity a reader sees is reproducible
    from what the model actually said."""
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


@dataclass(frozen=True)
class Assessment:
    """The risk verdict on one draft: the severity, and the record of how it was reached.

    The three axes are provenance — they say what the tier *rests on*, so a reader can see the
    judgement behind it rather than only its result."""
    severity: SeverityTier
    impact: ImpactLevel
    likelihood: LikelihoodLevel
    reasoning: str


def assess(draft: FindingDraft) -> Assessment:
    """Severity from the matrix. Every backend's, because every backend's evidence is a violation
    that happened — what differs is what a violation *is* there, which is what the prompt says."""
    return Assessment(
        severity=severity_for(draft.impact_level, draft.likelihood_level),
        impact=draft.impact_level,
        likelihood=draft.likelihood_level,
        reasoning=draft.risk_reasoning,
    )


@dataclass(frozen=True)
class RuleEvidence:
    """One failing instance of a violated rule: what a run observed about it, split by where each
    part came from.

    A backend fills the parts it has and leaves the rest ``None``. Absence always means "this run
    recorded nothing of that kind" — never that it recorded something empty — so the fields mean the
    same thing whichever backend produced them, and a prompt can say what is missing rather than
    guessing."""

    #: Which instance this is, where a rule can fail more than once: a parametric binding
    #: (``rule r(method f)``) for the prover, the component for a per-component backend. ``""``
    #: when the rule fails only once.
    label: str = ""
    #: The backend's own account of *why* the check failed, where it produces one — a root-cause
    #: explanation rather than the raw failure.
    analysis: str | None = None
    #: What reproduces the failure: a counterexample trace, a crashing input, an assertion message.
    counterexample: str | None = None
    #: The run's own outcome for this check, before any authored declaration is folded into the
    #: outcome the report shows. ``None`` for a backend that captures evidence only for failures,
    #: where it would add nothing; where a check can be reported BAD on the author's say-so, this is
    #: the only thing that says whether the run actually reproduced it.
    ran: Outcome | None = None
    #: The run's evidence about *itself*: what it spent against its budget, how far it reached, and
    #: whether this check was exercised at all. What makes a result weigh something — and what a
    #: proof of concept must not be padded with.
    accounting: str | None = None
    #: Why the author declared a failure here to be expected, when they did. Present means the row
    #: is a claim the author made, not only something the run tripped over.
    declared: str | None = None
    #: Which *finding* this belongs to, when one piece of evidence condemns several checks at once.
    #: Opaque — only ever compared, never read into. ``None`` is evidence standing on its own.
    finding: str | None = None


class FindingsSystemParams(TypedDict):
    """The full, typed context of ``autoprove_report_findings_system.j2``.

    One template for every backend, because the two halves of a findings system prompt split the
    same way everywhere: what this evidence *is* is the backend's claim, and how severity is reached
    and what sections come back is the host's contract. Only the first varies."""
    #: The backend's own prose: what its evidence is, what it does and does not establish, and how to
    #: read its own markers. Leads the message; the contract follows it.
    domain: str


class FindingsPromptParams(TypedDict):
    """The full, typed context of every backend's findings prompt: one violated rule and everything
    known about it.

    One shape for all of them, because a backend's prompt differs in what it *says* about the
    evidence, not in what it is given — "the Certora Prover found a concrete counterexample" is a
    claim about the same fields a fuzzing wheel fills differently. The backend supplies the template;
    `build_findings` binds it.

    ``properties`` is *every* property the rule formalizes, not a pre-picked one: a rule may jointly
    formalize several and only the evidence says which actually broke, which is the model's job to
    determine rather than this layer's to guess."""
    contract_name: str
    rule_name: RuleName
    properties: list[FormalizedProperty]
    groups: list[PropertyGroup]
    evidence: list[RuleEvidence]
    #: The other BAD rows this one write-up answers for — rows the backend could not tell apart from
    #: this one on the evidence it has. Empty in the ordinary case. Non-empty is itself something the
    #: write-up should say: the run found one thing and cannot name which check it broke.
    also_covers: list[RuleName]


#: Every captured failing instance of a violated rule, or ``[]`` when the backend has none for it.
#: Keyed by `RuleRef` — ``(file, name)``, how the report identifies a row — because a name alone
#: does not: one deliverable can hold several components' checks, and two authors given the same
#: property write the same name.
type EvidenceFetcher = Callable[[RuleRef], Awaitable[list[RuleEvidence]]]

_SYSTEM = TypedTemplate[FindingsSystemParams]("autoprove_report_findings_system.j2")


def proof_of_concept(evidence: list[RuleEvidence]) -> str | None:
    """Every instance's reproducer, labelled once there is more than one — the report shows a single
    row per rule, so its PoC should cover all of that rule's failing instances.

    Only what this run actually refuted. A declared failure the run did not reproduce has no proof of
    concept at all — its ground is the author's reading, which rides ``provenance.risk_reasoning`` —
    and neither does an error that reached no verdict. Never the accounting: what a run spent is a
    claim about the run, and padding a proof of concept with it leaves a reader unable to see where
    the evidence ends."""
    traces = [
        (e.label, e.counterexample) for e in evidence
        if e.counterexample and (e.ran is None or e.ran is Outcome.BAD)
    ]
    if len(traces) <= 1:
        return traces[0][1] if traces else None
    return "\n\n".join(f"# {label or 'counterexample'}\n{cex}" for label, cex in traces)


def finding_key(rule: RuleVerdict, evidence: list[RuleEvidence]) -> Hashable:
    """Identity of the *finding* behind a row: the key the backend stamped on its evidence, else the
    row itself. Rows sharing a key are written up once — see `build_findings`.

    A backend whose rows are one-to-one with findings stamps nothing and nothing ever collapses. One
    whose run can conclude something it cannot attribute to a single check fans that conclusion out
    over every check it covered and stamps them alike, so the run is written up once rather than once
    per row it took down — for a component's whole property set, dozens of them, each guessing a
    different check.

    The relation is never inferred from the evidence: rows fanned out from one conclusion look
    exactly like several checks that failed the same way, and those are two different facts about the
    program. Only the backend can tell them apart."""
    stamped = next((e.finding for e in evidence if e.finding is not None), None)
    return stamped if stamped is not None else rule.ref


@dataclass(frozen=True)
class FindingsPolicy:
    """What one backend's findings rest on: where its evidence comes from, what the model is told
    that evidence is, and what it can be asked to conclude from it.

    Values, not hooks: the prose is a string and the prompt is a template — a Rust wheel ships the
    first across the FFI boundary as JSON, which is the proof it was never behaviour. Only the
    fetcher is a function, because only it does I/O."""

    fetch_evidence: EvidenceFetcher
    #: The backend's half of the system message — see `FindingsSystemParams.domain`. Constant across
    #: this backend's rules; the per-rule context is `prompt`.
    domain: str
    #: The user message's template, bound by `build_findings` from `FindingsPromptParams`. The
    #: backend owns the prose because the prompt is a claim about what its evidence *is*; it does not
    #: own the binding, because the fields are the same for everyone.
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

    Fewer than one per rule where several rows are stamped as the same finding — see
    `finding_key`."""
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
        """This rule's evidence, or None when fetching it failed — which drops the rule rather than
        writing it up against nothing."""
        try:
            return await policy.fetch_evidence(rule.ref)
        except Exception:  # noqa: BLE001 — one rule failing must never fail the report
            _log.warning("report: evidence fetch failed for rule %r; skipping", rule.name,
                         exc_info=True)
            return None

    # Every BAD row's evidence first, so rows whose write-up would be the same write-up collapse
    # before any of them is paid for. The alternative is a model call per row: one crash a campaign
    # cannot attribute condemns every check that campaign covered, which for a Crucible component is
    # its whole property set.
    fetched = await asyncio.gather(*[_evidence(r) for r in bad])
    collapsed: dict[Hashable, tuple[RuleVerdict, list[RuleEvidence]]] = {}
    also: dict[Hashable, list[RuleName]] = {}
    for rule, evidence in zip(bad, fetched):
        if evidence is None:
            continue
        key = finding_key(rule, evidence)
        if key in collapsed:
            also[key].append(rule.name)
        else:
            collapsed[key], also[key] = (rule, evidence), []

    async def _one(
        rule: RuleVerdict, evidence: list[RuleEvidence], covers: list[RuleName],
    ) -> Finding | None:
        try:
            props = props_by_ref.get(rule.ref, [])
            # A rule's properties may share a group (some may be in none); dedupe by slug, first-seen order.
            rule_groups = list({g.slug: g for p in props if (g := group_by_key.get(p.key)) is not None}.values())
            user = policy.prompt.bind({
                "contract_name": contract_name, "rule_name": rule.name, "properties": props,
                "groups": rule_groups, "evidence": evidence, "also_covers": covers,
            }).render_to(load_jinja_template)
            draft = await bound.ainvoke([SystemMessage(system), HumanMessage(user)])
            assert isinstance(draft, FindingDraft)
            return _compose(rule, draft, evidence, rule_groups)
        except Exception:  # noqa: BLE001 — one finding failing must never fail the report
            _log.warning("report: finding synthesis failed for rule %r; skipping", rule.name, exc_info=True)
            return None

    sem = asyncio.Semaphore(_MAX_CONCURRENT_FINDING_CALLS)

    async def _bounded(key: Hashable) -> Finding | None:
        async with sem:
            rule, evidence = collapsed[key]
            return await _one(rule, evidence, also[key])

    findings = await asyncio.gather(*[_bounded(k) for k in collapsed])
    return [f for f in findings if f is not None]


def _compose(
    rule: RuleVerdict,
    draft: FindingDraft,
    evidence: list[RuleEvidence],
    groups: list[PropertyGroup],
) -> Finding:
    risk = assess(draft)
    return Finding(
        title=draft.title,
        severity=risk.severity,
        content=IssueContent(
            **{f: getattr(draft, f) for f in AuthoredContent.model_fields},
            proof_of_concept=proof_of_concept(evidence),
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
