"""Unit tests for scene-wide Math.Rounding classification and OZ Math spec rendering."""

import json
import re
from pathlib import Path

import pytest

from certora_autosetup.setup.setup_summaries import (
    RoundingClassification,
    RoundingVariant,
    SummarySetup,
)

V4_MEMBERS = ["Down", "Up", "Zero"]
V5_MEMBERS = ["Floor", "Ceil", "Trunc", "Expand"]

V4_SOURCE = "lib/oz-v4/Math.sol"
V5_SOURCE = "lib/oz-v5/Math.sol"


def _enum_row(main_contract, members, containing="Math", source_file=""):
    """One all_user_defined_types.json row, shaped like generate_all_user_defined_types_json emits."""
    return {
        "typeName": "Rounding",
        "qualifiedName": f"{containing}.Rounding",
        "baseType": "uint8",
        "typeCategory": "UserDefinedEnum",
        "containingContract": containing,
        "main_contract": main_contract,
        "sourceFile": source_file,
        "enumMembers": [{"name": m} for m in members],
    }


def _write_types(rows) -> None:
    internal = Path(".certora_internal")
    internal.mkdir(exist_ok=True)
    (internal / "all_user_defined_types.json").write_text(json.dumps(rows))
    # SummarySetup's TypeAnalyzer also insists on all_methods.json at init.
    methods = internal / "all_methods.json"
    if not methods.exists():
        methods.write_text("[]")


@pytest.fixture
def setup(tmp_path, monkeypatch) -> SummarySetup:
    monkeypatch.chdir(tmp_path)
    _write_types([])  # SummarySetup's TypeAnalyzer requires the file to exist
    summary_setup = SummarySetup()
    summary_setup.main_contract = "HarnessV5"
    return summary_setup


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classify_none_without_json(setup) -> None:
    Path(".certora_internal/all_user_defined_types.json").unlink()
    assert setup._classify_scene_rounding().kind == "none"


def test_classify_none_with_empty_json(setup) -> None:
    assert setup._classify_scene_rounding().kind == "none"


def test_classify_v4(setup) -> None:
    _write_types([_enum_row("HarnessV4", V4_MEMBERS, source_file=V4_SOURCE)])
    setup._scene_contracts = {"HarnessV4"}
    cls = setup._classify_scene_rounding()
    assert cls.kind == "single"
    assert cls.up_member == "Up"


def test_classify_v5(setup) -> None:
    _write_types([_enum_row("HarnessV5", V5_MEMBERS, source_file=V5_SOURCE)])
    setup._scene_contracts = {"HarnessV5"}
    cls = setup._classify_scene_rounding()
    assert cls.kind == "single"
    assert cls.up_member == "Ceil"


def test_classify_neither_member(setup) -> None:
    # A Math.Rounding with neither Up nor Ceil gets no directional summary.
    _write_types([_enum_row("HarnessX", ["Nearest", "Away"], source_file="lib/x/Math.sol")])
    setup._scene_contracts = {"HarnessX"}
    cls = setup._classify_scene_rounding()
    assert cls.kind == "single"
    assert cls.up_member is None


def test_classify_found_for_any_scene_contract(setup) -> None:
    # Regression: the old lookup was keyed on main_contract == the verified main
    # contract, so a Rounding enum imported only by an additional/linked scene
    # contract was missed. The classifier is scene-wide.
    _write_types([_enum_row("SomeLinkedContract", V5_MEMBERS, source_file=V5_SOURCE)])
    setup._scene_contracts = {"HarnessV5", "SomeLinkedContract"}
    cls = setup._classify_scene_rounding()
    assert cls.kind == "single"
    assert cls.up_member == "Ceil"


def test_classify_ignores_non_math_rounding(setup) -> None:
    # Regression: a `Rounding` enum declared in an unrelated contract must not
    # steer the Math summary.
    _write_types([_enum_row("MyToken", ["Nearest"], containing="MyToken", source_file="src/MyToken.sol")])
    setup._scene_contracts = {"MyToken"}
    assert setup._classify_scene_rounding().kind == "none"


def _mixed_rows():
    return [
        _enum_row("HarnessV4", V4_MEMBERS, source_file=V4_SOURCE),
        _enum_row("Math", V4_MEMBERS, source_file=V4_SOURCE),
        _enum_row("HarnessV5", V5_MEMBERS, source_file=V5_SOURCE),
        _enum_row("Math", V5_MEMBERS, source_file=V5_SOURCE),
    ]


def test_classify_mixed(setup) -> None:
    _write_types(_mixed_rows())
    setup._scene_contracts = {"HarnessV4", "HarnessV5", "Math"}
    cls = setup._classify_scene_rounding()
    assert cls.kind == "mixed"
    # Sorted by declaring source file; `Math` itself is never a qualifier.
    assert cls.variants == (
        RoundingVariant(qualifier="HarnessV4", up_member="Up"),
        RoundingVariant(qualifier="HarnessV5", up_member="Ceil"),
    )


def test_mixed_prefers_main_contract_qualifier(setup) -> None:
    rows = _mixed_rows() + [_enum_row("AnotherV5Consumer", V5_MEMBERS, source_file=V5_SOURCE)]
    _write_types(rows)
    setup._scene_contracts = {"HarnessV4", "HarnessV5", "AnotherV5Consumer"}
    setup.main_contract = "HarnessV5"
    cls = setup._classify_scene_rounding()
    assert RoundingVariant(qualifier="HarnessV5", up_member="Ceil") in cls.variants


