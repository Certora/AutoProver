"""Unit tests for scene-wide Math.Rounding classification and OZ Math spec rendering."""

import json
import re
from pathlib import Path

import pytest

from certora_autosetup.setup.oz_math_rounding import (
    RoundingClassification,
    RoundingVariant,
    classify_scene_rounding,
    render_oz_math_spec,
)
from certora_autosetup.setup.setup_summaries import SummarySetup
from certora_autosetup.utils.constants import PATH_ALL_USER_DEFINED_TYPES_JSON

V4_MEMBERS = ["Down", "Up", "Zero"]
V5_MEMBERS = ["Floor", "Ceil", "Trunc", "Expand"]

V4_CANONICAL = "lib/oz-v4/Math.sol|Math.Rounding"
V5_CANONICAL = "lib/oz-v5/Math.sol|Math.Rounding"


def _log(*_args, **_kwargs):
    pass


def _enum_row(main_contract, members, containing="Math", canonical_id=""):
    """One all_user_defined_types.json row, shaped like generate_all_user_defined_types_json emits."""
    return {
        "typeName": "Rounding",
        "qualifiedName": f"{containing}.Rounding",
        "baseType": "uint8",
        "typeCategory": "UserDefinedEnum",
        "containingContract": containing,
        "main_contract": main_contract,
        "canonicalId": canonical_id,
        "enumMembers": [{"name": m} for m in members],
    }


def _write_types(rows) -> None:
    PATH_ALL_USER_DEFINED_TYPES_JSON.parent.mkdir(exist_ok=True)
    PATH_ALL_USER_DEFINED_TYPES_JSON.write_text(json.dumps(rows))
    # SummarySetup's TypeAnalyzer also insists on all_methods.json at init.
    methods = PATH_ALL_USER_DEFINED_TYPES_JSON.parent / "all_methods.json"
    if not methods.exists():
        methods.write_text("[]")


@pytest.fixture(autouse=True)
def _in_tmp_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_types([])


def _classify(scene, main="HarnessV5"):
    return classify_scene_rounding(scene, main, _log)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classify_none_without_json() -> None:
    PATH_ALL_USER_DEFINED_TYPES_JSON.unlink()
    assert _classify({"HarnessV5"}).kind == "none"


def test_classify_none_with_unreadable_json() -> None:
    # A corrupt types file must degrade to "none", never crash the setup.
    PATH_ALL_USER_DEFINED_TYPES_JSON.write_text("{not json")
    assert _classify({"HarnessV5"}).kind == "none"


def test_classify_none_with_empty_json() -> None:
    assert _classify({"HarnessV5"}).kind == "none"


def test_classify_v4() -> None:
    _write_types([_enum_row("HarnessV4", V4_MEMBERS, canonical_id=V4_CANONICAL)])
    cls = _classify({"HarnessV4"})
    assert cls.kind == "single"
    assert cls.up_member == "Up"


def test_classify_v5() -> None:
    _write_types([_enum_row("HarnessV5", V5_MEMBERS, canonical_id=V5_CANONICAL)])
    cls = _classify({"HarnessV5"})
    assert cls.kind == "single"
    assert cls.up_member == "Ceil"


def test_classify_neither_member() -> None:
    # A Math.Rounding with neither Up nor Ceil gets no directional summary.
    _write_types([_enum_row("HarnessX", ["Nearest", "Away"], canonical_id="lib/x/Math.sol|Math.Rounding")])
    cls = _classify({"HarnessX"})
    assert cls.kind == "single"
    assert cls.up_member is None


def test_classify_found_for_any_scene_contract() -> None:
    # Regression: the old lookup was keyed on main_contract == the verified main
    # contract, so a Rounding enum imported only by an additional/linked scene
    # contract was missed. The classifier is scene-wide.
    _write_types([_enum_row("SomeLinkedContract", V5_MEMBERS, canonical_id=V5_CANONICAL)])
    cls = _classify({"HarnessV5", "SomeLinkedContract"})
    assert cls.kind == "single"
    assert cls.up_member == "Ceil"


def test_classify_ignores_non_math_rounding() -> None:
    # Regression: a `Rounding` enum declared in an unrelated contract must not
    # steer the Math summary.
    _write_types([_enum_row("MyToken", ["Nearest"], containing="MyToken",
                            canonical_id="src/MyToken.sol|MyToken.Rounding")])
    assert _classify({"MyToken"}).kind == "none"


def _mixed_rows():
    return [
        _enum_row("HarnessV4", V4_MEMBERS, canonical_id=V4_CANONICAL),
        _enum_row("Math", V4_MEMBERS, canonical_id=V4_CANONICAL),
        _enum_row("HarnessV5", V5_MEMBERS, canonical_id=V5_CANONICAL),
        _enum_row("Math", V5_MEMBERS, canonical_id=V5_CANONICAL),
    ]


