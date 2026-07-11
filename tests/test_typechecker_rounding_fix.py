"""Unit tests for Rounding-error parsing and the requalification fix in the typechecker loop."""

import json
from pathlib import Path

import pytest

from certora_autosetup.typechecker_loop import TypecheckerLoop

# Pre-8.17.1 error text (unqualified name not resolvable at all).
OLD_ROUNDING_ERROR = (
    'CRITICAL: [main] ERROR ALWAYS - Error in spec file (OZ_Math-Vault.spec:12:21): '
    'could not type expression "Math.Rounding.Ceil", message: In enum constant '
    "Math.Rounding.Ceil, Math.Rounding is not a valid enum type"
)

# certora-cli >= 8.17.1: conflicting same-name definitions purge the type and the
# error suggests importing-contract qualifiers.
AMBIGUOUS_ENUM_CONSTANT_ERROR = (
    "CRITICAL: [main] ERROR ALWAYS - Error in spec file (OZ_Math-HarnessV5.spec:12:21): "
    'could not type expression "Math.Rounding.Ceil", message: In enum constant '
    "Math.Rounding.Ceil, Type Math.Rounding is not a valid type. "
    "Did you mean `HarnessV4.Rounding`, or `HarnessV5.Rounding`?"
)
AMBIGUOUS_PARAM_TYPE_ERROR = (
    "Error in spec file (OZ_Math-HarnessV5.spec:5:5): Type Math.Rounding is not a valid type. "
    "Did you mean `HarnessV4.Rounding`, or `HarnessV5.Rounding`?"
)
# The methods{} entry position uses a DIFFERENT text: no "Type " prefix and
# "EVM type" (observed on a real certoraRun 8.17.1 run).
AMBIGUOUS_METHODS_ENTRY_ERROR = (
    "Error in spec file (OZ_Math-HarnessV5.spec:5:5): Math.Rounding is not a valid EVM type. "
    "Did you mean `HarnessV4.Rounding`, or `HarnessV5.Rounding`?"
)
# Reverse direction: a qualified spelling in a scene where the plain name is
# NOT ambiguous is also rejected, suggesting the plain spelling back.
UNAMBIGUOUS_QUALIFIED_ERROR = (
    "Error in spec file (OZ_Math-HarnessV5.spec:9:5): HarnessV5.Rounding is not a valid EVM type. "
    "Did you mean `Math.Rounding`?"
)
# Scene-level warning emitted at populate time (not parsed today; spelling pinned
# here so a future change to react to it has the exact text).
CONFLICT_WARNING = (
    "Conflicting types with name Math.Rounding, neither will be available within the spec. "
    "Qualify by the originating contract instead (e.g. `SomeContract.Rounding`)."
)


@pytest.fixture
def loop(tmp_path, monkeypatch) -> TypecheckerLoop:
    monkeypatch.chdir(tmp_path)
    return TypecheckerLoop(certora_dir=tmp_path / "certora")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_old_rounding_error(loop) -> None:
    matches = loop._parse_typechecker_errors(OLD_ROUNDING_ERROR)
    assert ("OZ_Math-Vault.spec", "12", "ROUNDING_ERROR", "Math.Rounding") in matches


def test_parse_old_rounding_error_any_qualifier(loop) -> None:
    # The pre-8.17.1 pattern must not be anchored on the literal `Math.` — a
    # qualified spelling hitting an old CLI produces the same error family.
    text = OLD_ROUNDING_ERROR.replace("Math.Rounding", "HarnessV5.Rounding")
    matches = loop._parse_typechecker_errors(text)
    assert any(m[2] == "ROUNDING_ERROR" for m in matches)


def test_parse_ambiguous_enum_constant_error(loop) -> None:
    matches = loop._parse_typechecker_errors(AMBIGUOUS_ENUM_CONSTANT_ERROR)
    assert (
        "OZ_Math-HarnessV5.spec",
        "12",
        "ROUNDING_AMBIGUOUS",
        "Math|HarnessV4,HarnessV5",
    ) in matches
    # The ambiguous line must not ALSO be classified as the old blind-fix error.
    assert not any(m[2] == "ROUNDING_ERROR" for m in matches)


def test_parse_ambiguous_param_type_error(loop) -> None:
    matches = loop._parse_typechecker_errors(AMBIGUOUS_PARAM_TYPE_ERROR)
    assert (
        "OZ_Math-HarnessV5.spec",
        "5",
        "ROUNDING_AMBIGUOUS",
        "Math|HarnessV4,HarnessV5",
    ) in matches


def test_parse_ambiguous_methods_entry_error(loop) -> None:
    # Regression: the methods-entry variant ("is not a valid EVM type", no
    # "Type " prefix) must be parsed too, or the 4-arg entry keeps its bad
    # qualifier forever and the loop cannot converge.
    matches = loop._parse_typechecker_errors(AMBIGUOUS_METHODS_ENTRY_ERROR)
    assert (
        "OZ_Math-HarnessV5.spec",
        "5",
        "ROUNDING_AMBIGUOUS",
        "Math|HarnessV4,HarnessV5",
    ) in matches