def test_mixed_contract_seeing_both_is_not_a_qualifier(setup) -> None:
    # A contract whose import closure contains BOTH definitions cannot
    # disambiguate either of them.
    rows = _mixed_rows() + [
        _enum_row("SeesBoth", V4_MEMBERS, source_file=V4_SOURCE),
        _enum_row("SeesBoth", V5_MEMBERS, source_file=V5_SOURCE),
    ]
    _write_types(rows)
    setup._scene_contracts = {"HarnessV4", "HarnessV5", "SeesBoth"}
    cls = setup._classify_scene_rounding()
    assert {v.qualifier for v in cls.variants} == {"HarnessV4", "HarnessV5"}


def test_mixed_definition_without_qualifier_gets_no_variant(setup) -> None:
    # The v4 definition is only seen by a contract that also sees v5 — no
    # contract can qualify it, so only the v5 variant is emitted.
    rows = [
        _enum_row("SeesBoth", V4_MEMBERS, source_file=V4_SOURCE),
        _enum_row("SeesBoth", V5_MEMBERS, source_file=V5_SOURCE),
        _enum_row("HarnessV5", V5_MEMBERS, source_file=V5_SOURCE),
    ]
    _write_types(rows)
    setup._scene_contracts = {"SeesBoth", "HarnessV5"}
    cls = setup._classify_scene_rounding()
    assert cls.kind == "mixed"
    assert cls.variants == (RoundingVariant(qualifier="HarnessV5", up_member="Ceil"),)


def test_scene_invisible_definitions_do_not_count(setup) -> None:
    # The types JSON is project-wide: a repo can contain an OZ v4 Math whose
    # compilation unit never enters the prover scene. That definition must not
    # flip the classification to mixed — the prover REJECTS the qualified
    # spelling when the plain name is not ambiguous in the actual scene.
    _write_types(_mixed_rows())
    setup._scene_contracts = {"HarnessV5"}  # the v4 unit is not in the scene
    cls = setup._classify_scene_rounding()
    assert cls.kind == "single"
    assert cls.up_member == "Ceil"


def test_all_definitions_scene_invisible_is_none(setup) -> None:
    _write_types(_mixed_rows())
    setup._scene_contracts = {"SomethingElse"}
    assert setup._classify_scene_rounding().kind == "none"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _uncommented(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))


def test_render_single_v4(setup) -> None:
    out = setup._render_oz_math_spec(RoundingClassification(kind="single", up_member="Up"))
    assert 'import "../Math.spec";' in out
    assert (
        "function Math.mulDiv(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) internal returns (uint256) => mulDivDirectionalSummary(x, y, denominator, rounding);"
        in out
    )
    assert "if (rounding == Math.Rounding.Up)" in out
    assert "Math.Rounding.Ceil" not in out
    assert "$" not in out


def test_render_single_v5(setup) -> None:
    out = setup._render_oz_math_spec(RoundingClassification(kind="single", up_member="Ceil"))
    assert "if (rounding == Math.Rounding.Ceil)" in out
    assert "Rounding.Up" not in out


def test_render_no_directional(setup) -> None:
    out = setup._render_oz_math_spec(RoundingClassification(kind="none"))
    assert "mulDivDirectionalSummary" not in out
    assert (
        "function Math.mulDiv(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivDownSummary(x,y,denominator);"
        in out
    )
    assert "Math.sqrt" in out
    # No commented-out remnants — the entry is simply absent.
    assert "AUTO-DISABLED" not in out


def test_render_mixed(setup) -> None:
    out = setup._render_oz_math_spec(
        RoundingClassification(
            kind="mixed",
            variants=(
                RoundingVariant(qualifier="HarnessV4", up_member="Up"),
                RoundingVariant(qualifier="HarnessV5", up_member="Ceil"),
            ),
        )
    )
    # Wildcard receivers everywhere: a concrete `Math` receiver is ambiguous.
    assert "function _.mulDiv(uint256 x, uint256 y, uint256 denominator) internal => mulDivDownSummary(x,y,denominator) expect (uint256);" in out
    assert "function _.mulDiv(uint256 x, uint256 y, uint256 denominator, HarnessV4.Rounding rounding) internal => mulDivDirectionalSummary_HarnessV4(x, y, denominator, rounding) expect (uint256);" in out
    assert "if (rounding == HarnessV4.Rounding.Up)" in out
    assert "if (rounding == HarnessV5.Rounding.Ceil)" in out
    # The purged ambiguous name must not appear outside comments.
    assert not re.search(r"\bMath\.Rounding\b", _uncommented(out))
    assert not re.search(r"\bfunction Math\.", _uncommented(out))


def test_materialize_template_renders_from_scene(setup) -> None:
    # End-to-end through _materialize_template: the on-disk template is a stub;
    # the written spec must be the rendered classification.
    _write_types(_mixed_rows())
    setup._scene_contracts = {"HarnessV4", "HarnessV5"}
    rel = setup._materialize_template(
        "specs/summaries/OpenZeppelin/OZ_Math.template.spec", "HarnessV5"
    )
    written = (Path.cwd() / "certora" / rel).read_text()
    assert "HarnessV4.Rounding.Up" in written
    assert "HarnessV5.Rounding.Ceil" in written
    assert "$" not in written
