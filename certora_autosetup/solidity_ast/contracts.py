"""Contract declarations and their kind, from either shape of solc AST.

:class:`ContractDeclView` is the uniform view. :func:`iter_contract_declarations` reads it
from the typed models over an ``AstDump`` stream. :func:`parse_only_declarations` reads it
from the raw nodes of a ``stopAfter: "parsing"`` AST, which the typed models reject: solc
emits no analysis-phase fields (``scope``, ``linearizedBaseContracts``,
``fullyImplemented``) at that stage.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

from certora_autosetup.utils.types import ContractKind

from .declarations import ContractDefinition
from .loader import FileAsts, SourceAst, iter_nodes_of_type


@dataclass(frozen=True)
class ContractDeclView:
    """Uniform view of a ContractDefinition for declaration/inheritance scans, whether
    it came from the typed AST or from raw nodes the models could not reach."""

    source_path: str
    node_id: Optional[int]
    name: str
    abstract: bool
    contract_kind: ContractKind
    linearized_base_ids: list[int]


def to_contract_kind(value: Any) -> ContractKind:
    """``contractKind`` as an enum. Anything solc did not give us is UNKNOWN."""
    return ContractKind(value) if value in {kind.value for kind in ContractKind} else ContractKind.UNKNOWN


def iter_contract_declarations(units: Iterable[FileAsts]) -> Iterator[ContractDeclView]:
    """Every contract declaration in a stream of compilation units (typically
    ``AstDump.stream_units(...)``, so the multi-GB dump is never fully in memory):
    typed where the models parsed, completed by a raw flat-map sweep for anything
    the typed walk could not reach (a solc surprise cannot hide contracts from
    setup; Vyper sources contribute nothing — no ContractDefinition nodes)."""
    for file_asts in units:
        for source in file_asts.sources.values():
            yield from unit_contract_declarations(source)


def unit_contract_declarations(source: SourceAst) -> Iterator[ContractDeclView]:
    for node in iter_nodes_of_type(source, ContractDefinition):
        yield _view(node, source.source_path)


def parse_only_declarations(ast: dict, source_path: str) -> Iterator[ContractDeclView]:
    """Declarations in a ``stopAfter: "parsing"`` AST node, which stays raw (see module
    docstring). Solidity declares contracts only at file scope, so the top level is all
    of them."""
    for node in ast.get("nodes", []):
        if isinstance(node, dict) and node.get("nodeType") == "ContractDefinition":
            yield _view(node, source_path)


def _view(node: ContractDefinition | dict[str, Any], source_path: str) -> ContractDeclView:
    if isinstance(node, ContractDefinition):
        return ContractDeclView(
            source_path=source_path,
            node_id=node.id,
            name=node.name,
            abstract=node.abstract,
            contract_kind=to_contract_kind(node.contractKind),
            linearized_base_ids=list(node.linearizedBaseContracts),
        )
    return ContractDeclView(
        source_path=source_path,
        node_id=node.get("id"),
        name=node.get("name") or "",
        abstract=bool(node.get("abstract", False)),
        contract_kind=to_contract_kind(node.get("contractKind")),
        linearized_base_ids=[i for i in node.get("linearizedBaseContracts", []) if isinstance(i, int)],
    )