def test_classify_mixed() -> None:
    _write_types(_mixed_rows())
    cls = _classify({"HarnessV4", "HarnessV5", "Math"})
    assert cls.kind == "mixed"
    # Sorted by declaring canonicalId; `Math` itself is never a qualifier.
    assert cls.variants == (
        RoundingVariant(qualifier="HarnessV4", up_member="Up"),
        RoundingVariant(qualifier="HarnessV5", up_member="Ceil"),
    )


def test_mixed_prefers_main_contract_qualifier() -> None:
    _write_types(_mixed_rows() + [_enum_row("AnotherV5Consumer", V5_MEMBERS, canonical_id=V5_CANONICAL)])
    cls = _classify({"HarnessV4", "HarnessV5", "AnotherV5Consumer"}, main="HarnessV5")
    assert RoundingVariant(qualifier="HarnessV5", up_member="Ceil") in cls.variants


def test_mixed_contract_seeing_both_is_not_a_qualifier() -> None:
    # A contract whose import closure contains BOTH definitions cannot
    # disambiguate either of them.
    _write_types(_mixed_rows() + [
        _enum_row("SeesBoth", V4_MEMBERS, canonical_id=V4_CANONICAL),
        _enum_row("SeesBoth", V5_MEMBERS, canonical_id=V5_CANONICAL),
    ])
    cls = _classify({"HarnessV4", "HarnessV5", "SeesBoth"})
    assert {v.qualifier for v in cls.variants} == {"HarnessV4", "HarnessV5"}


def test_mixed_definition_without_qualifier_gets_no_variant() -> None:
    # The v4 definition is only seen by a contract that also sees v5 — no
    # contract can qualify it, so only the v5 variant is emitted.
    _write_types([
        _enum_row("SeesBoth", V4_MEMBERS, canonical_id=V4_CANONICAL),
        _enum_row("SeesBoth", V5_MEMBERS, canonical_id=V5_CANONICAL),
        _enum_row("HarnessV5", V5_MEMBERS, canonical_id=V5_CANONICAL),
    ])
    cls = _classify({"SeesBoth", "HarnessV5"})
    assert cls.kind == "mixed"
    assert cls.variants == (RoundingVariant(qualifier="HarnessV5", up_member="Ceil"),)


def test_scene_invisible_definitions_do_not_count() -> None:
    # The types JSON is project-wide: a repo can contain an OZ v4 Math whose
    # compilation unit never enters the prover scene. That definition must not
    # flip the classification to mixed — the prover REJECTS the qualified
    # spelling when the plain name is not ambiguous in the actual scene.
    _write_types(_mixed_rows())
    cls = _classify({"HarnessV5"})  # the v4 unit is not in the scene
    assert cls.kind == "single"
    assert cls.up_member == "Ceil"


def test_all_definitions_scene_invisible_is_none() -> None:
    _write_types(_mixed_rows())
    assert _classify({"SomethingElse"}).kind == "none"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _uncommented(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))


def test_render_single_v4() -> None:
    out = render_oz_math_spec(RoundingClassification(kind="single", up_member="Up"))
    assert 'import "../Math.spec";' in out
    assert (
        "function Math.mulDiv(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) internal returns (uint256) => mulDivDirectionalSummary(x, y, denominator, rounding);"
        in out
    )
    assert "if (rounding == Math.Rounding.Up)" in out
    assert "Math.Rounding.Ceil" not in out
    assert "$" not in out


def test_render_single_v5() -> None:
    out = render_oz_math_spec(RoundingClassification(kind="single", up_member="Ceil"))
    assert "if (rounding == Math.Rounding.Ceil)" in out
    assert "Rounding.Up" not in out


def test_render_no_directional() -> None:
    out = render_oz_math_spec(RoundingClassification(kind="none"))
    assert "mulDivDirectionalSummary" not in out
    assert (
        "function Math.mulDiv(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivDownSummary(x,y,denominator);"
        in out
    )
    assert "Math.sqrt" in out
    # No commented-out remnants — the entry is simply absent.
    assert "AUTO-DISABLED" not in out


def test_render_mixed() -> None:
    out = render_oz_math_spec(
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


def test_materialize_template_renders_from_scene() -> None:
    # End-to-end through SummarySetup._materialize_template: the on-disk
    # template is a stub; the written spec must be the rendered classification.
    _write_types(_mixed_rows())
    setup = SummarySetup()
    setup.main_contract = "HarnessV5"
    setup._scene_contracts = {"HarnessV4", "HarnessV5"}
    rel = setup._materialize_template(
        "specs/summaries/OpenZeppelin/OZ_Math.template.spec", "HarnessV5"
    )
    written = (Path.cwd() / "certora" / rel).read_text()
    assert "HarnessV4.Rounding.Up" in written
    assert "HarnessV5.Rounding.Ceil" in written
    assert "$" not in written
