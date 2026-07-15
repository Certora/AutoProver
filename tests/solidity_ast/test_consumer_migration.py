"""Parity of the migrated AST consumers with the legacy raw-dict algorithms.

Each test re-implements the pre-migration algorithm inline (frozen) and asserts the
typed replacement produces identical results on the real fixtures.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from certora_autosetup.setup.auto_munges import (
    CODE_ACCESS_PATCH_FILE,
    _iter_code_accesses,
    detect_code_accesses,
)
from certora_autosetup.setup.setup_prover import _iter_contract_declarations
from certora_autosetup.solidity_ast import AstDump
from certora_autosetup.utils.scope import Scope

FIXTURES = Path(__file__).parent.parent / "fixtures" / "solidity_ast"
SOLC_FIXTURES = ["solc_0_4_26", "solc_0_5_17", "solc_0_6_12", "solc_0_7_6", "solc_0_8_30"]


def _load_raw(name: str) -> dict[str, Any]:
    with open(FIXTURES / f"{name}.asts.json") as f:
        return json.load(f)


def _legacy_inheritance(asts: dict[str, Any]) -> tuple[dict[str, list[str]], set[str]]:
    inheritance_info: dict[str, list[str]] = {}
    abstract_contracts: set[str] = set()
    id_to_name = {}
    for abs_path_dict in asts.values():
        for nodes in abs_path_dict.values():
            for node in nodes.values():
                if node.get("nodeType") == "ContractDefinition":
                    if node.get("id") and node.get("name"):
                        id_to_name[node["id"]] = node["name"]
    for abs_path_dict in asts.values():
        for nodes in abs_path_dict.values():
            for node in nodes.values():
                if node.get("nodeType") == "ContractDefinition" and node.get("name"):
                    if node.get("abstract", False) or node.get("contractKind", "contract") == "interface":
                        abstract_contracts.add(node["name"])
                    linearized = node.get("linearizedBaseContracts", [])
                    if len(linearized) > 1:
                        bases = [id_to_name[i] for i in linearized[1:] if i in id_to_name]
                        if bases:
                            inheritance_info[node["name"]] = bases
    return inheritance_info, abstract_contracts


@pytest.mark.parametrize("fixture", SOLC_FIXTURES)
def test_inheritance_extraction_parity(fixture: str) -> None:
    raw = _load_raw(fixture)
    expected_inheritance, expected_abstract = _legacy_inheritance(raw)

    declarations = list(_iter_contract_declarations(AstDump.from_dict(raw)))
    id_to_name = {d.node_id: d.name for d in declarations if d.node_id and d.name}
    inheritance: dict[str, list[str]] = {}
    abstract: set[str] = set()
    for decl in declarations:
        if not decl.name:
            continue
        if decl.abstract or decl.contract_kind == "interface":
            abstract.add(decl.name)
        if len(decl.linearized_base_ids) > 1:
            bases = [id_to_name[i] for i in decl.linearized_base_ids[1:] if i in id_to_name]
            if bases:
                inheritance[decl.name] = bases

    assert inheritance == expected_inheritance
    assert abstract == expected_abstract
    assert expected_inheritance  # fixtures must actually exercise inheritance


@pytest.mark.parametrize("fixture", SOLC_FIXTURES)
def test_declared_contracts_parity(fixture: str) -> None:
    raw = _load_raw(fixture)
    expected: dict[str, set[str]] = {}
    for abs_path_dict in raw.values():
        for abs_path, nodes in abs_path_dict.items():
            for node in nodes.values():
                if (
                    node.get("nodeType") == "ContractDefinition"
                    and node.get("contractKind") != "interface"
                    and node.get("name")
                ):
                    expected.setdefault(abs_path, set()).add(node["name"])

    actual: dict[str, set[str]] = {}
    for decl in _iter_contract_declarations(AstDump.from_dict(raw)):
        if decl.contract_kind != "interface" and decl.name:
            actual.setdefault(decl.source_path, set()).add(decl.name)

    assert actual == expected


def _code_access_ids(dump: AstDump) -> set[str]:
    return {
        c.node_id for _, source in dump.iter_sources() for c in _iter_code_accesses(source)
    }


def test_code_access_survives_unknown_ancestor() -> None:
    """A future-solc nodeType enclosing a .code access must not hide it: the typed
    walk stops at the UnknownNode, and the raw flat-map sweep must pick it up."""
    raw = _load_raw("solc_0_8_30")
    baseline = _code_access_ids(AstDump.from_dict(raw))
    assert baseline

    # Rename the nodeType of every enclosing statement of a .code access (its parent
    # in the flat map heuristic: any node holding the access as a direct child).
    mutated = json.loads(json.dumps(raw))
    for per_source in mutated.values():
        for nodes in per_source.values():
            access_ids = {
                node["id"]
                for node in nodes.values()
                if isinstance(node, dict)
                and node.get("nodeType") == "MemberAccess"
                and node.get("memberName") == "code"
            }
            for node in nodes.values():
                if not isinstance(node, dict) or node.get("nodeType") == "MemberAccess":
                    continue
                children = [
                    v for v in node.values() if isinstance(v, dict) and "id" in v
                ] + [
                    item
                    for v in node.values()
                    if isinstance(v, list)
                    for item in v
                    if isinstance(item, dict) and "id" in item
                ]
                if any(c["id"] in access_ids for c in children):
                    node["nodeType"] = "SolcFutureStatement"

    mutated_dump = AstDump.from_dict(mutated)
    assert all(s.is_parsed for _, s in mutated_dump.iter_sources())
    assert _code_access_ids(mutated_dump) == baseline


def test_patch_targets_the_containing_source_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .code access in an imported file compiled under another file's compilation
    unit must be patched in the imported file, not the unit's main file."""
    project = tmp_path / "project"
    project.mkdir()
    lib_text = "address a; a.code;"  # offsets below point into this text
    (project / "lib.sol").write_text(lib_text)
    (project / "main.sol").write_text("// no .code accesses here\n" * 4)

    code_off = lib_text.index("a.code")
    member_access = {
        "id": 7,
        "src": f"{code_off}:6:1",
        "nodeType": "MemberAccess",
        "memberName": "code",
        "expression": {"id": 6, "src": f"{code_off}:1:1", "nodeType": "Identifier", "name": "a"},
    }
    source_unit = {
        "id": 99, "src": "0:0:1", "nodeType": "SourceUnit",
        "absolutePath": "lib.sol", "exportedSymbols": {}, "nodes": [],
    }
    dump = {
        "main.sol": {
            "main.sol": {"1": {
                "id": 1, "src": "0:0:0", "nodeType": "SourceUnit",
                "absolutePath": "main.sol", "exportedSymbols": {}, "nodes": [],
            }},
            "lib.sol": {"99": source_unit, "7": member_access},
        }
    }
    ast_path = tmp_path / "asts.json"
    ast_path.write_text(json.dumps(dump))

    monkeypatch.chdir(project)
    detect_code_accesses(lambda *a, **k: None, ast_path, tmp_path / "no_graph.json", Scope(project))

    patches = json.loads((project / CODE_ACCESS_PATCH_FILE).read_text())
    assert [p["file"] for p in patches] == ["lib.sol"]
    assert patches[0]["original"] == "a.code"
    assert patches[0]["replacement"] == "certora_loadCode(a)"