def test_parse_reverse_direction_single_suggestion(loop) -> None:
    matches = loop._parse_typechecker_errors(UNAMBIGUOUS_QUALIFIED_ERROR)
    assert (
        "OZ_Math-HarnessV5.spec",
        "9",
        "ROUNDING_AMBIGUOUS",
        "HarnessV5|Math",
    ) in matches


def test_parse_both_ambiguous_errors_together(loop) -> None:
    matches = loop._parse_typechecker_errors(
        AMBIGUOUS_ENUM_CONSTANT_ERROR + "\n" + AMBIGUOUS_METHODS_ENTRY_ERROR + "\n" + CONFLICT_WARNING
    )
    ambiguous = [m for m in matches if m[2] == "ROUNDING_AMBIGUOUS"]
    assert len(ambiguous) == 2


# ---------------------------------------------------------------------------
# Requalification fix
# ---------------------------------------------------------------------------

SPEC_BEFORE = """import "../Math.spec";

methods {
    function Math.mulDiv(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivDownSummary(x,y,denominator);
    function Math.mulDiv(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) internal returns (uint256) => mulDivDirectionalSummary(x, y, denominator, rounding);
    function Math.average(uint256 a, uint256 b) internal returns (uint256) => averageSummary(a,b);
    function Math.sqrt(uint256 x) internal returns (uint256) => sqrtSummaryDown(x);
}

function mulDivDirectionalSummary(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) returns uint256 {
    // OZ v<5 used `Up`, v>=5 uses `Ceil`.
    if (rounding == Math.Rounding.Ceil) {
        return mulDivUpSummary(x, y, denominator);
    } else {
        return mulDivDownSummary(x, y, denominator);
    }
}
"""


def _write_scene_types() -> None:
    internal = Path(".certora_internal")
    internal.mkdir(exist_ok=True)
    rows = [
        {
            "typeName": "Rounding",
            "typeCategory": "UserDefinedEnum",
            "containingContract": "Math",
            "main_contract": "HarnessV4",
            "sourceFile": "lib/oz-v4/Math.sol",
            "enumMembers": [{"name": m} for m in ("Down", "Up", "Zero")],
        },
        {
            "typeName": "Rounding",
            "typeCategory": "UserDefinedEnum",
            "containingContract": "Math",
            "main_contract": "HarnessV5",
            "sourceFile": "lib/oz-v5/Math.sol",
            "enumMembers": [{"name": m} for m in ("Floor", "Ceil", "Trunc", "Expand")],
        },
    ]
    (internal / "all_user_defined_types.json").write_text(json.dumps(rows))


def _run_requalify(loop, tmp_path, errors):
    spec = tmp_path / "OZ_Math-HarnessV5.spec"
    spec.write_text(SPEC_BEFORE)
    callback = loop._create_rounding_requalify_callback(errors, keep_intermediate=False)
    callback(spec, lambda s: s, lambda s: s)
    return spec.read_text()


def test_requalify_by_member(loop, tmp_path) -> None:
    _write_scene_types()
    # Line 12 holds Math.Rounding.Ceil; line 5 and 10 hold the param types.
    errors = [
        ("12", "ROUNDING_AMBIGUOUS", "Math|HarnessV4,HarnessV5"),
        ("5", "ROUNDING_AMBIGUOUS", "Math|HarnessV4,HarnessV5"),
        ("10", "ROUNDING_AMBIGUOUS", "Math|HarnessV4,HarnessV5"),
    ]
    fixed = _run_requalify(loop, tmp_path, errors)
    # Ceil pins the qualifier to HarnessV5 (only its Rounding has Ceil), and the
    # member-less param-type lines follow that spec-wide choice.
    assert "rounding == HarnessV5.Rounding.Ceil" in fixed
    assert "uint256 denominator, HarnessV5.Rounding rounding) internal returns" in fixed
    assert "function mulDivDirectionalSummary(uint256 x, uint256 y, uint256 denominator, HarnessV5.Rounding rounding)" in fixed
    assert "AUTO-DISABLED" not in fixed
    assert "Math.Rounding" not in fixed


def test_requalify_falls_back_to_disable_without_member_info(loop, tmp_path) -> None:
    # No member appears in any error line's text (only param-type positions) and
    # the referenced member is unknown -> no unique choice -> reported lines are
    # disabled, nothing else touched.
    _write_scene_types()
    errors = [("5", "ROUNDING_AMBIGUOUS", "Math|HarnessV4,HarnessV5")]
    spec = tmp_path / "OZ_Math-HarnessV5.spec"
    spec.write_text(SPEC_BEFORE)
    callback = loop._create_rounding_requalify_callback(errors, keep_intermediate=False)
    callback(spec, lambda s: s, lambda s: s)
    fixed = spec.read_text()
    lines = fixed.splitlines()
    assert lines[4].startswith("// AUTO-DISABLED (Math.Rounding error):")
    # Only the reported line (plus no block, since line 5 is a methods entry).
    assert sum("AUTO-DISABLED" in l for l in lines) == 1


