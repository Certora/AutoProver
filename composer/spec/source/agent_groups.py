"""Agent-declared verification groups over properties.

The transparent, agent-controlled splitting policy. Rather than infer a partition,
the CVL author *declares* it: a set of groups, each naming the properties it
verifies, the summaries it installs (per function), and any conf overrides. This
rides the structure autoprover already has — the property -> rule mapping and its
coverage guarantee (every non-skipped property is mapped to rules) — so a group is
expressed in the agent's native unit (properties), and coverage composes: every
property lands in exactly one group, every group's owned rules are verified
(per-group completion), therefore every property is covered.

Groups here are NOT opaque. The agent names them, sees their membership, and
controls each group's summaries (`summaries`). (The substrate group also carries a
per-group conf overlay, but the agent-facing tool does not expose it yet — a
per-group prover-config knob is new, unvalidated, and unreviewed by the judge, so it
is deferred to a follow-up with proper guardrails.)
The machinery only expands properties to rules, enforces a disjoint rule partition,
validates coverage, and bounds the count to the cap. The per-group spec is the
shared base spec plus a `methods{}` block of the summaries that group declared
(:func:`composer.spec.source.verification_groups.append_summaries`); a function a
group does not summarize is verified precise there.
"""

from pydantic import BaseModel, Field

from composer.spec.cvl_generation import PropertyRuleMapping
from composer.spec.source.verification_groups import (
    VerificationGroup, append_summaries, cap_groups,
)


def validate_declared_coverage(
    specs: list["VerificationGroupSpec"],
    *,
    all_properties: set[str],
    skipped: set[str],
) -> str | None:
    """Whether the declared groups cover the property space exactly once.

    Reuses autoprover's coverage contract: every non-skipped property must be
    assigned to exactly one group; no unknown or skipped property may be assigned.
    Returns None when valid, else one message enumerating every problem — the shape
    an agent tool hands back so the author can fix its declaration."""
    assigned: list[str] = [str(m.property_title) for s in specs for m in s.property_rules]
    seen: set[str] = set()
    duplicated: set[str] = set()
    for p in assigned:
        (duplicated if p in seen else seen).add(p)
    required = all_properties - skipped
    problems: list[str] = []
    if duplicated:
        problems.append(f"properties assigned to more than one group: {sorted(duplicated)}")
    if missing := required - seen:
        problems.append(f"non-skipped properties assigned to no group: {sorted(missing)}")
    if unknown := seen - all_properties:
        problems.append(f"unknown property titles: {sorted(unknown)}")
    if skipped_assigned := seen & skipped:
        problems.append(f"skipped properties should not be assigned to a group: {sorted(skipped_assigned)}")
    return "; ".join(problems) if problems else None


# --- Agent-facing declaration (the tool input / state shape) ----------------


class VerificationGroupSpec(BaseModel):
    """One verification group as the CVL author declares it — the transparent,
    agent-controlled unit. Carries its own property->rule mapping so the rules are
    known during authoring (the publish-time mapping is their union) and the functions
    it keeps precise. (Per-group conf overrides are supported by the substrate but not
    exposed here yet — see the module docstring.)"""
    name: str = Field(description="A short, unique, human-readable name for this group (used in run/spec names).")
    property_rules: list[PropertyRuleMapping] = Field(
        description="The properties this group verifies and, for each, the rule/invariant names in "
        "the spec that verify it. A group may cover multiple properties. Across all groups every "
        "non-skipped property must appear in exactly one group."
    )
    summaries: dict[str, str] = Field(
        default_factory=dict,
        description="The summaries THIS group installs: each key a hostile function, each value the full "
        "CVL methods{} entry to summarize it here (e.g. \"function C.f(uint) external => NONDET;\", a ghost "
        "mirror, a model). A function absent from this map is verified as the base spec has it in this "
        "group — precise only if the base spec (incl. its imports) does not already summarize it. The same "
        "function may be summarized differently in different groups — choose, per group, "
        "the weakest summary sound for that group's rules, and reuse the same entry across groups where "
        "it is sound (consistency).",
    )


def owned_rules_per_group(specs: list[VerificationGroupSpec]) -> list[frozenset[str]]:
    """Each spec's owned rules under first-declaration-wins, aligned to ``specs`` by index.

    A group's owned rules are the union of its properties' rules; a rule declared by more than
    one group is owned by the FIRST that declares it, so the partition stays disjoint (the
    ``merge_group_results`` / per-group-completion invariant that every rule has exactly one
    owner)."""
    claimed: set[str] = set()
    owned_per: list[frozenset[str]] = []
    for s in specs:
        owned = {str(r) for m in s.property_rules for r in m.rules} - claimed
        claimed |= owned
        owned_per.append(frozenset(owned))
    return owned_per