def test_detect_code_accesses_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the migrated detect_code_accesses on the 0.8.30 fixture and check the
    patch file targets the exact `probe.code` byte range of the source."""
    contracts = FIXTURES / "contracts"
    project = tmp_path / "project"
    project.mkdir()
    source_name = "breadth_08.sol"
    (project / source_name).write_bytes((contracts / source_name).read_bytes())
    (project / "dummy.spec").write_text("")

    ast_path = tmp_path / "asts.json"
    ast_path.write_bytes((FIXTURES / "solc_0_8_30.asts.json").read_bytes())

    monkeypatch.chdir(project)
    messages: list[str] = []
    detect_code_accesses(
        lambda msg, level="INFO": messages.append(f"{level}: {msg}"),
        ast_path,
        tmp_path / "no_graph.json",  # absent: exercises the manual chain-check fallback
        Scope(project),
    )

    patch_file = project / CODE_ACCESS_PATCH_FILE
    assert patch_file.exists(), messages
    patches = json.loads(patch_file.read_text())
    assert patches, messages

    source_text = (project / source_name).read_text()
    for patch in patches:
        assert patch["file"] == source_name
        original = source_text[patch["offset"] : patch["offset"] + patch["length"]]
        assert original == patch["original"]
        assert original.endswith(".code")
        assert patch["replacement"] == f"certora_loadCode({original[: -len('.code')]})"
    # .code.length accesses must have been skipped as chained
    assert all(".code.length" not in p["original"] for p in patches)
