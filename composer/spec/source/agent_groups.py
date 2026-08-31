"""Agent-declared verification groups over properties.

The transparent, agent-controlled splitting policy. Rather than infer a partition,
the CVL author *declares* it: a set of groups, each naming the properties it
verifies, the hostile functions it keeps precise, and any conf overrides. This
rides the structure autoprover already has — the property -> rule mapping and its
coverage guarantee (every non-skipped property is mapped to rules) — so a group is
expressed in the agent's native unit (properties), and coverage composes: every
property lands in exactly one group, every group's owned rules are verified
(per-group completion), therefore every property is covered.

Groups here are NOT opaque. The agent names them, sees their membership, and
controls each group's precision (`keep_precise`) and configuration
(`conf_overlay`). The machinery only expands properties to rules, enforces a
disjoint rule partition, validates coverage, and bounds the count to the cap. The
per-group spec is the shared base spec plus a `methods{}` block of every hostile
summary the group can afford (:func:`composer.spec.source.summarization_groups.append_summaries`).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from composer.spec.cvl_generation import PropertyRuleMapping
from composer.spec.source.summarization_groups import append_summaries
from composer.spec.source.verification_groups import VerificationGroup, cap_groups


@dataclass(frozen=True)
class GroupDeclaration:
    """One agent-declared verification group, expressed over properties."""
    #: Agent-chosen, human-readable identifier (used in conf/spec names and results).
    name: str
    #: Property titles this group verifies. Expanded to rules via the property->rule
    #: mapping; every non-skipped property must appear in exactly one declaration.
    properties: frozenset[str]
    #: Hostile functions this group keeps PRECISE (unsummarized). Everything else in
    #: the summary palette is summarized in this group's spec.
    keep_precise: frozenset[str] = frozenset()
    #: Per-group conf overlay (e.g. {"loop_iter": 2, "global_timeout": 4000}). The
    #: non-summarization split axis — isolation, timeouts, loop bounds, links.
    conf_overlay: Mapping[str, object] = field(default_factory=dict)


def validate_declared_coverage(
    declarations: list[GroupDeclaration],
    *,
    all_properties: set[str],
    skipped: set[str],
) -> str | None:
    """Whether the declared groups cover the property space exactly once.

    Reuses autoprover's coverage contract: every non-skipped property must be
    assigned to exactly one group; no unknown or skipped property may be assigned.
    Returns None when valid, else one message enumerating every problem — the shape
    an agent tool hands back so the author can fix its declaration."""
    assigned: list[str] = [p for d in declarations for p in d.properties]
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


def build_declared_groups(
    base_spec: str,
    *,
    declarations: list[GroupDeclaration],
    property_rules: dict[str, list[str]],
    summary_palette: dict[str, str],
    cap: int,
) -> list[VerificationGroup]:
    """Expand agent-declared property groups into runnable :class:`VerificationGroup`s.

    Each declaration's owned rules are the union of its properties' rules, made a
    disjoint partition by first-declaration-wins (a rule shared across properties in
    different groups is owned — and thus authoritatively verified — by the first,
    keeping the ``merge_group_results`` / per-group-completion invariant that every
    rule has exactly one owner). Each group's spec summarizes every palette function
    outside its ``keep_precise`` set. The result is bounded to ``cap`` via the greedy
    footprint merge (regenerating the merged spec for the unioned precise set).

    Coverage should be checked first with :func:`validate_declared_coverage`; this
    function assumes a valid declaration and only enforces the rule partition."""
    claimed: set[str] = set()
    groups: list[VerificationGroup] = []
    for d in declarations:
        owned = {r for p in d.properties for r in property_rules.get(p, [])}
        owned -= claimed  # first-declaration-wins: keep the rule partition disjoint
        claimed |= owned
        groups.append(
            VerificationGroup(
                name=d.name,
                owned_rules=frozenset(owned),
                spec_contents=append_summaries(base_spec, summary_palette, d.keep_precise),
                footprint=d.keep_precise,
                conf_overlay=d.conf_overlay,
            )
        )

    def merge_pair(a: VerificationGroup, b: VerificationGroup) -> VerificationGroup:
        precise = a.footprint | b.footprint
        return VerificationGroup(
            name=f"{a.name}+{b.name}"[:60],
            owned_rules=a.owned_rules | b.owned_rules,
            spec_contents=append_summaries(base_spec, summary_palette, precise),
            footprint=precise,
            conf_overlay={**a.conf_overlay, **b.conf_overlay},
        )

    return cap_groups(groups, cap, merge_pair=merge_pair)


# --- Agent-facing declaration (the tool input / state shape) ----------------


class VerificationGroupSpec(BaseModel):
    """One verification group as the CVL author declares it — the transparent,
    agent-controlled unit. Carries its own property->rule mapping so the rules are
    known during authoring (the publish-time mapping is their union), the functions
    it keeps precise, and any conf overrides."""
    name: str = Field(description="A short, unique, human-readable name for this group (used in run/spec names).")
    property_rules: list[PropertyRuleMapping] = Field(
        description="The properties this group verifies and, for each, the rule/invariant names in "
        "the spec that verify it. A group may cover multiple properties. Across all groups every "
        "non-skipped property must appear in exactly one group."
    )
    keep_precise: list[str] = Field(
        default_factory=list,
        description="Hostile functions to keep PRECISE (unsummarized) in this group — the ones this "
        "group's rules genuinely need exact. Every other function in the summary palette is "
        "summarized here.",
    )
    conf_overlay: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional per-group prover-config overrides for the non-summarization split "
        "reasons, e.g. {\"loop_iter\": 2, \"global_timeout\": 4000}.",
    )


def property_rules_of(specs: list[VerificationGroupSpec]) -> dict[str, list[str]]:
    """The combined property->rule mapping across all declared groups — the value
    finalized into ``property_rules`` at publish."""
    return {str(m.property_title): [str(r) for r in m.rules] for spec in specs for m in spec.property_rules}


def coverage_error(
    specs: list[VerificationGroupSpec], *, all_properties: set[str], skipped: set[str]
) -> str | None:
    """Validate declared-group coverage over the property space (see
    :func:`validate_declared_coverage`), reading properties from each group's mapping."""
    decls = [
        GroupDeclaration(name=s.name, properties=frozenset(m.property_title for m in s.property_rules))
        for s in specs
    ]
    return validate_declared_coverage(decls, all_properties=all_properties, skipped=skipped)


