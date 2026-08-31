"""Unit tests for the agent-declared, property-level group planner (transparent policy)."""

from composer.spec.source.agent_groups import (
    GroupDeclaration,
    build_declared_groups,
    validate_declared_coverage,
)

BASE = "// rules\ninvariant a() true;\nghost g(uint) returns uint;\n"
PALETTE = {
    "sortByKey": "function KVL.sortByKey(uint256[] l) internal returns (uint256[]) => g(0);",
    "uncheckedExp": "function MathUtils.uncheckedExp(uint256 a, uint256 b) internal returns (uint256) => g(a);",
}
# property title -> rule names
PROP_RULES = {
    "P-bitmap": ["r_borrow", "r_collat"],
    "P-accounting": ["r_supply", "r_premium"],
    "P-misc": ["r_misc"],
}


def _decl(name, props, keep=(), conf=None):
    return GroupDeclaration(
        name=name, properties=frozenset(props),
        keep_precise=frozenset(keep), conf_overlay=conf or {},
    )


# --- coverage validation ----------------------------------------------------


def test_coverage_ok():
    decls = [_decl("g1", ["P-bitmap"]), _decl("g2", ["P-accounting", "P-misc"])]
    assert validate_declared_coverage(
        decls, all_properties=set(PROP_RULES), skipped=set()
    ) is None


def test_coverage_missing_property():
    decls = [_decl("g1", ["P-bitmap"])]
    err = validate_declared_coverage(decls, all_properties=set(PROP_RULES), skipped=set())
    assert err is not None and "no group" in err


def test_coverage_duplicate_property():
    decls = [_decl("g1", ["P-bitmap"]), _decl("g2", ["P-bitmap", "P-accounting", "P-misc"])]
    err = validate_declared_coverage(decls, all_properties=set(PROP_RULES), skipped=set())
    assert err is not None and "more than one group" in err


def test_coverage_skipped_property_not_required_nor_assignable():
    # P-misc is skipped -> need not be covered, but must not be assigned either.
    ok = [_decl("g1", ["P-bitmap"]), _decl("g2", ["P-accounting"])]
    assert validate_declared_coverage(
        ok, all_properties=set(PROP_RULES), skipped={"P-misc"}
    ) is None
    bad = [_decl("g1", ["P-bitmap"]), _decl("g2", ["P-accounting", "P-misc"])]
    err = validate_declared_coverage(bad, all_properties=set(PROP_RULES), skipped={"P-misc"})
    assert err is not None and "skipped" in err


def test_coverage_unknown_property():
    decls = [_decl("g1", ["P-bitmap"]), _decl("g2", ["P-accounting", "P-misc", "P-ghost"])]
    err = validate_declared_coverage(decls, all_properties=set(PROP_RULES), skipped=set())
    assert err is not None and "unknown" in err


# --- build ------------------------------------------------------------------


def test_build_expands_properties_to_rules_and_summaries():
    decls = [
        _decl("bitmap", ["P-bitmap"], keep=["sortByKey"]),
        _decl("rest", ["P-accounting", "P-misc"], keep=["uncheckedExp"]),
    ]
    groups = build_declared_groups(
        BASE, declarations=decls, property_rules=PROP_RULES, summary_palette=PALETTE, cap=4
    )
    by = {g.name: g for g in groups}
    # bitmap group owns the bitmap property's rules, keeps sortByKey precise (not summarized),
    # summarizes uncheckedExp.
    assert by["bitmap"].owned_rules == {"r_borrow", "r_collat"}
    assert "KVL.sortByKey" not in by["bitmap"].spec_contents
    assert "MathUtils.uncheckedExp" in by["bitmap"].spec_contents
    # rest group is the mirror image and owns the other properties' rules.
    assert by["rest"].owned_rules == {"r_supply", "r_premium", "r_misc"}
    assert "MathUtils.uncheckedExp" not in by["rest"].spec_contents
    assert "KVL.sortByKey" in by["rest"].spec_contents


def test_build_conf_overlay_passes_through():
    decls = [
        _decl("slow", ["P-bitmap"], conf={"global_timeout": 4000, "loop_iter": 2}),
        _decl("fast", ["P-accounting", "P-misc"]),
    ]
    groups = build_declared_groups(
        BASE, declarations=decls, property_rules=PROP_RULES, summary_palette=PALETTE, cap=4
    )
    slow = next(g for g in groups if g.name == "slow")
    assert slow.conf_overlay == {"global_timeout": 4000, "loop_iter": 2}


def test_build_rule_partition_first_declaration_wins():
    # A rule shared by two properties placed in different groups is owned by the first.
    shared = {"P-x": ["r1", "r2"], "P-y": ["r2", "r3"]}  # r2 shared
    decls = [_decl("gx", ["P-x"]), _decl("gy", ["P-y"])]
    groups = build_declared_groups(
        BASE, declarations=decls, property_rules=shared, summary_palette={}, cap=4
    )
    by = {g.name: g for g in groups}
    assert by["gx"].owned_rules == {"r1", "r2"}
    assert by["gy"].owned_rules == {"r3"}  # r2 already claimed by gx
    # partition: every rule owned exactly once
    owned = [r for g in groups for r in g.owned_rules]
    assert sorted(owned) == ["r1", "r2", "r3"]


def test_build_caps_and_merges_keeping_partition():
    decls = [_decl(f"g{i}", [p], keep=[k]) for i, (p, k) in enumerate(
        [("P-bitmap", "sortByKey"), ("P-accounting", "uncheckedExp"), ("P-misc", "sortByKey")]
    )]
    groups = build_declared_groups(
        BASE, declarations=decls, property_rules=PROP_RULES, summary_palette=PALETTE, cap=2
    )
    assert len(groups) == 2
    owned = [r for g in groups for r in g.owned_rules]
    assert sorted(owned) == sorted(r for rs in PROP_RULES.values() for r in rs)
