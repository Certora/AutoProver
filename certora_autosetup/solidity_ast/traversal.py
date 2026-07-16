"""Traversal utilities over typed AST nodes, plus the legacy raw parent-graph builder."""

from __future__ import annotations

from typing import Any, Iterable, Iterator, TypeVar, cast

from pydantic import BaseModel

from .base import AstNode, SolcNode

N = TypeVar("N", bound=AstNode)


def iter_children(node: AstNode) -> Iterator[AstNode]:
    """Direct AST children of a node, in model-field declaration order.

    Helper models that are not themselves AST nodes (e.g. import symbol aliases,
    ``using``-directive function lists) are transparent containers: AST nodes found
    inside them are yielded as direct children of ``node``. Extra fields captured by
    ``model_extra`` (unknown to the model set) are not descended into.
    """
    for name in type(node).model_fields:
        yield from _child_nodes(getattr(node, name))


def _child_nodes(value: Any) -> Iterator[AstNode]:
    if isinstance(value, AstNode):
        yield value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _child_nodes(getattr(value, name))
    elif isinstance(value, list):
        for item in value:
            yield from _child_nodes(item)


def walk(node: AstNode) -> Iterator[AstNode]:
    """Pre-order DFS over ``node`` and all its descendants (iterative — deep
    expression chains cannot hit the interpreter recursion limit)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(list(iter_children(current))))


def find_all(root: AstNode, node_type: type[N] | tuple[type[N], ...]) -> Iterator[N]:
    """All nodes of the given type(s) in the subtree rooted at ``root`` (inclusive),
    in document order. The typed replacement for ``nodeType == "X"`` scans."""
    for node in walk(root):
        if isinstance(node, node_type):
            yield node


def build_node_index(root: AstNode) -> dict[int, AstNode]:
    """Map every id-carrying node in the subtree to its instance (Yul nodes carry
    no id and are not indexed)."""
    return {node.id: node for node in walk(root) if isinstance(node, SolcNode)}


def build_parent_map(root: AstNode) -> dict[int, int]:
    """Map child node id -> parent node id over the subtree, for id-carrying nodes.

    Nodes nested inside transparent helper containers are attached to the nearest
    id-carrying AST ancestor.
    """
    parent_map: dict[int, int] = {}
    stack: list[tuple[AstNode, int | None]] = [(root, None)]
    while stack:
        node, parent_id = stack.pop()
        node_id = node.id if isinstance(node, SolcNode) else None
        if node_id is not None and parent_id is not None:
            parent_map[node_id] = parent_id
        enclosing = node_id if node_id is not None else parent_id
        stack.extend((child, enclosing) for child in iter_children(node))
    return parent_map


def build_parent_graph_json(
    raw_asts: dict[str, Any] | Iterable[tuple[str, Any]],
) -> dict[str, dict[str, dict[str, str]]]:
    """Parent graph over RAW ``.asts.json`` data (a full dict, or streamed
    ``(relative_path, path_data)`` pairs from ``loader.stream_raw_units``), in the
    exact legacy format written to ``all_ast_parent_graph.json``:
    {rel_path: {abs_path: {child_id: parent_id}}} with string ids.

    Deliberately operates on the raw dicts with the historical child heuristic (a child
    is any direct dict value, or list element, carrying an ``id`` key) and preserves
    raw key order, so ``json.dump(..., indent=2)`` output stays byte-identical to what
    existing readers of the file expect. Use :func:`build_parent_map` for typed code.
    """
    units: Iterable[tuple[str, Any]]
    if isinstance(raw_asts, dict):
        units = cast("Iterable[tuple[str, Any]]", raw_asts.items())
    else:
        units = raw_asts
    parent_graph: dict[str, dict[str, dict[str, str]]] = {}
    for relative_path, path_data in units:
        parent_graph[relative_path] = {}
        for absolute_path, nodes in path_data.items():
            parent_graph[relative_path][absolute_path] = {}
            for node_id, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                for child_id in _legacy_child_ids(node):
                    parent_graph[relative_path][absolute_path][str(child_id)] = str(node_id)
    return parent_graph


def _legacy_child_ids(node: dict[str, Any]) -> list[Any]:
    child_ids = []
    for value in node.values():
        if isinstance(value, dict) and "id" in value:
            child_ids.append(value["id"])
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "id" in item:
                    child_ids.append(item["id"])
    return child_ids