def groups_from_specs(
    base_spec: str,
    specs: list[VerificationGroupSpec],
    *,
    summary_palette: dict[str, str],
    cap: int,
) -> list[VerificationGroup]:
    """Expand the agent's declared group specs into runnable groups (coverage assumed
    already validated). Convenience over :func:`build_declared_groups` that unpacks the
    embedded property->rule mappings."""
    declarations = [
        GroupDeclaration(
            name=s.name,
            properties=frozenset(m.property_title for m in s.property_rules),
            keep_precise=frozenset(s.keep_precise),
            conf_overlay=s.conf_overlay,
        )
        for s in specs
    ]
    return build_declared_groups(
        base_spec,
        declarations=declarations,
        property_rules=property_rules_of(specs),
        summary_palette=summary_palette,
        cap=cap,
    )


def render_group_plan_for_judge(
    specs: list["VerificationGroupSpec"], summary_palette: dict[str, str]
) -> str | None:
    """A judge-facing note describing the verification-group plan, or ``None`` when
    no groups are declared.

    The feedback judge reviews the *base* spec (``curr_spec``), which deliberately
    leaves the hostile summaries OUT of its ``methods{}`` block — each group installs
    its own summaries at prover time via :func:`append_summaries`. Without this note
    the judge sees hostile functions used-but-not-summarized and false-flags them as
    unsound/HAVOCing. The note makes the per-group install concrete: it shows the
    shared summary palette and, for each group, exactly which palette entries that
    group installs (palette minus ``keep_precise``) and which it keeps precise — so
    the judge evaluates the spec as it is actually verified, not as a monolith."""
    if not specs:
        return None
    lines: list[str] = [
        "// ============================================================================",
        "// Verification-group plan (informational — NOT part of the base spec above)",
        "// ============================================================================",
        "// This spec is NOT verified as a monolith. It is split into parallel prover",
        "// runs ('verification groups'), each with its OWN methods{} block installing",
        "// sound summaries from the shared palette below, keeping precise only what",
        "// that group's rules need exact. A hostile function that appears",
        "// un-summarized in the base spec above IS summarized in every group that does",
        "// not list it under 'keeps precise' — treat those palette entries as installed",
        "// (not HAVOCing) when judging soundness and coverage.",
        "//",
    ]
    if summary_palette:
        lines.append("// Summary palette (sound summaries applied per-group):")
        for func in sorted(summary_palette):
            lines.append(f"//   {func}: {summary_palette[func].strip()}")
        lines.append("//")
    for s in specs:
        props = [str(m.property_title) for m in s.property_rules]
        rules = [str(r) for m in s.property_rules for r in m.rules]
        precise = frozenset(s.keep_precise)
        installs = [f for f in sorted(summary_palette) if f not in precise]
        lines.append(f"// Group \"{s.name}\":")
        lines.append(f"//   properties: {', '.join(props) if props else '(none)'}")
        lines.append(f"//   rules: {', '.join(rules) if rules else '(none)'}")
        lines.append(f"//   keeps precise: {', '.join(sorted(precise)) if precise else '(none)'}")
        lines.append(
            f"//   installs summaries for: {', '.join(installs) if installs else '(none)'}"
        )
        if s.conf_overlay:
            lines.append(f"//   conf overrides: {s.conf_overlay}")
        lines.append("//")
    return "\n".join(lines)
