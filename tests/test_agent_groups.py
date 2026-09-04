"""Unit tests for the agent-declared, property-level group planner (transparent policy)."""

from composer.spec.cvl_generation import PropertyRuleMapping
from composer.spec.source.agent_groups import (
    VerificationGroupSpec,
    groups_from_specs,
    over_cap_message,
    render_group_plan_for_judge,
    validate_declared_coverage,
)

BASE = "// rules\ninvariant a() true;\nghost g(uint) returns uint;\n"
SORT_SUMMARY = "function KVL.sortByKey(uint256[] l) internal returns (uint256[]) => g(0);"
EXP_SUMMARY = "function MathUtils.uncheckedExp(uint256 a, uint256 b) internal returns (uint256) => g(a);"
# property title -> rule names
PROP_RULES = {
    "P-bitmap": ["r_borrow", "r_collat"],
    "P-accounting": ["r_supply", "r_premium"],
    "P-misc": ["r_misc"],
}


def _spec(name, prop_rules, summaries=None):
    return VerificationGroupSpec(
        name=name,
        property_rules=[
            PropertyRuleMapping(property_title=p, rules=rs) for p, rs in prop_rules
        ],
        summaries=summaries or {},
    )


def _grp(name, props, summaries=None, rules=None):
    # A group over property titles, sourcing each property's rules from `rules` (PROP_RULES
    # by default); an unknown property maps to no rules.
    rules = rules if rules is not None else PROP_RULES
    return _spec(name, [(p, list(rules.get(p, []))) for p in props], summaries)


# --- coverage validation ----------------------------------------------------


def test_coverage_ok():
    decls = [_grp("g1", ["P-bitmap"]), _grp("g2", ["P-accounting", "P-misc"])]
    assert validate_declared_coverage(
        decls, all_properties=set(PROP_RULES), skipped=set()
    ) is None


def test_coverage_missing_property():
    decls = [_grp("g1", ["P-bitmap"])]
    err = validate_declared_coverage(decls, all_properties=set(PROP_RULES), skipped=set())
    assert err is not None and "no group" in err


def test_coverage_duplicate_property():
    decls = [_grp("g1", ["P-bitmap"]), _grp("g2", ["P-bitmap", "P-accounting", "P-misc"])]
    err = validate_declared_coverage(decls, all_properties=set(PROP_RULES), skipped=set())
    assert err is not None and "more than one group" in err


def test_coverage_skipped_property_not_required_nor_assignable():
    # P-misc is skipped -> need not be covered, but must not be assigned either.
    ok = [_grp("g1", ["P-bitmap"]), _grp("g2", ["P-accounting"])]
    assert validate_declared_coverage(
        ok, all_properties=set(PROP_RULES), skipped={"P-misc"}
    ) is None
    bad = [_grp("g1", ["P-bitmap"]), _grp("g2", ["P-accounting", "P-misc"])]
    err = validate_declared_coverage(bad, all_properties=set(PROP_RULES), skipped={"P-misc"})
    assert err is not None and "skipped" in err


def test_coverage_unknown_property():
    decls = [_grp("g1", ["P-bitmap"]), _grp("g2", ["P-accounting", "P-misc", "P-ghost"])]
    err = validate_declared_coverage(decls, all_properties=set(PROP_RULES), skipped=set())
    assert err is not None and "unknown" in err


# --- build ------------------------------------------------------------------


def test_build_expands_properties_to_rules_and_summaries():
    decls = [
        _grp("bitmap", ["P-bitmap"], summaries={"uncheckedExp": EXP_SUMMARY}),
        _grp("rest", ["P-accounting", "P-misc"], summaries={"sortByKey": SORT_SUMMARY}),
    ]
    groups = groups_from_specs(BASE, decls, cap=4)
    by = {g.name: g for g in groups}
    # bitmap group installs the uncheckedExp summary and keeps sortByKey precise (not installed).
    assert by["bitmap"].owned_rules == {"r_borrow", "r_collat"}
    assert "KVL.sortByKey" not in by["bitmap"].spec_contents
    assert "MathUtils.uncheckedExp" in by["bitmap"].spec_contents
    # rest group is the mirror image and owns the other properties' rules.
    assert by["rest"].owned_rules == {"r_supply", "r_premium", "r_misc"}
    assert "MathUtils.uncheckedExp" not in by["rest"].spec_contents
    assert "KVL.sortByKey" in by["rest"].spec_contents


