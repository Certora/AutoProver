"""Corpus-discovered leniency cases: shapes real solc emits that the vendored schema
does not account for (see LENIENT_REQUIRED / DELIBERATELY_OPEN in the conformance
test). Each must parse typed — not as UnknownNode, not as a parse failure — and
round-trip without inventing the absent fields."""

from certora_autosetup.solidity_ast import ContractDefinition, EventDefinition, Return, SourceUnit


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
