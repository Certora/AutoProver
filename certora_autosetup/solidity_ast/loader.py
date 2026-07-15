"""Typed loader for the ``.asts.json`` dump written by ``certoraRun --dump_asts``.

The dump is a three-level dict ``{original_file: {source_file: {node_id_str: node}}}``:
the outer key is the contract file the compiler was invoked on, the middle key is every
source in that compilation unit, and the inner map is a flat id-index whose values are
the same nodes that also appear nested inside their parents. The loader therefore
validates each source's SourceUnit tree exactly once and derives the id-index by
traversal, keeping the raw flat map alongside for fallback and byte-compatible uses.

Degradation policy (a project that compiles must never fail because of this loader):
an unrecognized ``nodeType`` becomes an ``UnknownNode`` inside an otherwise-typed tree;
a source whose shape the models reject entirely is kept raw as ``parse_failed``;
Vyper sources (``ast_type``/``node_id`` dialect) are kept raw as ``vyper``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Literal, TypeVar, get_args

from pydantic import ValidationError

from . import unions as unions  # import resolves forward refs and rebuilds the models
from .base import AstNode
from .declarations import SourceUnit
from .traversal import build_node_index, find_all

logger = logging.getLogger(__name__)

RawKind = Literal["solidity", "vyper", "parse_failed"]
OnError = Literal["raw", "raise"]


@dataclass
class SourceAst:
    """One source file's AST within a compilation unit."""

    source_path: str
    root: SourceUnit | None
    nodes: dict[int, AstNode] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    raw_kind: RawKind = "solidity"
    parse_error: str | None = None

    @property
    def is_parsed(self) -> bool:
        return self.root is not None


@dataclass
class FileAsts:
    """All source ASTs of one compilation unit (one outer key of the dump)."""

    original_file: str
    sources: dict[str, SourceAst]


@dataclass
class AstDump:
    """The full, typed view of a ``.asts.json`` dump."""

    files: dict[str, FileAsts]

    @classmethod
    def load(cls, path: Path | str, *, on_error: OnError = "raw") -> "AstDump":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f), on_error=on_error)

    @classmethod
    def load_cached(cls, path: Path | str, *, on_error: OnError = "raw") -> "AstDump":
        """``load()`` memoized on (resolved path, mtime, size) — a setup run reads the
        same dump at several points. The returned instance is shared: treat it as
        read-only.
        """
        stat = os.stat(path)
        return _load_cached(str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size, on_error)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, on_error: OnError = "raw") -> "AstDump":
        files = {
            original_file: FileAsts(
                original_file=original_file,
                sources={
                    source_path: _load_source(source_path, flat, on_error)
                    for source_path, flat in per_source.items()
                },
            )
            for original_file, per_source in data.items()
        }
        return cls(files=files)

    def iter_sources(self) -> Iterator[tuple[str, SourceAst]]:
        """(original_file, SourceAst) over every source, including vyper/failed ones."""
        for file_asts in self.files.values():
            for source in file_asts.sources.values():
                yield file_asts.original_file, source

    def iter_parsed_roots(self) -> Iterator[tuple[str, str, SourceUnit]]:
        """(original_file, source_path, SourceUnit) over successfully parsed sources."""
        for original_file, source in self.iter_sources():
            if source.root is not None:
                yield original_file, source.source_path, source.root

    def find_node(self, source_path: str, node_id: int) -> AstNode | None:
        for file_asts in self.files.values():
            source = file_asts.sources.get(source_path)
            if source is not None and node_id in source.nodes:
                return source.nodes[node_id]
        return None


N = TypeVar("N", bound=AstNode)


def iter_nodes_of_type(source: SourceAst, model: type[N]) -> Iterator[N | dict[str, Any]]:
    """All nodes of one concrete model type in a source: typed instances from the
    parsed tree first, then the raw flat-map dicts of matching nodeType that the
    typed walk did not reach (nested under an UnknownNode, or the whole source
    unparsable). Gives exact-parity coverage with a raw flat-map scan while staying
    typed wherever the models reached; callers must accept both shapes.
    """
    (node_type,) = get_args(model.model_fields["nodeType"].annotation)
    seen: set[int] = set()
    if source.root is not None:
        for node in find_all(source.root, model):
            node_id = getattr(node, "id", None)  # Yul models carry no id
            if isinstance(node_id, int):
                seen.add(node_id)
            yield node
    for raw_node in source.raw.values():
        if (
            isinstance(raw_node, dict)
            and raw_node.get("nodeType") == node_type
            and raw_node.get("id") not in seen
        ):
            yield raw_node


@lru_cache(maxsize=8)
def _load_cached(resolved_path: str, _mtime_ns: int, _size: int, on_error: OnError) -> AstDump:
    return AstDump.load(resolved_path, on_error=on_error)


def _load_source(source_path: str, flat: dict[str, Any], on_error: OnError) -> SourceAst:
    node_dicts = [n for n in flat.values() if isinstance(n, dict)]

    has_solidity = any("nodeType" in n for n in node_dicts)
    has_vyper = any("nodeType" not in n and ("ast_type" in n or "node_id" in n) for n in node_dicts)
    if has_vyper and not has_solidity:
        return SourceAst(source_path=source_path, root=None, raw=flat, raw_kind="vyper")

    roots = [n for n in node_dicts if n.get("nodeType") == "SourceUnit"]
    if len(roots) != 1:
        return _failed(
            source_path, flat, f"expected exactly one SourceUnit node, found {len(roots)}", on_error
        )

    try:
        root = SourceUnit.model_validate(roots[0])
    except ValidationError as e:
        if on_error == "raise":
            raise
        return _failed(
            source_path, flat, f"{e.error_count()} validation error(s): {e.errors()[0]}", on_error
        )

    nodes = build_node_index(root)
    missing = [i for i in flat if i.isdigit() and int(i) not in nodes]
    if missing:
        logger.debug(
            "%s: %d raw index ids not reached by typed traversal (first: %s)",
            source_path, len(missing), missing[0],
        )
    return SourceAst(source_path=source_path, root=root, nodes=nodes, raw=flat)


def _failed(source_path: str, flat: dict[str, Any], msg: str, on_error: OnError) -> SourceAst:
    if on_error == "raise":
        raise ValueError(f"failed to parse AST of {source_path}: {msg}")
    logger.warning("falling back to raw AST for %s: %s", source_path, msg)
    return SourceAst(
        source_path=source_path, root=None, raw=flat, raw_kind="parse_failed", parse_error=msg
    )
