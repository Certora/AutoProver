"""OpenZeppelin Math.Rounding handling.

The OZ ``Math`` library's ``Rounding`` enum differs between major versions (v4:
{Down, Up, Zero}; v5: {Floor, Ceil, Trunc, Expand}), and a verification scene
can even contain BOTH definitions at once (two vendored Math copies), in which
case the prover purges the ambiguous ``Math.Rounding`` name and every CVL
reference must be qualified by a contract that imports exactly one definition
(``C.Rounding``, certora-cli >= 8.17.1).

This module owns everything Rounding-specific: classifying the scene's
Rounding situation from the compiled-scene type inventory, rendering the OZ
Math summary spec for that classification, and looking up enum members per
qualifier contract for the typechecker's requalification fix.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple, Literal

from certora_autosetup.utils.constants import PATH_ALL_USER_DEFINED_TYPES_JSON

MATH_LIBRARY_NAME = "Math"
ROUNDING_ENUM_NAME = "Rounding"
# The bundled template name that _materialize_template routes to this module.
OZ_MATH_TEMPLATE_NAME = "OZ_Math.template.spec"

LogFn = Callable[..., None]


@dataclass(frozen=True)
class RoundingDefinition:
    """One distinct ``enum Rounding`` definition declared in a ``Math`` library
    present in the scene."""

    canonical_id: str  # declaring "path|Qualified.Name" (empty on legacy JSON)
    members: FrozenSet[str]
    seen_by: FrozenSet[str]  # scene contracts whose import closure contains it


@dataclass(frozen=True)
class RoundingVariant:
    """A directional-summary variant for one Rounding definition in a mixed scene."""

    qualifier: str  # scene contract qualifying the enum in CVL (C.Rounding)
    up_member: str  # "Up" (OZ v4) or "Ceil" (OZ v5)


@dataclass(frozen=True)
class RoundingClassification:
    """Scene-wide Math.Rounding classification driving the OZ Math spec rendering."""

    kind: Literal["none", "single", "mixed"]
    up_member: Optional[str] = None  # single: "Up"/"Ceil"; None = no directional
    variants: Tuple[RoundingVariant, ...] = ()  # mixed only


def _enum_member_names(enum_members: List) -> Set[str]:
    """Member names from an all_user_defined_types.json enum row (members are
    either dicts with a ``name`` field or bare strings)."""
    names: Set[str] = set()
    for member in enum_members:
        if isinstance(member, dict) and "name" in member:
            names.add(member["name"])
        elif isinstance(member, str):
            names.add(member)
    return names


def _round_up_member(members: FrozenSet[str]) -> Optional[str]:
    """The member that means "round up" for a Math.Rounding definition:
    ``Ceil`` in OZ v5 ({Floor, Ceil, Trunc, Expand}), ``Up`` in OZ v4
    ({Down, Up, Zero}). None if the definition has neither."""
    if "Ceil" in members:
        return "Ceil"
    if "Up" in members:
        return "Up"
    return None


def _load_math_rounding_rows(log: LogFn) -> List[Dict]:
    """Rounding-enum rows declared inside a ``Math`` library, from the
    compiled-scene type inventory. Any read failure degrades to an empty list
    (=> "none" classification, no directional summary)."""
    if not PATH_ALL_USER_DEFINED_TYPES_JSON.exists():
        log(f"{PATH_ALL_USER_DEFINED_TYPES_JSON} not found", "WARNING")
        return []
    try:
        with open(PATH_ALL_USER_DEFINED_TYPES_JSON, "r") as f:
            user_types = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"Could not read {PATH_ALL_USER_DEFINED_TYPES_JSON}: {e}", "ERROR")
        return []
    return [
        row
        for row in user_types
        if row.get("typeCategory") == "UserDefinedEnum"
        and row.get("typeName") == ROUNDING_ENUM_NAME
        and row.get("containingContract") == MATH_LIBRARY_NAME
    ]


def classify_scene_rounding(
    scene_contracts: Set[str], main_contract: str, log: LogFn
) -> RoundingClassification:
    """Classify the scene's ``Math.Rounding`` situation.

    Every scene contract's row set covers the types in its own import closure,
    so grouping rows by declaring canonicalId yields the DISTINCT ``Rounding``
    definitions visible from the scene: one definition -> the plain
    ``Math.Rounding`` spelling works; two or more -> the prover purges the
    ambiguous name and every reference must be qualified by a contract that
    imports exactly one definition.
    """
    rows = _load_math_rounding_rows(log)
    if not rows:
        log("No Math.Rounding enum found in user types", "WARNING")
        return RoundingClassification(kind="none")

    # Group rows into distinct definitions. canonicalId ("path|Qualified.Name")
    # tells two definitions apart even with identical members; rows without it
    # fall back to the member set.
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        members = _enum_member_names(row.get("enumMembers", []))
        key = row.get("canonicalId") or f"members:{','.join(sorted(members))}"
        group = grouped.setdefault(
            key, {"canonical_id": row.get("canonicalId", ""), "members": set(), "seen_by": set()}
        )
        group["members"] |= members
        group["seen_by"].add(row.get("main_contract"))

    definitions = [
        RoundingDefinition(
            canonical_id=g["canonical_id"],
            members=frozenset(g["members"]),
            seen_by=frozenset(g["seen_by"] & scene_contracts),
        )
        for g in grouped.values()
    ]
    # The types JSON comes from the project-wide compilation analysis, which
    # can compile Math copies whose units never enter the prover scene. Only
    # scene-visible definitions can conflict — counting the others would
    # misclassify an unambiguous scene as mixed, and the prover REJECTS the
    # contract-qualified spelling when the plain name is not ambiguous.
    definitions = [d for d in definitions if d.seen_by]
    if not definitions:
        log("No Math.Rounding definition visible from the scene", "WARNING")
        return RoundingClassification(kind="none")

    if len(definitions) == 1:
        up_member = _round_up_member(definitions[0].members)
        if up_member is None:
            log(
                f"Rounding enum found but contains neither 'Up' nor 'Ceil'. "
                f"Members: {sorted(definitions[0].members)}",
                "WARNING",
            )
        log(f"Single Math.Rounding definition in scene; round-up member: {up_member}")
        return RoundingClassification(kind="single", up_member=up_member)

    # Mixed: pick, per definition, a qualifier contract that sees ONLY this
    # definition (a contract seeing both cannot disambiguate) and is not the
    # ambiguous library name itself.
    log(
        f"{len(definitions)} conflicting Math.Rounding definitions in scene — "
        f"emitting qualified summaries (requires certora-cli >= 8.17.1)"
    )
    variants: List[RoundingVariant] = []
    for definition in sorted(definitions, key=lambda d: (d.canonical_id, sorted(d.members))):
        up_member = _round_up_member(definition.members)
        others_seen: Set[str] = set()
        for other in definitions:
            if other is not definition:
                others_seen |= other.seen_by
        candidates = {
            c for c in definition.seen_by
            if c and c not in others_seen and c != MATH_LIBRARY_NAME
        }
        if up_member is None or not candidates:
            log(
                f"Math.Rounding definition {definition.canonical_id or sorted(definition.members)} "
                f"gets no directional summary (round-up member: {up_member}, "
                f"qualifier candidates: {sorted(candidates)})",
                "WARNING",
            )
            continue
        qualifier = main_contract if main_contract in candidates else sorted(candidates)[0]
        variants.append(RoundingVariant(qualifier=qualifier, up_member=up_member))
    return RoundingClassification(kind="mixed", variants=tuple(variants))


def render_oz_math_spec(classification: RoundingClassification) -> str:
    """Render the OZ Math summary spec for the scene's Rounding classification.

    Generated programmatically rather than from placeholders because the mixed
    case needs a variable number of directional summaries with scene-dependent
    qualifiers.
    """
    header = 'import "../Math.spec";\n'

    if classification.kind != "mixed":
        lines = [
            header,
            "methods {",
            "    function Math.mulDiv(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivDownSummary(x,y,denominator);",
        ]
        if classification.up_member is not None:
            lines.append(
                "    function Math.mulDiv(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) internal returns (uint256) => mulDivDirectionalSummary(x, y, denominator, rounding);"
            )
        lines += [
            "    function Math.average(uint256 a, uint256 b) internal returns (uint256) => averageSummary(a,b);",
            "    function Math.sqrt(uint256 x) internal returns (uint256) => sqrtSummaryDown(x);",
            "}",
        ]
        if classification.up_member is not None:
            lines += [
                "",
                "function mulDivDirectionalSummary(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) returns uint256 {",
                "    // OZ v<5 used `Up`, v>=5 uses `Ceil`.",
                f"    if (rounding == Math.Rounding.{classification.up_member}) {{",
                "        return mulDivUpSummary(x, y, denominator);",
                "    } else {",
                "        return mulDivDownSummary(x, y, denominator);",
                "    }",
                "}",
            ]
        return "\n".join(lines) + "\n"

    # Mixed: `Math` (receiver and type qualifier alike) is ambiguous and purged
    # by the prover, so every entry uses a wildcard receiver, and each Rounding
    # definition is referenced through its qualifier contract. The unqualified
    # helpers are faithful (exact floor/ceil semantics), so wildcard matching
    # any same-signature internal function is sound.
    lines = [
        header,
        "// The scene contains conflicting `Math.Rounding` definitions (e.g. OZ v4 and",
        "// v5), so the names `Math` / `Math.Rounding` are ambiguous and unavailable in",
        "// CVL. Wildcard receivers + per-definition qualifier contracts are used",
        "// instead (requires certora-cli >= 8.17.1).",
        "methods {",
        "    function _.mulDiv(uint256 x, uint256 y, uint256 denominator) internal => mulDivDownSummary(x,y,denominator) expect (uint256);",
        "    function _.average(uint256 a, uint256 b) internal => averageSummary(a,b) expect (uint256);",
        "    function _.sqrt(uint256 x) internal => sqrtSummaryDown(x) expect (uint256);",
    ]
    for variant in classification.variants:
        lines.append(
            f"    function _.mulDiv(uint256 x, uint256 y, uint256 denominator, {variant.qualifier}.Rounding rounding) internal => mulDivDirectionalSummary_{variant.qualifier}(x, y, denominator, rounding) expect (uint256);"
        )
    lines.append("}")
    for variant in classification.variants:
        lines += [
            "",
            f"function mulDivDirectionalSummary_{variant.qualifier}(uint256 x, uint256 y, uint256 denominator, {variant.qualifier}.Rounding rounding) returns uint256 {{",
            f"    if (rounding == {variant.qualifier}.Rounding.{variant.up_member}) {{",
            "        return mulDivUpSummary(x, y, denominator);",
            "    } else {",
            "        return mulDivDownSummary(x, y, denominator);",
            "    }",
            "}",
        ]
    return "\n".join(lines) + "\n"


def rounding_members_by_qualifier(qualifiers: Set[str]) -> Dict[str, Set[str]]:
    """Member names of the Rounding enum each qualifier contract sees, from the
    compiled-scene type inventory. A suggested qualifier sees exactly one
    Rounding definition (that is why the prover suggests it), so its rows
    identify which definition — and which round-up member — it stands for."""
    result: Dict[str, Set[str]] = {q: set() for q in qualifiers}
    for row in _load_math_rounding_rows(lambda *a, **k: None):
        if row.get("main_contract") in result:
            result[row["main_contract"]] |= _enum_member_names(row.get("enumMembers", []))
    return result
