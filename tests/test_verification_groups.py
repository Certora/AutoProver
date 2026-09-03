"""Unit tests for the general verification-group substrate (Layer 1).

Pure-logic tests: the group cap, the cap-driven greedy merge, and result
recombination. No prover or LLM involved.
"""

import pytest

from composer.prover.ptypes import RulePath
from composer.spec.source.verification_groups import (
    DEFAULT_MAX_VERIFICATION_GROUPS,
    MAX_VERIFICATION_GROUPS_ENV,
    VerificationGroup,
    cap_groups,
    merge_group_results,
    plan_verification_groups,
    resolved_max_groups,
    single_group,
)


def _g(name, rules, summaries=None):
    return VerificationGroup(name=name, owned_rules=frozenset(rules), summaries=summaries or {})


# --- single_group / behavior-preserving default -----------------------------


def test_single_group_owns_all_rules():
    groups = single_group(["a", "b", "c"])
    assert len(groups) == 1
    assert groups[0].owned_rules == {"a", "b", "c"}
    assert groups[0].spec_contents is None
    assert groups[0].conf_overlay == {}


def test_default_planner_is_single_group():
    # Until a splitting policy is wired, the planner reproduces the current
    # one-spec/one-run model: one group owning every rule.
    groups = plan_verification_groups(["a", "b", "c"])
    assert len(groups) == 1
    assert groups[0].owned_rules == {"a", "b", "c"}
    assert groups[0].spec_contents is None


# --- resolved_max_groups ----------------------------------------------------


def test_max_groups_default_when_unset(monkeypatch):
    monkeypatch.delenv(MAX_VERIFICATION_GROUPS_ENV, raising=False)
    assert resolved_max_groups() == DEFAULT_MAX_VERIFICATION_GROUPS


def test_max_groups_env_override(monkeypatch):
    monkeypatch.setenv(MAX_VERIFICATION_GROUPS_ENV, "2")
    assert resolved_max_groups() == 2


@pytest.mark.parametrize("bad", ["0", "-3", "notanint", ""])
def test_max_groups_invalid_falls_back(monkeypatch, bad):
    monkeypatch.setenv(MAX_VERIFICATION_GROUPS_ENV, bad)
    assert resolved_max_groups() == DEFAULT_MAX_VERIFICATION_GROUPS


# --- cap_groups -------------------------------------------------------------


def test_cap_noop_when_within_cap():
    groups = [_g("x", ["a"]), _g("y", ["b"])]
    capped = cap_groups(groups, cap=4)
    assert capped == groups


def test_cap_to_one_collapses_everything():
    groups = [_g("x", ["a"], {"f1": "S1"}), _g("y", ["b"], {"f2": "S2"}), _g("z", ["c"], {"f3": "S3"})]
    capped = cap_groups(groups, cap=1)
    assert len(capped) == 1
    # The single surviving group owns every rule; the disjoint summaries all disagree,
    # so every function drops to precise — i.e. the monolithic run.
    assert capped[0].owned_rules == {"a", "b", "c"}
    assert capped[0].summaries == {}


def test_cap_merges_most_agreeing_summaries_first():
    # x and y summarize f1 IDENTICALLY; z summarizes a disjoint f9. Capping 3->2 must merge
    # x+y (cheapest: they agree on f1, losing nothing) and leave z alone.
    x = _g("x", ["a"], {"f1": "S1"})
    y = _g("y", ["b"], {"f1": "S1"})
    z = _g("z", ["c"], {"f9": "S9"})
    capped = cap_groups([x, y, z], cap=2)
    assert len(capped) == 2
    by_rules = {frozenset(g.owned_rules): g for g in capped}
    assert frozenset({"a", "b"}) in by_rules  # x+y merged
    assert frozenset({"c"}) in by_rules  # z untouched
    assert by_rules[frozenset({"a", "b"})].summaries == {"f1": "S1"}


def test_merge_drops_disagreed_summaries_to_precise():
    # Both summarize f1 but INCOMPARABLY differently -> the merged group keeps neither
    # (drops f1 to precise); no summary-strength ordering is assumed.
    x = _g("x", ["a"], {"f1": "monotone"})
    y = _g("y", ["b"], {"f1": "injective"})
    capped = cap_groups([x, y], cap=1)
    assert capped[0].summaries == {}


def test_cap_below_one_rejected():
    with pytest.raises(ValueError):
        cap_groups([_g("x", ["a"])], cap=0)


def test_cap_partition_disjoint_after_merges():
    groups = [_g(str(i), [f"r{i}"], {f"f{i % 2}": f"S{i % 2}"}) for i in range(6)]
    capped = cap_groups(groups, cap=2)
    assert len(capped) == 2
    owned = [r for g in capped for r in g.owned_rules]
    assert sorted(owned) == sorted(f"r{i}" for i in range(6))  # every rule kept exactly once


# --- merge_group_results ----------------------------------------------------


def test_merge_results_keeps_only_owned_rules():
    ga = _g("A", ["r1", "r2"])
    gb = _g("B", ["r3"])
    # Group A's run also instantiated r3 (referenced), but under A's precision it
    # must be ignored; r3's authoritative verdict comes from B.
    res_a = {RulePath(rule="r1"): "VERIFIED", RulePath(rule="r2"): "VIOLATED", RulePath(rule="r3"): "TIMEOUT"}
    res_b = {RulePath(rule="r3"): "VERIFIED"}
    combined = merge_group_results([(ga, res_a), (gb, res_b)])
    assert combined[RulePath(rule="r1")] == "VERIFIED"
    assert combined[RulePath(rule="r2")] == "VIOLATED"
    assert combined[RulePath(rule="r3")] == "VERIFIED"  # from B, not A's TIMEOUT


def test_merge_results_single_group_is_passthrough():
    g = _g("all", ["r1", "r2"])
    res = {RulePath(rule="r1"): "VERIFIED", RulePath(rule="r2"): "VIOLATED"}
    assert merge_group_results([(g, res)]) == res
