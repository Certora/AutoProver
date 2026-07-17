"""AnalysisContext: the one object every signal receives.

Wraps one or more AstDumps (a project may have per-contract dumps), deduplicates
sources across them, separates project code from dependencies, and maps AST byte
offsets to source lines for evidence.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from certora_autosetup.solidity_ast import (
    AstDump,
    ContractDefinition,
    FunctionDefinition,
    SourceUnit,
    find_all,
)

# Path fragments that mark a source as a dependency rather than project code.
# Signals score project code; dependencies still participate where they must
# (e.g. curated-summary library detection).
DEPENDENCY_MARKERS = ("node_modules/", "lib/", "dependencies/", ".deps/")


def is_dependency_path(source_path: str) -> bool:
    p = source_path.lstrip("./")
    return any(p.startswith(m) or f"/{m}" in p for m in DEPENDENCY_MARKERS)


@dataclass
class AnalysisContext:
    project_root: Path
    dumps: list[AstDump]
    _sources: dict[str, SourceUnit] = field(default_factory=dict, init=False)
    _line_tables: dict[str, list[int]] = field(default_factory=dict, init=False)
    _source_texts: dict[str, Optional[bytes]] = field(default_factory=dict, init=False)
    unparsed_source_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        for dump in self.dumps:
            for _, source in dump.iter_sources():
                if source.root is None:
                    self.unparsed_source_count += 1
                elif source.source_path not in self._sources:
                    self._sources[source.source_path] = source.root

    # ---- source iteration -------------------------------------------------

    def iter_sources(self, *, include_dependencies: bool = False) -> Iterator[tuple[str, SourceUnit]]:
        for path, root in self._sources.items():
            if include_dependencies or not is_dependency_path(path):
                yield path, root

    def iter_contracts(
        self, *, include_dependencies: bool = False
    ) -> Iterator[tuple[str, ContractDefinition]]:
        for path, root in self.iter_sources(include_dependencies=include_dependencies):
            for contract in find_all(root, ContractDefinition):
                yield path, contract

    def iter_functions(
        self, *, implemented_only: bool = True, include_dependencies: bool = False
    ) -> Iterator[tuple[str, ContractDefinition, FunctionDefinition]]:
        """(source_path, contract, function) over contract members (not free functions)."""
        for path, contract in self.iter_contracts(include_dependencies=include_dependencies):
            for node in contract.nodes:
                if isinstance(node, FunctionDefinition) and (node.implemented or not implemented_only):
                    yield path, contract, node

    # ---- source text / line mapping --------------------------------------

    def display_path(self, source_path: str) -> str:
        """Project-relative path for reports (falls back to the raw dump path)."""
        try:
            return str(Path(source_path).resolve().relative_to(self.project_root.resolve()))
        except ValueError:
            return source_path

    def _text(self, source_path: str) -> Optional[bytes]:
        if source_path not in self._source_texts:
            candidate = self.project_root / source_path
            self._source_texts[source_path] = (
                candidate.read_bytes() if candidate.is_file() else None
            )
        return self._source_texts[source_path]

    def offset_to_line(self, source_path: str, byte_offset: int) -> int:
        """1-based line for a solc src byte offset; 0 when the source file is
        unavailable (evidence stays usable, just without a line anchor)."""
        text = self._text(source_path)
        if text is None:
            return 0
        if source_path not in self._line_tables:
            starts = [0]
            for i, b in enumerate(text):
                if b == 0x0A:
                    starts.append(i + 1)
            self._line_tables[source_path] = starts
        starts = self._line_tables[source_path]
        # binary search: number of line starts <= offset
        lo, hi = 0, len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= byte_offset:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def line_span(self, source_path: str, byte_offset: int, byte_length: int) -> int:
        """Number of source lines a node covers (0 when the source is unavailable)."""
        text = self._text(source_path)
        if text is None:
            return 0
        return text[byte_offset : byte_offset + byte_length].count(b"\n") + 1