def over_cap_message(specs: list[VerificationGroupSpec], cap: int) -> str | None:
    """A rejection message when the agent declared MORE groups than the cap, else ``None``.

    Each group is a separate prover run, so the count is bounded. Rather than silently auto-merge the
    declaration (which would undo the split the agent deliberately chose), the declaring tool rejects an
    over-cap declaration and asks the agent to refactor — and SUGGESTS a concrete valid merge (the greedy,
    most-agreeing-summaries merge :func:`cap_groups` computes), which the agent can adopt or improve."""
    if len(specs) <= cap:
        return None
    # A lightweight sim of the partition, run through cap_groups to name one valid merge to suggest.
    sim = [
        VerificationGroup(name=s.name, owned_rules=owned, summaries=dict(s.summaries))
        for s, owned in zip(specs, owned_rules_per_group(specs))
    ]
    suggested = "; ".join(g.name for g in cap_groups(sim, cap))
    return (
        f"You declared {len(specs)} verification groups but at most {cap} are allowed — each group is a "
        f"separate prover run (raise the limit via AUTOPROVER_MAX_VERIFICATION_GROUPS). Merge groups until "
        f"there are at most {cap}: combine the ones whose rules can share the same summaries — a merged "
        f"group keeps a summary only where both groups agree, else that function drops to precise. One valid "
        f"merge to adopt or improve: {suggested}."
    )


def groups_from_specs(
    base_spec: str,
    specs: list[VerificationGroupSpec],
    *,
    cap: int,
) -> list[VerificationGroup]:
    """Expand the agent's declared group specs into runnable :class:`VerificationGroup`s
    (coverage assumed already validated with :func:`validate_declared_coverage`).

    Owned rules are partitioned first-declaration-wins (:func:`owned_rules_per_group`). Each
    group's spec installs the summaries it declared (:func:`append_summaries`); a function it
    does not summarize is verified precise. The declaration must already be within ``cap`` — the
    declaring tool rejects an over-cap declaration (:func:`over_cap_message`) rather than merging
    — so this asserts the bound instead of capping."""
    assert len(specs) <= cap, (
        f"{len(specs)} groups exceeds cap {cap}; over-cap declarations are rejected at declare time"
    )
    return [
        VerificationGroup(
            name=s.name,
            owned_rules=owned,
            spec_contents=append_summaries(base_spec, s.summaries),
            summaries=dict(s.summaries),
            # conf_overlay left at its substrate default ({}): the agent tool does not expose it yet.
        )
        for s, owned in zip(specs, owned_rules_per_group(specs))
    ]


def render_group_plan_for_judge(specs: list["VerificationGroupSpec"]) -> str | None:
    """A judge-facing note describing the verification-group plan, or ``None`` when
    no groups are declared.

    The feedback judge reviews the *base* spec (``curr_spec``), which deliberately
    leaves the hostile summaries OUT of its ``methods{}`` block — each group installs
    its own summaries at prover time via :func:`append_summaries`. Without this note
    the judge sees hostile functions used-but-not-summarized and false-flags them as
    unsound/HAVOCing. The note makes each group's install concrete: its properties,
    rules, and exactly which functions it summarizes (with the summary text) — so the
    judge evaluates the spec as it is actually verified, not as a monolith. A function
    a group does NOT list is verified as the base spec has it there — precise unless the
    base spec itself summarizes it."""
    if not specs:
        return None
    lines: list[str] = [
        "// ============================================================================",
        "// Verification-group plan (informational — NOT part of the base spec above)",
        "// ============================================================================",
        "// This spec is NOT verified as a monolith. It is split into parallel prover",
        "// runs ('verification groups'), each with its OWN methods{} block installing the",
        "// summaries listed below. A hostile function that appears un-summarized in the base",
        "// spec above IS summarized in every group that lists it here — treat those as",
        "// installed (not HAVOCing) when judging soundness and coverage; a function a group",
        "// does not list is verified as the base spec above has it (precise unless the base",
        "// spec above already summarizes it). A summary a group DOES list for a function the base",
        "// spec above already summarizes is a more-specific override (exact beats wildcard), so that",
        "// group verifies under the group's entry, not the base's.",
        "//",
    ]
    for s in specs:
        props = [str(m.property_title) for m in s.property_rules]
        rules = [str(r) for m in s.property_rules for r in m.rules]
        lines.append(f"// Group \"{s.name}\":")
        lines.append(f"//   properties: {', '.join(props) if props else '(none)'}")
        lines.append(f"//   rules: {', '.join(rules) if rules else '(none)'}")
        if s.summaries:
            lines.append("//   installs summaries:")
            for func in sorted(s.summaries):
                lines.append(f"//     {func}: {s.summaries[func].strip()}")
        else:
            lines.append("//   installs summaries: (none — all functions precise)")
        # No per-group conf is shown: the agent-facing group carries none yet (deferred), and even
        # the substrate's conf overlay would be moot here — the judge is handed the spec, not the
        # .conf, so it has neither a base to compare against nor a mandate to review conf soundness.
        # If groups gain agent-set conf AND the judge starts reviewing .conf files, list each
        # group's conf diff here.
        lines.append("//")
    return "\n".join(lines)
