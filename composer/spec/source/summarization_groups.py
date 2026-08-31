"""Summarization-footprint populator — one splitting policy over the general
verification-group substrate (:mod:`composer.spec.source.verification_groups`).

The problem it solves: a single spec has one global ``methods{}`` block, so every
rule is verified under the *intersection* of what all rules need precise — the
least-aggressive summarization possible. A function can be summarized only if *no*
rule needs it exact, so one bitmap-heavy rule forces the whole spec to keep the
bitmap precise, and every unrelated rule pays that cost. That is the usual source
of the 2h timeout.

This policy breaks that coupling. Given, per rule, the set of functions that rule
needs kept *precise* (its "footprint"), it groups rules by footprint and gives each
group its own spec that summarizes every hostile function EXCEPT the ones that
group's rules need exact. So the bitmap rules keep the bitmap precise while the
accounting rules summarize it, and vice versa — each group runs a far more
tractable problem than the monolith.

Mechanism (no CVL parsing): the author writes the base spec with rule bodies and
any ghost / CVL-function definitions the summaries use, but leaves the hostile
summaries OUT of it, providing each as a separate ``methods`` entry keyed by its
target function. A group's spec is the base spec with an appended ``methods{}``
block holding only the entries the group can afford. CVL merges ``methods`` blocks,
so appending is sound; a ghost left unused when its summary is dropped is harmless.
"""

from collections import defaultdict

from composer.spec.source.verification_groups import (
    VerificationGroup,
    cap_groups,
    single_group,
)
from composer.spec.util import string_hash


def append_summaries(base_spec: str, summary_entries: dict[str, str], needs_exact: frozenset[str]) -> str:
    """The base spec plus a ``methods{}`` block of every summary entry whose target
    function is NOT in ``needs_exact`` — i.e. everything this group can summarize.
    Entries are emitted in a stable (sorted-by-target) order. When nothing is left to
    summarize, the base spec is returned unchanged."""
    kept = [text for func, text in sorted(summary_entries.items()) if func not in needs_exact]
    if not kept:
        return base_spec
    block = "methods {\n" + "\n".join(f"    {entry}" for entry in kept) + "\n}\n"
    return base_spec.rstrip() + "\n\n// --- summarization group: functions summarized here ---\n" + block


def _group_name(needs_exact: frozenset[str]) -> str:
    """A stable, readable group name from the functions it keeps precise."""
    base = "exact_" + ("_".join(sorted(needs_exact)) if needs_exact else "none")
    # Bound the length (names become conf/spec file stems); keep it collision-stable
    # by appending a short digest of the full set when truncated.
    if len(base) <= 60:
        return base
    # Truncate, but keep the name stable+collision-resistant with a content digest
    # (string_hash is deterministic across runs, unlike hash() under PYTHONHASHSEED,
    # so conf/spec names and cache keys stay reproducible).
    return base[:48] + "_" + string_hash("|".join(sorted(needs_exact)))[:8]


def build_summarization_groups(
    base_spec: str,
    *,
    summary_entries: dict[str, str],
    footprints: dict[str, frozenset[str]],
    all_rules: list[str],
    cap: int,
) -> list[VerificationGroup]:
    """Partition ``all_rules`` into verification groups by summarization footprint.

    Rules sharing a footprint (the set of functions they need precise) share a group;
    each group's spec summarizes every hostile function outside its footprint. The
    result is bounded to ``cap`` groups via :func:`cap_groups`, whose greedy merge —
    here regenerating the merged group's spec for the unioned footprint — keeps the
    most-similar footprints together so each merge sheds the least summarization.

    A rule with no declared footprint needs nothing precise, so it joins the maximal-
    summarization group (footprint ``frozenset()``). With no footprints and no summary
    entries this degenerates to the single shared spec — the current behavior.
    """
    if not summary_entries and not any(footprints.values()):
        # Nothing to vary: one group over the shared spec unchanged.
        return single_group(all_rules)

    by_footprint: dict[frozenset[str], set[str]] = defaultdict(set)
    for rule in all_rules:
        by_footprint[footprints.get(rule, frozenset())].add(rule)

    def make_group(needs_exact: frozenset[str], rules: set[str]) -> VerificationGroup:
        return VerificationGroup(
            name=_group_name(needs_exact),
            owned_rules=frozenset(rules),
            spec_contents=append_summaries(base_spec, summary_entries, needs_exact),
            footprint=needs_exact,
        )

    groups = [make_group(fp, rules) for fp, rules in by_footprint.items()]

    def merge_pair(a: VerificationGroup, b: VerificationGroup) -> VerificationGroup:
        # The merged group must keep BOTH footprints precise; regenerate its spec so
        # the summaries it drops match the unioned footprint.
        return make_group(a.footprint | b.footprint, set(a.owned_rules | b.owned_rules))

    return cap_groups(groups, cap, merge_pair=merge_pair)
