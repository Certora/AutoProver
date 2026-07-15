"""Traversal semantics: order, transparency of helper containers, parent maps."""

import json
from pathlib import Path

from certora_autosetup.solidity_ast import (
    AstDump,
    ContractDefinition,
    FunctionDefinition,
    IdentifierPath,
    SourceUnit,
    UsingForDirective,
    build_parent_map,
    find_all,
    iter_children,
    walk,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "solidity_ast"


def _root_08() -> SourceUnit:
    dump = AstDump.load(FIXTURES / "solc_0_8_30.asts.json", on_error="raise")
    [root] = [root for _, _, root in dump.iter_parsed_roots()]
    return root


def test_walk_is_preorder_document_order() -> None:
    root = _root_08()
    nodes = list(walk(root))
    assert nodes[0] is root
    # document order: src offsets of the top-level nodes are non-decreasing
    offsets = [n.src_location.offset for n in iter_children(root)]
    assert offsets == sorted(offsets)


def test_find_all_includes_matching_root() -> None:
    root = _root_08()
    contract = next(find_all(root, ContractDefinition))
    assert next(find_all(contract, ContractDefinition)) is contract


def test_helper_containers_are_transparent() -> None:
    # `using {addPrice as +, ...} for Price global`: the IdentifierPath nodes live
    # inside functionList helper objects and must still be reachable.
    root = _root_08()
    directives = [u for u in find_all(root, UsingForDirective) if u.functionList]
    assert directives
    paths = [p for u in directives for p in find_all(u, IdentifierPath)]
    assert {getattr(p, "name") for p in paths} >= {"addPrice", "eqPrice"}


def test_parent_map_matches_containment() -> None:
    root = _root_08()
    parent_map = build_parent_map(root)
    contract = next(find_all(root, ContractDefinition))
    function = next(find_all(contract, FunctionDefinition))
    # walk up from the function; must reach the contract, then the root (no parent)
    seen = set()
    node_id = function.id
    while node_id in parent_map:
        node_id = parent_map[node_id]
        assert node_id not in seen, "cycle in parent map"
        seen.add(node_id)
    assert node_id == root.id
    assert contract.id in seen


def test_parent_map_ids_exist_in_index() -> None:
    dump = AstDump.load(FIXTURES / "solc_0_8_30.asts.json", on_error="raise")
    for _, source in dump.iter_sources():
        assert source.root is not None
        parent_map = build_parent_map(source.root)
        assert set(parent_map) <= set(source.nodes)
        assert set(parent_map.values()) <= set(source.nodes)
