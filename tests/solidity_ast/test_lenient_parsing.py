"""Corpus-discovered leniency cases: shapes real solc emits that the vendored schema
does not account for (see LENIENT_REQUIRED / DELIBERATELY_OPEN in the conformance
test). Each must parse typed — not as UnknownNode, not as a parse failure — and
round-trip without inventing the absent fields. When the producing solc version is
known, VERSION_GATES turns illegitimate absence into a failure instead of a None."""

import pytest

from certora_autosetup.solidity_ast import (
    AstDump,
    ContractDefinition,
    EventDefinition,
    FunctionDefinition,
    Identifier,
    ImportDirective,
    Return,
    SourceUnit,
    VariableDeclaration,
)


def test_pre_06_contract_without_abstract_parses() -> None:
    contract = ContractDefinition.model_validate(
        {
            "id": 1, "src": "0:10:0", "nodeType": "ContractDefinition",
            "name": "C", "baseContracts": [], "contractDependencies": [],
            "contractKind": "contract", "fullyImplemented": True,
            "linearizedBaseContracts": [1], "nodes": [], "scope": 0,
        }
    )
    assert contract.abstract is False
    assert "abstract" not in contract.model_dump(exclude_unset=True)


def test_return_inside_modifier_body_parses() -> None:
    ret = Return.model_validate({"id": 2, "src": "0:7:0", "nodeType": "Return"})
    assert ret.functionReturnParameters is None
    assert "functionReturnParameters" not in ret.model_dump(exclude_unset=True)


_BARE_CONTRACT = {
    "id": 1, "src": "0:10:0", "nodeType": "ContractDefinition",
    "name": "C", "baseContracts": [], "contractDependencies": [],
    "contractKind": "contract", "fullyImplemented": True,
    "linearizedBaseContracts": [1], "nodes": [], "scope": 0,
}


def _dump_with(node: dict) -> dict:
    return {"a.sol": {"a.sol": {"1": {
        "id": 0, "src": "0:10:0", "nodeType": "SourceUnit",
        "absolutePath": "a.sol", "exportedSymbols": {}, "nodes": [node],
    }}}}


def test_version_gate_fails_absent_field_at_or_above_gate() -> None:
    data = _dump_with(dict(_BARE_CONTRACT))  # no `abstract` (gate: 0.6.0)
    with pytest.raises(ValueError, match="version-gate violation.*abstract"):
        AstDump.from_dict(data, on_error="raise", solc_version="0.8.30")

    dump = AstDump.from_dict(data, on_error="raw", solc_version="0.8.30")
    [(_, source)] = list(dump.iter_sources())
    assert source.raw_kind == "parse_failed" and "abstract" in (source.parse_error or "")


def test_version_gate_allows_absence_below_gate() -> None:
    data = _dump_with(dict(_BARE_CONTRACT))
    dump = AstDump.from_dict(data, on_error="raise", solc_version="0.5.17")
    [(_, source)] = list(dump.iter_sources())
    assert source.is_parsed


def test_version_gate_off_without_version() -> None:
    dump = AstDump.from_dict(_dump_with(dict(_BARE_CONTRACT)), on_error="raise")
    [(_, source)] = list(dump.iter_sources())
    assert source.is_parsed


def test_effective_mutability_derives_from_constant() -> None:
    def var(constant: bool) -> VariableDeclaration:
        return VariableDeclaration.model_validate({
            "id": 5, "src": "0:1:0", "nodeType": "VariableDeclaration",
            "name": "x", "constant": constant, "scope": 1, "stateVariable": True,
            "storageLocation": "default", "typeDescriptions": {}, "visibility": "internal",
        })

    assert var(True).mutability is None and var(True).effective_mutability == "constant"
    assert var(False).effective_mutability == "mutable"


def test_effective_kind_derives_from_04_flags() -> None:
    def fn(name: str, is_constructor: bool) -> FunctionDefinition:
        return FunctionDefinition.model_validate({
            "id": 6, "src": "0:1:0", "nodeType": "FunctionDefinition",
            "name": name, "implemented": True, "isConstructor": is_constructor,
            "modifiers": [], "scope": 1, "stateMutability": "nonpayable",
            "visibility": "public",
            "parameters": {"id": 7, "src": "0:0:0", "nodeType": "ParameterList", "parameters": []},
            "returnParameters": {"id": 8, "src": "0:0:0", "nodeType": "ParameterList", "parameters": []},
        })

    assert fn("f", False).kind is None and fn("f", False).effective_kind == "function"
    assert fn("", True).effective_kind == "constructor"
    assert fn("", False).effective_kind == "fallback"
    assert fn("f", False).virtual is None


def test_file_level_event_definition_is_typed() -> None:
    unit = SourceUnit.model_validate(
        {
            "id": 10, "src": "0:50:0", "nodeType": "SourceUnit",
            "absolutePath": "a.sol", "exportedSymbols": {},
            "nodes": [{
                "id": 11, "src": "0:20:0", "nodeType": "EventDefinition",
                "name": "E", "anonymous": False,
                "parameters": {"id": 12, "src": "0:0:0", "nodeType": "ParameterList",
                               "parameters": []},
            }],
        }
    )
    [event] = unit.nodes
    assert isinstance(event, EventDefinition)


def test_pre_06_symbol_alias_foreign_is_a_bare_declaration_id() -> None:
    # solc writes an Identifier node from 0.6.1 on; 0.4 and 0.5 write the id of
    # the declaration being imported. Whole compilation units used to fall back
    # to an untyped AST over this one field.
    directive = ImportDirective.model_validate(
        {
            "id": 4, "src": "0:30:0", "nodeType": "ImportDirective",
            "absolutePath": "contracts/B.sol", "file": "./B.sol",
            "scope": 20, "sourceUnit": 19, "unitAlias": "",
            "symbolAliases": [{"foreign": 3, "local": None}],
        }
    )
    assert directive.symbolAliases[0].foreign == 3


def test_symbol_alias_foreign_is_still_typed_from_06_on() -> None:
    directive = ImportDirective.model_validate(
        {
            "id": 4, "src": "0:30:0", "nodeType": "ImportDirective",
            "absolutePath": "contracts/B.sol", "file": "./B.sol",
            "scope": 20, "sourceUnit": 19, "unitAlias": "",
            "symbolAliases": [
                {
                    "foreign": {
                        "id": 3, "src": "9:1:0", "nodeType": "Identifier",
                        "name": "B", "overloadedDeclarations": [],
                        "typeDescriptions": {},
                    },
                    "nameLocation": "-1:-1:-1",
                }
            ],
        }
    )
    foreign = directive.symbolAliases[0].foreign
    assert isinstance(foreign, Identifier) and foreign.name == "B"
