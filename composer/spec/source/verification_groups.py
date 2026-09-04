"""General-purpose partitioning of a spec's rules into independent Certora
verification runs ("verification groups").

A *verification group* is a subset of a spec's rules verified in its own prover
run, under its own spec / conf configuration. Groups exist so that different
rules can run under verification setups a single run cannot express. The
hard driver is CVL itself: every imported ``methods{}`` block is merged globally
within one spec, so giving different rules different summarization *requires*
splitting them into different spec files (hence different confs, hence different
runs). But splitting is deliberately not summarization-specific — a group may
equally carry its own ``loop_iter``, link/dispatch setup, ``global_timeout`` or
``prover_args``, or exist only to isolate one expensive rule.

This module is policy-neutral. It owns:
  * the group model (:class:`VerificationGroup`),
  * the group-count cap and its env override,
  * the cap-driven greedy merge (:func:`cap_groups`), and
  * result aggregation across groups (:func:`merge_group_results`).

It does NOT decide *why* rules are split — which rules share a group, and each
group's spec/conf configuration. That is a populating policy's job (e.g. the
summarization-footprint clustering), which constructs the groups this module
then bounds and whose results it recombines. With a single group covering every
rule, this machinery is a behavior-preserving pass-through of the current
one-spec/one-run model.
"""

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from composer.prover.ptypes import RulePath, StatusCodes

_logger = logging.getLogger("composer.prover")


# A group count above this is merged down (see cap_groups). Each group is a
# separate prover run, so the cap bounds run fan-out (cost / parallelism) and the
# worst case of one-group-per-rule; at 1 the whole spec runs as a single group,
# i.e. exactly today's behavior. Overridable per run via the env var, mirroring
# the AUTOPROVER_* prover-config knobs.
DEFAULT_MAX_VERIFICATION_GROUPS = 4
MAX_VERIFICATION_GROUPS_ENV = "AUTOPROVER_MAX_VERIFICATION_GROUPS"


def resolved_max_groups() -> int:
    """The verification-group cap: ``DEFAULT_MAX_VERIFICATION_GROUPS``, or the
    integer value of ``$AUTOPROVER_MAX_VERIFICATION_GROUPS`` when set. Values
    below 1, and non-integers, are ignored with a warning (a cap of 0 groups is
    meaningless)."""
    raw = os.environ.get(MAX_VERIFICATION_GROUPS_ENV)
    if raw is None:
        return DEFAULT_MAX_VERIFICATION_GROUPS
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("Ignoring non-integer %s=%r", MAX_VERIFICATION_GROUPS_ENV, raw)
        return DEFAULT_MAX_VERIFICATION_GROUPS
    if value < 1:
        _logger.warning("Ignoring %s=%r (must be >= 1)", MAX_VERIFICATION_GROUPS_ENV, raw)
        return DEFAULT_MAX_VERIFICATION_GROUPS
    return value


@dataclass(frozen=True)
class VerificationGroup:
    """One independent verification run over a subset of a spec's rules.

    Groups partition the rule set: every rule is *owned* by exactly one group,
    and that group's run is authoritative for its verdict (:func:`merge_group_results`).
    A run may still instantiate more rules than it owns — e.g. a spec whose
    invariants reference each other — but only the owned rules' verdicts are kept.
    """

    #: Stable identifier, used in conf/spec names and logs.
    name: str
    #: Rules whose verdict is taken from this group's run. Partition-disjoint
    #: across groups.
    owned_rules: frozenset[str]
    #: Per-group spec text. ``None`` means "use the shared spec unchanged" — the
    #: single-group / behavior-preserving case. A populating policy sets this when
    #: the group needs a distinct spec (e.g. a different ``methods{}`` block).
    spec_contents: str | None = None
    #: Per-group conf overlay merged onto the base config for this group's run
    #: (e.g. ``{"loop_iter": 2}``). Empty means no overlay.
    conf_overlay: Mapping[str, object] = field(default_factory=dict)
    #: The summaries this group installs: function -> the ``methods{}`` entry (opaque
    #: text — NONDET, a monotone / injective ghost, a model, …). A function absent here
    #: is verified PRECISE (unsummarized) in this group. Drives ``spec_contents`` and the
    #: cap merge (:func:`cap_groups`): the cheapest merges are the pairs that agree on the
    #: most summaries; on any disagreement a function drops to precise (:func:`merge_summaries`).
    summaries: Mapping[str, str] = field(default_factory=dict)


def append_summaries(base_spec: str, summaries: Mapping[str, str]) -> str:
    """The base spec plus a ``methods{}`` block of this group's ``summaries`` (each value a
    full methods entry), in stable sorted-by-function order. Empty summaries return the base
    spec unchanged. CVL merges ``methods`` blocks, so appending is sound; a ghost the base
    spec defines but no installed summary uses is harmless."""
    if not summaries:
        return base_spec
    block = "methods {\n" + "\n".join(f"    {summaries[f]}" for f in sorted(summaries)) + "\n}\n"
    return base_spec.rstrip() + "\n\n// --- verification group: summaries installed here ---\n" + block


def merge_summaries(a: Mapping[str, str], b: Mapping[str, str]) -> dict[str, str]:
    """The summaries two groups can BOTH keep when merged into one run: a function is kept only
    where both groups summarize it identically; any disagreement (different text, or only one
    group summarizes it) drops it to PRECISE. Order-free and always sound — dropping a summary
    only adds precision — so it assumes no summary-strength ordering (summaries are not generally
    comparable: e.g. 'monotone' and 'injective' are incomparable)."""
    return {f: a[f] for f in a.keys() & b.keys() if a[f] == b[f]}