def test_build_leaves_conf_overlay_at_substrate_default():
    # The agent tool does not expose conf_overlay; built groups carry the substrate default ({}).
    decls = [_grp("g1", ["P-bitmap"]), _grp("g2", ["P-accounting", "P-misc"])]
    groups = groups_from_specs(BASE, decls, cap=4)
    assert all(g.conf_overlay == {} for g in groups)


def test_build_rule_partition_first_declaration_wins():
    # A rule shared by two properties placed in different groups is owned by the first.
    shared = {"P-x": ["r1", "r2"], "P-y": ["r2", "r3"]}  # r2 shared
    decls = [_grp("gx", ["P-x"], rules=shared), _grp("gy", ["P-y"], rules=shared)]
    groups = groups_from_specs(BASE, decls, cap=4)
    by = {g.name: g for g in groups}
    assert by["gx"].owned_rules == {"r1", "r2"}
    assert by["gy"].owned_rules == {"r3"}  # r2 already claimed by gx
    # partition: every rule owned exactly once
    owned = [r for g in groups for r in g.owned_rules]
    assert sorted(owned) == ["r1", "r2", "r3"]


def test_build_caps_and_merges_keeping_partition():
    decls = [_grp(f"g{i}", [p], summaries=s) for i, (p, s) in enumerate([
        ("P-bitmap", {"uncheckedExp": EXP_SUMMARY}),
        ("P-accounting", {"sortByKey": SORT_SUMMARY}),
        ("P-misc", {"uncheckedExp": EXP_SUMMARY}),
    ])]
    groups = groups_from_specs(BASE, decls, cap=2)
    assert len(groups) == 2
    owned = [r for g in groups for r in g.owned_rules]
    assert sorted(owned) == sorted(r for rs in PROP_RULES.values() for r in rs)


# --- judge-facing group plan rendering --------------------------------------


def test_over_cap_message_none_within_cap():
    specs = [_spec("a", [("P1", ["r1"])]), _spec("b", [("P2", ["r2"])])]
    assert over_cap_message(specs, cap=4) is None
    assert over_cap_message(specs, cap=2) is None


def test_over_cap_message_rejects_and_shows_the_forced_merge():
    # 3 groups, cap 2 -> reject. g1 and g2 agree on `foo` (cheapest merge, loses nothing); g3 is disjoint,
    # so the merge the run would force is g1+g2, which the message names so the agent can adopt or improve it.
    specs = [
        _spec("g1", [("P1", ["r1"])], summaries={"foo": "S"}),
        _spec("g2", [("P2", ["r2"])], summaries={"foo": "S"}),
        _spec("g3", [("P3", ["r3"])], summaries={"bar": "T"}),
    ]
    msg = over_cap_message(specs, cap=2)
    assert msg is not None
    assert "3 verification groups" in msg and "at most 2" in msg
    assert "AUTOPROVER_MAX_VERIFICATION_GROUPS" in msg
    assert "g1+g2" in msg and "g3" in msg


def test_render_plan_none_when_no_groups():
    assert render_group_plan_for_judge([]) is None


def test_render_plan_shows_per_group_installed_summaries():
    specs = [
        _spec("bitmap", [("P-bitmap", ["r_borrow", "r_collat"])], summaries={"uncheckedExp": EXP_SUMMARY}),
        _spec(
            "acct", [("P-accounting", ["r_supply"])],
            summaries={"sortByKey": SORT_SUMMARY, "uncheckedExp": EXP_SUMMARY},
        ),
    ]
    out = render_group_plan_for_judge(specs)
    assert out is not None
    # The bitmap group lists exactly the one summary it installs; sortByKey is precise there.
    bitmap = out.split('Group "bitmap"')[1].split('Group "acct"')[0]
    assert "installs summaries:" in bitmap
    assert "uncheckedExp:" in bitmap
    assert "sortByKey" not in bitmap
    # The acct group installs both summaries.
    acct = out.split('Group "acct"')[1]
    assert "uncheckedExp:" in acct and "sortByKey:" in acct
    # No conf is rendered — the judge is given the spec, not the .conf (and groups carry no conf).
    assert "conf" not in out.lower()
    # It is clearly marked informational so the judge does not treat it as spec text.
    assert "informational" in out.lower()
