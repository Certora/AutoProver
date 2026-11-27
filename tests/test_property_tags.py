import json
from pathlib import Path

import pytest

import composer.pipeline.property_tags as pt
from composer.pipeline.property_tags import (
    StrayTag, check_suite, comments, read_tags, tagged_ids,
)

SPEC = """\
/// @property supply_matches_shares
invariant supplyMatchesShares()
    totalSupply() == sumOfShares();

// A plain comment, then the tags.
/// @property no_round_trip_profit, burn_rounding
/// @property mint_rounding
rule roundTrip(env e) {
    // @property inside_the_body
    assert true;
}

/**
 * Burning never pays out more than the shares are worth.
 * @property burn_rounding
 */
rule burnRounding(env e) {
    string s = "// @property in_a_string";
    assert true;
}

/// @property left_over

rule afterABlankLine(env e) {
    assert true;
}
"""

#: Each rule's first line in ``SPEC``, counted from 1.
FIRST_LINES = {"supplyMatchesShares": 2, "roundTrip": 8, "burnRounding": 17, "afterABlankLine": 24}


def test_tagged_ids_reads_comma_separated_ids_from_every_tag() -> None:
    assert tagged_ids("/ @property a, b @property c") == ["a", "b", "c"]
    assert tagged_ids("@property a,b  , c then prose") == ["a", "b", "c"]
    assert tagged_ids("@property a @property a") == ["a"]
    assert tagged_ids("@title Solvency @notice holds") == []


def test_comments_are_found_outside_strings() -> None:
    source = 'string s = "// not a comment"; // a comment\n/* block\n * two */ rule r() {}\n'
    found = comments(source)
    assert [(c.start_line, c.end_line, c.text, c.own_line) for c in found] == [
        (1, 1, "a comment", False),
        (2, 3, "block two", True),
    ]


def test_read_tags_maps_each_rule_to_the_tags_directly_above_it() -> None:
    tags = read_tags("s.spec", SPEC, FIRST_LINES)

    assert tags.by_rule == {
        "supplyMatchesShares": ("supply_matches_shares",),
        # Several tags add up; the plain comment in the same block is no tag.
        "roundTrip": ("no_round_trip_profit", "burn_rounding", "mint_rounding"),
        # A tag in a block comment, after prose.
        "burnRounding": ("burn_rounding",),
    }
    assert tags.stray == (
        StrayTag("s.spec", 9, ("inside_the_body",)),
        # A blank line parts the tag from the rule below it.
        StrayTag("s.spec", 22, ("left_over",)),
    )


def test_range_lines_count_from_one() -> None:
    assert pt._range_lines({"start": {"line": 15}, "end": {"line": 22}}) == (16, 23)
    assert pt._range_lines({"start": {}}) is None


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _project(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(root, "certora/specs/s.spec", SPEC.replace("mint_rounding", "typo_id"))
    _write(root, "certora/suites/rounding.property-suite.json", json.dumps({
        "component": {"name": "Rounding", "description": "How shares are rounded."},
        "imports": ["common.property-suite.json"],
        "properties": [{"id": "burn_rounding", "description": "burning rounds down"},
                       {"id": "no_round_trip_profit", "description": "d"},
                       {"id": "never_tagged", "description": "d"}],
    }))
    _write(root, "certora/suites/common.property-suite.json", json.dumps({"properties": []}))
    # Another suite: its properties may be tagged without being remarks, and it defines
    # burn_rounding differently.
    _write(root, "certora/suites/accounting.property-suite.json", json.dumps({
        "component": {"name": "Accounting", "description": "Minting and burning shares."},
        "properties": [{"id": "supply_matches_shares", "description": "d"},
                       {"id": "burn_rounding", "description": "burning rounds up"}],
    }))
    # Copies a tool wrote: skipped with ``skipped``.
    _write(root, "out-copies/s.spec", SPEC)
    monkeypatch.setattr(pt, "rule_lines", lambda _text: {r: (ln, ln + 1) for r, ln in FIRST_LINES.items()})


def test_check_suite_lists_each_property_with_its_rules_and_the_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path, monkeypatch)

    result = check_suite(tmp_path, "certora/suites/rounding.property-suite.json", ["out-copies"])

    spec = "certora/specs/s.spec"
    assert result.rules == {
        "burn_rounding": [(spec, "roundTrip"), (spec, "burnRounding")],
        "no_round_trip_profit": [(spec, "roundTrip")],
        "never_tagged": [],
    }
    by_kind: dict[str, list[pt.Problem]] = {}
    for p in result.problems:
        by_kind.setdefault(p.kind, []).append(p)
    [unknown] = by_kind["unknown-property"]
    assert (unknown.severity, unknown.file, unknown.rule) == ("error", spec, "roundTrip")
    assert unknown.message == "Rule roundTrip is tagged with typo_id, which no properties file defines."
    assert unknown.hint
    assert [(p.severity, p.line) for p in by_kind["stray-property-tag"]] == [("warning", 9), ("warning", 22)]
    [different] = by_kind["property-defined-differently"]
    assert different.message == (
        "Property burn_rounding has different definitions in "
        "certora/suites/accounting.property-suite.json, certora/suites/rounding.property-suite.json."
    )
    [untagged] = by_kind["untagged-property"]
    assert (untagged.severity, untagged.message) == ("info", "No rule is tagged with property never_tagged.")
    # supply_matches_shares is another suite's property: no problem.
    assert not [p for p in result.problems if "supply_matches_shares" in p.message]
    assert set(by_kind) == {"unknown-property", "stray-property-tag", "property-defined-differently", "untagged-property"}
    assert not result.ok


def test_a_suite_file_that_does_not_read_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, monkeypatch)
    _write(tmp_path, "certora/suites/broken.property-suite.json", json.dumps({
        "component": {"name": "Broken", "description": "d"}, "properties": [{"id": "x"}],
    }))
    # A run's own property dumps are named differently, and are not read.
    _write(tmp_path, "certora/properties/Component.properties.json", "[]")

    result = check_suite(tmp_path, "certora/suites/rounding.property-suite.json", ["out-copies"])

    unreadable = [p for p in result.problems if p.kind == "unreadable-property-file"]
    assert [(p.severity, p.file) for p in unreadable] == [("error", "certora/suites/broken.property-suite.json")]
    # The reason is the field error alone, not the file's path on this machine.
    assert unreadable[0].message == (
        "The file does not read as a properties file: properties.0.description: Field required"
    )


def test_a_suite_that_does_not_load_is_one_error(tmp_path: Path) -> None:
    _write(tmp_path, "a.property-suite.json", json.dumps({"imports": ["a.property-suite.json"]}))

    result = check_suite(tmp_path, "a.property-suite.json")

    [problem] = result.problems
    assert (problem.severity, problem.kind) == ("error", "suite-does-not-load") and "cycle" in problem.message
    assert pt.main([str(tmp_path), "a.property-suite.json"]) == 1