def single_group(
    all_rules: Sequence[str],
    *,
    name: str = "all",
    spec_contents: str | None = None,
) -> list[VerificationGroup]:
    """The trivial partition: one group owning every rule, no per-group spec/conf.

    This is the behavior-preserving default — routing a run through
    ``single_group`` reproduces the current one-spec/one-run model exactly."""
    return [VerificationGroup(name=name, owned_rules=frozenset(all_rules), spec_contents=spec_contents)]


def plan_verification_groups(
    all_rules: Sequence[str],
    *,
    spec_contents: str | None = None,
) -> list[VerificationGroup]:
    """Partition this run's rules into verification groups.

    The seam a splitting policy plugs into. The default — and, until a policy is
    wired, the only — partition is the trivial one: a single group owning every
    rule under the shared spec, i.e. the current one-spec/one-run behavior. A
    populating policy (e.g. summarization-footprint clustering) replaces this to
    return multiple groups with distinct specs/confs, then bounds them with
    :func:`cap_groups`; the run loop and :func:`merge_group_results` treat the
    result as N-way, so turning a policy on needs no change to callers here.
    """
    return single_group(all_rules, spec_contents=spec_contents)


def _default_merge_pair(a: VerificationGroup, b: VerificationGroup) -> VerificationGroup:
    """Combine two groups when neither carries a distinct spec: union the owned rules, keep
    the agreed summaries (:func:`merge_summaries`), keep the shared spec, and merge conf
    overlays (``b`` wins on key conflicts). A policy that gives groups distinct
    ``spec_contents`` must pass its own merge (it alone knows how to rebuild the merged spec
    from the merged summaries); this default is correct for summary-free / conf-overlay-only
    groups."""
    merged_overlay: dict[str, object] = {**a.conf_overlay, **b.conf_overlay}
    return replace(
        a,
        name=f"{a.name}+{b.name}",
        owned_rules=a.owned_rules | b.owned_rules,
        summaries=merge_summaries(a.summaries, b.summaries),
        conf_overlay=merged_overlay,
    )


def cap_groups(
    groups: Sequence[VerificationGroup],
    cap: int,
    merge_pair: Callable[[VerificationGroup, VerificationGroup], VerificationGroup] = _default_merge_pair,
) -> list[VerificationGroup]:
    """Merge ``groups`` down to at most ``cap`` groups, cheapest merges first.

    Each group is a prover run, so an unbounded partition (worst case: one group
    per rule) must be bounded. When there are more groups than ``cap``, this
    repeatedly merges the pair whose footprints are most similar — the pair whose
    merged footprint is smallest — via ``merge_pair``. Merging keeps the *union*
    of both groups' footprints precise, so it only ever removes summarization
    (adds precision): sound, but slower. Merging the most-similar pair first sheds
    the least precision per step. At ``cap == 1`` everything collapses into one
    group (today's monolith). A partition already within ``cap`` is returned as-is
    (a fresh list).
    """
    if cap < 1:
        raise ValueError(f"cap must be >= 1, got {cap}")
    remaining = list(groups)
    if len(remaining) <= cap:
        return remaining

    def merge_cost(a: VerificationGroup, b: VerificationGroup) -> int:
        # Summaries this merge would drop to precise (kept only where both agree): fewer =
        # less precision lost. Ties broken by combined rule count (prefer merging the smaller
        # groups, so no single run grows unnecessarily large).
        lost = len(a.summaries.keys() | b.summaries.keys()) - len(merge_summaries(a.summaries, b.summaries))
        return lost * 100_000 + len(a.owned_rules) + len(b.owned_rules)

    while len(remaining) > cap:
        best: tuple[int, int, int] | None = None  # (cost, i, j)
        for i in range(len(remaining)):
            for j in range(i + 1, len(remaining)):
                cost = merge_cost(remaining[i], remaining[j])
                if best is None or cost < best[0]:
                    best = (cost, i, j)
        assert best is not None  # len(remaining) > cap >= 1 => at least 2 groups
        _cost, i, j = best
        merged = merge_pair(remaining[i], remaining[j])
        # Remove the higher index first so the lower stays valid.
        remaining.pop(j)
        remaining.pop(i)
        remaining.append(merged)
    return remaining


def prune_phantom_owned_rules(
    groups: Sequence[VerificationGroup], all_rules: Sequence[str]
) -> tuple[list[VerificationGroup], set[str]]:
    """Remap each group's owned rules to those the compiled spec actually declares, and return the
    dropped "phantom" rules — names owned by a group but absent from ``all_rules`` (an agent typo, or a
    ``property_rules`` entry naming a non-existent rule).

    Left in, a phantom owned rule is never submitted (the submit set is filtered to ``all_rules``) yet
    forever counts as pending, so its group would never complete — a silent perpetual re-run. Returns the
    groups unchanged and an empty set when every owned rule is real, so the caller warns only on a genuine
    mistake."""
    actual = frozenset(all_rules)
    phantom = {r for g in groups for r in g.owned_rules} - actual
    if not phantom:
        return list(groups), set()
    return [replace(g, owned_rules=g.owned_rules & actual) for g in groups], phantom


def merge_group_results(
    per_group: Sequence[tuple[VerificationGroup, Mapping[RulePath, StatusCodes]]],
) -> dict[RulePath, StatusCodes]:
    """Recombine per-group prover verdicts into one verdict map.

    For each group, keep only the statuses of the rules that group *owns* (a run
    may instantiate more rules than it owns, but a non-owned rule's verdict there
    is under the wrong precision setup and must be ignored). The union over groups
    is the authoritative status of every rule. With one group owning all rules,
    this returns that group's map unchanged.
    """
    combined: dict[RulePath, StatusCodes] = {}
    for group, statuses in per_group:
        for path, status in statuses.items():
            if path.rule in group.owned_rules:
                combined[path] = status
    return combined