def test_requalify_member_in_no_suggestion_disables_block(loop, tmp_path) -> None:
    # The member on the error line exists in NO suggested qualifier's enum ->
    # fallback disables the line; a line inside the directional function takes
    # the whole block with it (half-commented CVL would not parse).
    _write_scene_types()
    spec_text = SPEC_BEFORE.replace("Math.Rounding.Ceil", "Math.Rounding.Nearest")
    spec = tmp_path / "OZ_Math-HarnessV5.spec"
    spec.write_text(spec_text)
    errors = [("12", "ROUNDING_AMBIGUOUS", "Math|HarnessV4,HarnessV5")]
    callback = loop._create_rounding_requalify_callback(errors, keep_intermediate=False)
    callback(spec, lambda s: s, lambda s: s)
    lines = spec.read_text().splitlines()
    disabled = [i for i, l in enumerate(lines) if "AUTO-DISABLED" in l]
    # The whole mulDivDirectionalSummary block (0-indexed 9-16), minus index 10
    # which was already a comment line and needs no disabling.
    assert disabled == [9, 11, 12, 13, 14, 15, 16]


def test_requalify_reverse_direction_to_plain_math(loop, tmp_path) -> None:
    # A qualified spec landing in a scene where the plain name is unambiguous
    # gets healed back to Math.Rounding via the suggestion.
    internal = Path(".certora_internal")
    internal.mkdir(exist_ok=True)
    (internal / "all_user_defined_types.json").write_text(
        json.dumps(
            [
                {
                    "typeName": "Rounding",
                    "typeCategory": "UserDefinedEnum",
                    "containingContract": "Math",
                    "main_contract": "Math",
                    "sourceFile": "lib/oz-v5/Math.sol",
                    "enumMembers": [{"name": m} for m in ("Floor", "Ceil", "Trunc", "Expand")],
                }
            ]
        )
    )
    spec = tmp_path / "OZ_Math-HarnessV5.spec"
    spec.write_text(SPEC_BEFORE.replace("Math.Rounding", "HarnessV5.Rounding"))
    errors = [
        ("12", "ROUNDING_AMBIGUOUS", "HarnessV5|Math"),
        ("5", "ROUNDING_AMBIGUOUS", "HarnessV5|Math"),
        ("10", "ROUNDING_AMBIGUOUS", "HarnessV5|Math"),
    ]
    callback = loop._create_rounding_requalify_callback(errors, keep_intermediate=False)
    callback(spec, lambda s: s, lambda s: s)
    fixed = spec.read_text()
    assert "rounding == Math.Rounding.Ceil" in fixed
    assert "HarnessV5.Rounding" not in fixed
    assert "AUTO-DISABLED" not in fixed


def test_block_expansion_covers_any_function_name(loop, tmp_path) -> None:
    # Regression: the block expansion must not be tied to the
    # mulDivDirectionalSummary name — OZ_MathUpgradeable.spec's helper is named
    # mathUpgradeableMulDivDirectionalSummary, and commenting only the reported
    # line inside it would leave unparsable CVL.
    spec_text = SPEC_BEFORE.replace(
        "mulDivDirectionalSummary", "mathUpgradeableMulDivDirectionalSummary"
    )
    spec = tmp_path / "OZ_MathUpgradeable.spec"
    spec.write_text(spec_text)
    errors = [("OZ_MathUpgradeable.spec", "12", "ROUNDING_ERROR", "Math.Rounding")]
    updates = loop.generate_updates_to_specs_from_errors(errors)
    updates["OZ_MathUpgradeable"](spec, lambda s: s, lambda s: s)
    lines = spec.read_text().splitlines()
    disabled = [i for i, l in enumerate(lines) if "AUTO-DISABLED" in l]
    # Whole function block (0-indexed 9-16; 10 was already a comment line but
    # the old-error fix path prefixes indiscriminately, matching prior behavior).
    assert set(disabled) >= {9, 11, 12, 13, 14, 15, 16}
    # Nothing outside the block was touched.
    assert min(disabled) >= 9 and max(disabled) <= 16


def test_old_fix_comments_reported_lines_not_hardcoded_range(loop, tmp_path) -> None:
    # Regression: the pre-rework fix commented hardcoded line indices [4, 9..16]
    # regardless of the reported errors. It must now target reported lines only
    # (expanding to the enclosing directional block).
    spec = tmp_path / "OZ_Math-HarnessV5.spec"
    spec.write_text(SPEC_BEFORE)
    errors = [("OZ_Math-HarnessV5.spec", "5", "ROUNDING_ERROR", "Math.Rounding")]
    updates = loop.generate_updates_to_specs_from_errors(errors)
    assert "OZ_Math-HarnessV5" in updates
    updates["OZ_Math-HarnessV5"](spec, lambda s: s, lambda s: s)
    lines = spec.read_text().splitlines()
    assert lines[4].startswith("// AUTO-DISABLED (Math.Rounding error):")
    assert sum("AUTO-DISABLED" in l for l in lines) == 1  # not the old lines 10-17 sweep
