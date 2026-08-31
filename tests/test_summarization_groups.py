"""Unit tests for the summarization-footprint populator (Layer 2, piece 5).

Pure: exercises the clustering, per-group spec generation (append-methods
mechanism), and the cap's footprint-aware merge. No prover, no agent.
"""

from composer.spec.source.summarization_groups import (
    append_summaries,
    build_summarization_groups,
)

BASE = "// rules\ninvariant foo() true;\nghost g(uint) returns uint;\n"
ENTRIES = {
    "uncheckedExp": "function MathUtils.uncheckedExp(uint256 a, uint256 b) internal returns (uint256) => g(a);",
    "sortByKey": "function KeyValueList.sortByKey(uint256[] l) internal returns (uint256[]) => g(0);",
}


# --- append_summaries -------------------------------------------------------


def test_append_includes_only_affordable_summaries():
    # Keep sortByKey precise -> only its summary is dropped; uncheckedExp is summarized.
    spec = append_summaries(BASE, ENTRIES, needs_exact=frozenset({"sortByKey"}))
    assert "MathUtils.uncheckedExp" in spec
    assert "KeyValueList.sortByKey" not in spec
    assert "methods {" in spec
    assert spec.startswith(BASE.rstrip())  # base preserved verbatim, block appended


def test_append_all_precise_returns_base_unchanged():
    spec = append_summaries(BASE, ENTRIES, needs_exact=frozenset(ENTRIES))
    assert spec == BASE  # nothing left to summarize -> no methods block appended


def test_append_none_precise_summarizes_everything():
    spec = append_summaries(BASE, ENTRIES, needs_exact=frozenset())
    assert "MathUtils.uncheckedExp" in spec and "KeyValueList.sortByKey" in spec


# --- build_summarization_groups ---------------------------------------------


def test_build_groups_by_distinct_footprint():
    # r1 needs sortByKey precise; r2 needs uncheckedExp precise; r3 needs nothing.
    footprints = {
        "r1": frozenset({"sortByKey"}),
        "r2": frozenset({"uncheckedExp"}),
        "r3": frozenset(),
    }
    groups = build_summarization_groups(
        BASE, summary_entries=ENTRIES, footprints=footprints,
        all_rules=["r1", "r2", "r3"], cap=4,
    )
    assert len(groups) == 3
    by_rule = {r: g for g in groups for r in g.owned_rules}
    # r1's group keeps sortByKey precise (summary absent) but summarizes uncheckedExp.
    g1 = by_rule["r1"]
    assert "KeyValueList.sortByKey" not in g1.spec_contents
    assert "MathUtils.uncheckedExp" in g1.spec_contents
    # r2's group is the mirror image.
    g2 = by_rule["r2"]
    assert "MathUtils.uncheckedExp" not in g2.spec_contents
    assert "KeyValueList.sortByKey" in g2.spec_contents
    # r3 needs nothing precise -> both summarized.
    g3 = by_rule["r3"]
    assert "MathUtils.uncheckedExp" in g3.spec_contents and "KeyValueList.sortByKey" in g3.spec_contents


def test_build_groups_partition_covers_every_rule_once():
    footprints = {f"r{i}": frozenset({f"fn{i}"}) for i in range(5)}
    entries = {f"fn{i}": f"function C.fn{i}() internal => g(0);" for i in range(5)}
    groups = build_summarization_groups(
        BASE, summary_entries=entries, footprints=footprints,
        all_rules=[f"r{i}" for i in range(5)], cap=2,
    )
    assert len(groups) == 2  # capped
    owned = [r for g in groups for r in g.owned_rules]
    assert sorted(owned) == sorted(f"r{i}" for i in range(5))  # every rule exactly once


def test_cap_merge_regenerates_spec_for_unioned_footprint():
    # Two singleton footprints, capped to 1 -> merged group keeps BOTH precise,
    # so its spec summarizes NEITHER of the two merged functions.
    footprints = {"r1": frozenset({"uncheckedExp"}), "r2": frozenset({"sortByKey"})}
    groups = build_summarization_groups(
        BASE, summary_entries=ENTRIES, footprints=footprints,
        all_rules=["r1", "r2"], cap=1,
    )
    assert len(groups) == 1
    merged = groups[0]
    assert merged.owned_rules == {"r1", "r2"}
    assert merged.footprint == {"uncheckedExp", "sortByKey"}
    # Both needed precise now -> the merged spec has no summaries (== base).
    assert "MathUtils.uncheckedExp" not in merged.spec_contents
    assert "KeyValueList.sortByKey" not in merged.spec_contents


def test_no_footprints_no_entries_degenerates_to_single_shared_group():
    groups = build_summarization_groups(
        BASE, summary_entries={}, footprints={}, all_rules=["r1", "r2"], cap=4,
    )
    assert len(groups) == 1
    assert groups[0].owned_rules == {"r1", "r2"}
    assert groups[0].spec_contents is None  # the shared spec, unchanged
