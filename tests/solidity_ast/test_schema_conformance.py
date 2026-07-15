"""Machine-checks every solidity_ast pydantic model against the vendored JSON Schema.

The vendored OpenZeppelin ``solidity-ast`` schema (``certora_autosetup/solidity_ast/
schema/schema.json``) is the source of truth for field sets. This test asserts, per
schema definition and property: presence, requiredness, nullability, discriminator
value, enum values, and a one-level structural kind check of the pydantic annotation.

The schema-side helpers (``load_schema``/``node_definitions``/``classify_prop``) are
pure and importable without the model modules; everything model-side is imported
lazily via ``models()`` so this file stays usable while the model package is being
built.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field as dc_field, replace
from functools import lru_cache
from importlib import resources
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pytest

# ---------------------------------------------------------------------------
# Schema side (pure: no model imports)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def load_schema() -> dict[str, Any]:
    schema_file = resources.files("certora_autosetup.solidity_ast") / "schema" / "schema.json"
    return json.loads(schema_file.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def node_definitions() -> dict[str, dict[str, Any]]:
    """Every schema definition that has a ``nodeType`` property, plus the schema
    ROOT (the SourceUnit definition lives at the top level, not under definitions).
    """
    schema = load_schema()
    defs = {
        name: definition
        for name, definition in schema["definitions"].items()
        if "nodeType" in definition.get("properties", {})
    }
    defs["SourceUnit"] = {"properties": schema["properties"], "required": schema["required"]}
    return defs


@dataclass(frozen=True)
class Shape:
    """One classified schema property (nulls stripped out of anyOf into ``nullable``)."""

    kind: str  # primitive | enum | ref | array | map | object | union | null
    nullable: bool = False
    py: type | None = None  # primitive
    values: tuple[Any, ...] = ()  # enum
    ref: str = ""  # ref (definition name)
    item: "Shape | None" = None  # array
    members: tuple["Shape", ...] = ()  # union


_PRIMITIVES = {"string": str, "integer": int, "boolean": bool, "number": float}


def classify_prop(prop: dict[str, Any]) -> Shape:
    """Classify a schema property into a Shape; raises on any shape the schema does
    not actually contain (so schema updates that add new shapes fail loudly).
    """
    if "$ref" in prop:
        return Shape("ref", ref=prop["$ref"].rsplit("/", 1)[-1])
    if "anyOf" in prop:
        non_null = [m for m in prop["anyOf"] if m.get("type") != "null"]
        nullable = len(non_null) < len(prop["anyOf"])
        if not non_null:
            return Shape("null", nullable=True)
        if len(non_null) == 1:
            inner = classify_prop(non_null[0])
            return replace(inner, nullable=nullable or inner.nullable)
        return Shape(
            "union", nullable=nullable, members=tuple(classify_prop(m) for m in non_null)
        )
    if "enum" in prop:
        return Shape("enum", values=tuple(prop["enum"]))
    schema_type = prop.get("type")
    if schema_type == "null":
        return Shape("null", nullable=True)
    if schema_type in _PRIMITIVES:
        return Shape("primitive", py=_PRIMITIVES[schema_type])
    if schema_type == "array":
        return Shape("array", item=classify_prop(prop["items"]))
    if schema_type == "object":
        additional = prop.get("additionalProperties")
        if isinstance(additional, dict):
            return Shape("map", item=classify_prop(additional))
        return Shape("object")
    raise ValueError(f"unclassifiable schema property: {json.dumps(prop)[:200]}")


def classify_all() -> Counter[str]:
    """Classify every property of every node definition; raises if any is
    unclassifiable. Returns kind counts ('?' suffix marks nullable shapes).
    Runnable standalone, before the model modules exist.
    """
    counts: Counter[str] = Counter()
    for definition in node_definitions().values():
        for prop in definition["properties"].values():
            shape = classify_prop(prop)
            counts[shape.kind + ("?" if shape.nullable else "")] += 1
    return counts


# ---------------------------------------------------------------------------
# Model side (lazy imports: unions.py wires and rebuilds all node modules)
# ---------------------------------------------------------------------------


class ModelInterface:
    def __init__(self) -> None:
        from pydantic import BaseModel

        from certora_autosetup.solidity_ast import unions, yul
        from certora_autosetup.solidity_ast.base import (
            Mutability,
            StateMutability,
            StorageLocation,
            TypeDescriptions,
            UnknownNode,
            Visibility,
        )

        self.base_model: type = BaseModel
        self.registry: dict[str, type] = unions.MODEL_BY_SCHEMA_DEF
        self.unknown_node: type = UnknownNode
        self.type_descriptions: type = TypeDescriptions
        # How each schema helper definition is transcribed on the python side.
        self.ref_to_py: dict[str, Any] = {
            "SourceLocation": str,
            "TypeDescriptions": TypeDescriptions,
            "Visibility": Visibility,
            "StateMutability": StateMutability,
            "Mutability": Mutability,
            "StorageLocation": StorageLocation,
            "Expression": unions.Expression,
            "Statement": unions.Statement,
            "TypeName": unions.TypeName,
            "YulStatement": yul.YulStatement,
            "YulExpression": yul.YulExpression,
            "YulLiteral": yul.YulLiteral,
        }
        # Classes that legitimately appear inside field annotations; any other
        # BaseModel subclass found there is an inline-object helper model.
        self.known_classes: frozenset[type] = frozenset(self.registry.values()) | {
            TypeDescriptions
        }


@lru_cache(maxsize=None)
def models() -> ModelInterface:
    return ModelInterface()


# Model fields allowed to have no schema property backing them.
# certoraRun injects certora_contract_name into the dump (AstNode base field);
# the rest are the <= 0.5 dialect: InlineAssembly.operations (assembly source
# text) and the solc-0.4/0.5 FunctionDefinition flags.
# nativeSrc needs no entry because every Yul definition lists it in the schema.
FIELD_ALLOWLIST = frozenset({
    "certora_contract_name",
    "operations",
    "isConstructor",
    "isDeclaredConst",
    "payable",
    "superFunction",
})

# Definitions whose nodeType tag differs from the registry key: solc emits
# nodeType "YulLiteral" for both literal kinds, discriminated by hexValue/value.
TAG_OVERRIDES = {"YulLiteralValue": "YulLiteral", "YulLiteralHexValue": "YulLiteral"}


# ---------------------------------------------------------------------------
# Annotation flattening / atom extraction
# ---------------------------------------------------------------------------


def _flat_members(ann: Any) -> list[Any]:
    """Union members of an annotation, with Annotated wrappers (pydantic Tag /
    Discriminator metadata) and PEP 695 TypeAliasType lazily unwrapped.
    """
    while True:
        if type(ann).__name__ == "TypeAliasType" and hasattr(ann, "__value__"):
            ann = ann.__value__
            continue
        if get_origin(ann) is Annotated:
            ann = get_args(ann)[0]
            continue
        break
    if get_origin(ann) in (Union, UnionType):
        members: list[Any] = []
        for arg in get_args(ann):
            members.extend(_flat_members(arg))
        return members
    return [ann]


@dataclass
class Atoms:
    """The one-level structural content of an annotation (or of a Shape)."""

    classes: set[type] = dc_field(default_factory=set)  # known models / primitives
    helpers: set[type] = dc_field(default_factory=set)  # inline-object helper models
    literals: set[Any] = dc_field(default_factory=set)
    lists: list[Any] = dc_field(default_factory=list)  # element annotations / Shapes
    dicts: int = 0
    objects: int = 0  # expected-side marker for inline-object schemas
    has_none: bool = False
    other: list[Any] = dc_field(default_factory=list)

    def merge(self, more: "Atoms") -> None:
        self.classes |= more.classes
        self.helpers |= more.helpers
        self.literals |= more.literals
        self.lists.extend(more.lists)
        self.dicts += more.dicts
        self.objects += more.objects
        self.has_none = self.has_none or more.has_none
        self.other.extend(more.other)


def atoms_of(ann: Any, m: ModelInterface) -> Atoms:
    """Flatten a python annotation into Atoms. The deliberate extra UnknownNode
    union member is dropped (it is not part of the schema contract).
    """
    atoms = Atoms()
    for member in _flat_members(ann):
        origin = get_origin(member)
        if member is NoneType:
            atoms.has_none = True
        elif origin is Literal:
            atoms.literals |= set(get_args(member))
        elif origin is list:
            args = get_args(member)
            atoms.lists.append(args[0] if args else Any)
        elif origin is dict:
            atoms.dicts += 1
        elif isinstance(member, type):
            if member is m.unknown_node:
                pass
            elif issubclass(member, m.base_model) and member not in m.known_classes:
                atoms.helpers.add(member)
            else:
                atoms.classes.add(member)
        else:
            atoms.other.append(member)
    return atoms


def expected_atoms(shape: Shape, m: ModelInterface) -> Atoms:
    """Atoms the schema Shape demands of the annotation (nullability excluded --
    it is checked against requiredness separately).
    """
    atoms = Atoms()
    if shape.kind == "primitive":
        assert shape.py is not None
        atoms.classes.add(shape.py)
    elif shape.kind == "enum":
        atoms.literals |= set(shape.values)
    elif shape.kind == "ref":
        target = m.ref_to_py.get(shape.ref)
        if target is not None:
            resolved = atoms_of(target, m)  # alias -> literals / union members / class
            resolved.has_none = False  # alias-internal nullability is not the field's
            atoms.merge(resolved)
        elif shape.ref in m.registry:
            atoms.classes.add(m.registry[shape.ref])
        else:
            atoms.other.append(f"unmapped $ref {shape.ref}")
    elif shape.kind == "union":
        for member in shape.members:
            atoms.merge(expected_atoms(member, m))
    elif shape.kind == "array":
        atoms.lists.append(shape.item)
    elif shape.kind == "map":
        atoms.dicts += 1
    elif shape.kind == "object":
        atoms.objects += 1
    elif shape.kind == "null":
        pass
    return atoms


def _compare_atoms(actual: Atoms, expected: Atoms, where: str) -> list[str]:
    """One-level comparison; array elements are compared one level deeper, maps and
    inline objects are not recursed into.
    """
    errors: list[str] = []
    if actual.classes != expected.classes:
        errors.append(
            f"{where}: annotation classes {sorted(c.__name__ for c in actual.classes)} "
            f"!= schema {sorted(c.__name__ for c in expected.classes)}"
        )
    if actual.literals != expected.literals:
        errors.append(
            f"{where}: Literal values {sorted(map(str, actual.literals))} "
            f"!= schema enum {sorted(map(str, expected.literals))}"
        )
    if expected.objects and not (actual.helpers or actual.dicts):
        errors.append(f"{where}: schema inline object needs a helper BaseModel or dict")
    if not expected.objects and actual.helpers:
        errors.append(
            f"{where}: unexpected helper model(s) "
            f"{sorted(h.__name__ for h in actual.helpers)}"
        )
    if expected.dicts and not actual.dicts:
        errors.append(f"{where}: schema map needs a dict[...] annotation")
    if actual.dicts and not (expected.dicts or expected.objects):
        errors.append(f"{where}: unexpected dict annotation")
    if expected.other:
        errors.append(f"{where}: {expected.other}")
    if actual.other:
        errors.append(f"{where}: unrecognized annotation member(s) {actual.other}")
    return errors


def check_shape(shape: Shape, ann: Any, m: ModelInterface, where: str) -> list[str]:
    actual = atoms_of(ann, m)
    expected = expected_atoms(shape, m)
    errors = _compare_atoms(actual, expected, where)

    if expected.lists:
        if not actual.lists:
            errors.append(f"{where}: schema array needs a list[...] annotation")
        else:
            # Compare all list elements jointly, so both list[A] | list[B] and
            # list[A | B] transcriptions of an anyOf-of-arrays are accepted.
            elem_expected = Atoms()
            elem_nullable = False
            for item_shape in expected.lists:
                assert isinstance(item_shape, Shape)
                elem_expected.merge(expected_atoms(item_shape, m))
                elem_nullable = elem_nullable or item_shape.nullable
            elem_actual = Atoms()
            for elem_ann in actual.lists:
                elem_actual.merge(atoms_of(elem_ann, m))
            errors += _compare_atoms(elem_actual, elem_expected, where + " (element)")
            if elem_nullable and not elem_actual.has_none:
                errors.append(f"{where}: array items are nullable, element lacks | None")
            if elem_actual.has_none and not elem_nullable:
                errors.append(f"{where}: element allows None but schema items are not nullable")
    elif actual.lists:
        errors.append(f"{where}: unexpected list annotation")
    return errors


# ---------------------------------------------------------------------------
# Per-property check
# ---------------------------------------------------------------------------


def field_for(model: type, prop: str) -> Any:
    for field_name, info in model.model_fields.items():  # type: ignore[attr-defined]
        if field_name == prop or info.alias == prop:
            return info
    return None


# Fields deliberately WIDER than the schema, all validated against real dumps
# (fixtures for solc 0.4.26 / 0.5.17 and the project corpus sweep). Presence and
# requiredness are still checked; only the shape check is waived:
# - InlineAssembly.evmVersion/flags: version-freshness enums — a closed transcription
#   would demote every assembly-containing source on the first solc release the
#   vendored schema lags behind.
# - SourceUnit.nodes: the schema's union lacks EventDefinition, but solc >= 0.8.22
#   allows file-level events.
# - documentation on Contract/Function/Modifier/EventDefinition: plain NatSpec string
#   in dumps from solc <= 0.5 (the StructuredDocumentation node form is 0.6+).
# - ElementaryTypeNameExpression.typeName: plain string in dumps from solc <= 0.5.
# - InlineAssembly.externalReferences: solc <= 0.5 items are keyed by identifier name.
DELIBERATELY_OPEN = {
    ("InlineAssembly", "evmVersion"),
    ("InlineAssembly", "flags"),
    ("SourceUnit", "nodes"),
    ("ContractDefinition", "documentation"),
    ("FunctionDefinition", "documentation"),
    ("ModifierDefinition", "documentation"),
    ("EventDefinition", "documentation"),
    ("ElementaryTypeNameExpression", "typeName"),
    ("InlineAssembly", "externalReferences"),
}

# Schema-required fields the models default instead: solc omits them in situations
# the schema does not account for — either below the 0.6 floor (lenient-older
# policy: typed parsing must still work) or in corners the schema over-requires.
# Shape is still checked. exclude_unset round-trips keep the absence loyal.
# - ContractDefinition.abstract, {Function,Modifier}Definition.virtual,
#   FunctionCall.tryCall, VariableDeclaration.mutability: concepts added in 0.6.x.
# - FunctionDefinition.kind: added in 0.5 (0.4 uses isConstructor).
# - InlineAssembly.AST/evmVersion: absent in the <= 0.5 assembly dialect.
# - Return.functionReturnParameters: omitted for `return;` inside a modifier body.
# - MemberAccess.isLValue: omitted by solc 0.7.2 (only) on enum-member accesses —
#   a bug window, present both before and after, so it cannot be a version gate.
LENIENT_REQUIRED = {
    ("MemberAccess", "isLValue"),
    ("ContractDefinition", "abstract"),
    ("FunctionDefinition", "virtual"),
    ("FunctionDefinition", "kind"),
    ("ModifierDefinition", "virtual"),
    ("FunctionCall", "tryCall"),
    ("VariableDeclaration", "mutability"),
    ("InlineAssembly", "AST"),
    ("InlineAssembly", "evmVersion"),
    ("Return", "functionReturnParameters"),
}


def check_property(
    model: type, prop: str, spec: dict[str, Any], required: bool, m: ModelInterface
) -> list[str]:
    where = f"{model.__name__}.{prop}"
    info = field_for(model, prop)
    if info is None:
        return [f"{where}: field missing (no field named or aliased '{prop}')"]

    errors: list[str] = []
    shape = classify_prop(spec)
    nullable = shape.nullable or shape.kind == "null"
    has_none = atoms_of(info.annotation, m).has_none

    if required:
        if (model.__name__, prop) in LENIENT_REQUIRED:
            if info.is_required():
                errors.append(f"{where}: listed in LENIENT_REQUIRED but has no default")
        else:
            if not info.is_required():
                errors.append(f"{where}: schema-required but field has a default")
            if nullable and not has_none:
                errors.append(f"{where}: required-but-nullable, annotation lacks | None")
            if not nullable and has_none:
                errors.append(f"{where}: required non-nullable, annotation must not allow None")
    else:
        if info.is_required():
            errors.append(f"{where}: schema-optional but field is required")
        elif info.default is not None:
            errors.append(f"{where}: schema-optional, default must be None (got {info.default!r})")
        if not has_none:
            errors.append(f"{where}: schema-optional, annotation lacks | None")

    if prop == "nodeType":
        if get_args(info.annotation) != (shape.values[0],):
            errors.append(
                f"{where}: get_args(annotation) == {get_args(info.annotation)!r}, "
                f"expected ({shape.values[0]!r},)"
            )
    elif (model.__name__, prop) in DELIBERATELY_OPEN:
        pass
    elif shape.kind != "null":  # a pure-null property only constrains nullability
        errors += check_shape(shape, info.annotation, m, where)
    return errors


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

DEF_NAMES = sorted(node_definitions())


def test_classifier_covers_every_schema_shape() -> None:
    """Pure schema test (no models): every property shape is classifiable."""
    counts = classify_all()
    assert sum(counts.values()) == sum(
        len(d["properties"]) for d in node_definitions().values()
    )


def test_registry_coverage_both_directions() -> None:
    m = models()
    schema_names = set(node_definitions())
    model_names = set(m.registry)
    missing = schema_names - model_names
    extra = model_names - schema_names
    assert not missing and not extra, (
        f"MODEL_BY_SCHEMA_DEF mismatch: missing={sorted(missing)} extra={sorted(extra)}"
    )


@pytest.mark.parametrize("def_name", DEF_NAMES)
def test_definition_conforms_to_schema(def_name: str) -> None:
    m = models()
    model = m.registry.get(def_name)
    assert model is not None, f"no model registered for schema definition {def_name}"
    definition = node_definitions()[def_name]
    required = set(definition.get("required", []))
    errors: list[str] = []
    for prop, spec in definition["properties"].items():
        errors += check_property(model, prop, spec, prop in required, m)
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("def_name", DEF_NAMES)
def test_no_fields_beyond_schema(def_name: str) -> None:
    m = models()
    model = m.registry.get(def_name)
    assert model is not None, f"no model registered for schema definition {def_name}"
    props = set(node_definitions()[def_name]["properties"])
    stray = [
        info.alias or field_name
        for field_name, info in model.model_fields.items()
        if (info.alias or field_name) not in props
        and (info.alias or field_name) not in FIELD_ALLOWLIST
    ]
    assert not stray, f"{model.__name__}: fields with no schema property: {sorted(stray)}"


def test_node_type_tags_match_registry_keys() -> None:
    m = models()
    errors: list[str] = []
    for def_name, model in m.registry.items():
        expected_tag = TAG_OVERRIDES.get(def_name, def_name)
        info = model.model_fields.get("nodeType")
        if info is None:
            errors.append(f"{def_name}: model {model.__name__} has no nodeType field")
            continue
        tags = get_args(info.annotation)
        if tags != (expected_tag,):
            errors.append(f"{def_name}: nodeType Literal {tags!r} != ({expected_tag!r},)")
    assert not errors, "\n".join(errors)
