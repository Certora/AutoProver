"""
AST-driven detection of native value transfers: ``send``, ``transfer``, and
``.call{value: ...}("")``.

The prover can never resolve these calls: they carry no function selector, so
neither storage-path linking nor DISPATCHER summaries apply. The call HAVOCs, and
a havoced call can always be assumed to revert — which makes every caller
revertible on all paths (the vacuous-rule failure mode). Call resolution consults
the sites found here when deciding whether to recommend ``optimistic_fallback``,
the prover flag that assumes unresolved *empty-input-buffer* calls succeed.

Detection walks the flattened solc ASTs the Certora build already produces
(``.certora_internal/all_asts.json``). Classification relies exclusively on AST
node properties — ``nodeType`` / ``memberName`` / ``typeIdentifier`` equality and
call-option membership — never on matching source-text snippets.

AST dump structure (written by ``certoraBuild.collect_asts``)::

    {compilation_unit_file: {source_file: {node_id: node}}}

Every node is flattened into the per-source-file map (children remain inline) and
is stamped with ``certora_contract_name`` — the enclosing ``ContractDefinition``'s
name. That stamp is the join key against the verification scene: code inherited
from a base contract is stamped with the *base*'s name, so callers must pass the
scene's contracts together with their inheritance ancestors.
"""

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Set, Tuple

from certora_autosetup.utils.types import ContractName

# The certoraBuild stamp identifying the ContractDefinition a node belongs to.
CERTORA_CONTRACT_NAME_KEY = "certora_contract_name"

# solc typeIdentifiers of expressions whose members `send`/`transfer`/`call` are the
# EVM builtins (a `transfer` member on e.g. a `t_contract$...` expression is a plain
# external function and is excluded by this check).
_ADDRESS_TYPE_IDENTIFIERS = frozenset({"t_address", "t_address_payable"})


class ValueTransferKind(enum.Enum):
    """Which native value-transfer builtin a call site uses."""

    SEND = "send"
    TRANSFER = "transfer"
    CALL_WITH_VALUE = "call{value}"


@dataclass(frozen=True)
class NativeValueTransferSite:
    """One native value-transfer call site found in the solc AST."""

    contract: ContractName
    # Source file as recorded in the AST dump, relative to the project root when possible.
    file: str
    # 1-based line, or None when the source file couldn't be read to convert the offset.
    line: Optional[int]
    kind: ValueTransferKind

    def display(self) -> str:
        location = f"{self.file}:{self.line}" if self.line is not None else self.file
        return f"`{self.kind.value}` in {self.contract} ({location})"


def _type_identifier(node: Any) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    type_descriptions = node.get("typeDescriptions")
    if not isinstance(type_descriptions, dict):
        return None
    return type_descriptions.get("typeIdentifier")


def _is_address_expression(node: Any) -> bool:
    return _type_identifier(node) in _ADDRESS_TYPE_IDENTIFIERS


def _is_empty_bytes_literal(node: Any) -> bool:
    """The statically-empty payload of a native transfer via bare call: ``""``/``hex""``."""
    return (
        isinstance(node, dict)
        and node.get("nodeType") == "Literal"
        and not node.get("value")
        and not node.get("hexValue")
    )


def classify_native_value_transfer(node: Any) -> Optional[ValueTransferKind]:
    """Classify an AST node as a native value-transfer call site, or None.

    ``send``/``transfer`` on an address always transfer value with an empty input
    buffer. A bare ``call`` counts only with a ``value`` call option *and* a
    statically-empty payload literal, matching the empty-input-buffer semantics of
    ``optimistic_fallback``. Value-bearing calls with a non-literal payload are
    deliberately left out: whether the buffer is empty is not statically decidable
    there, and payloads built via ``abi.encode*`` carry a selector the dispatcher
    can resolve, so recommending the (unsound) flag for them would overclaim.

    Only the modern (solc >= 0.6.2) ``FunctionCallOptions`` form of
    ``.call{value: ...}`` is recognized; the legacy ``.call.value(...)`` chain is not.
    """
    if not isinstance(node, dict) or node.get("nodeType") != "FunctionCall":
        return None
    expression = node.get("expression")
    if not isinstance(expression, dict):
        return None

    if expression.get("nodeType") == "MemberAccess":
        if not _is_address_expression(expression.get("expression")):
            return None
        member = expression.get("memberName")
        if member == "send":
            return ValueTransferKind.SEND
        if member == "transfer":
            return ValueTransferKind.TRANSFER
        return None

    if expression.get("nodeType") == "FunctionCallOptions":
        names = expression.get("names")
        if not isinstance(names, list) or "value" not in names:
            return None
        target = expression.get("expression")
        if not (
            isinstance(target, dict)
            and target.get("nodeType") == "MemberAccess"
            and target.get("memberName") == "call"
            and _is_address_expression(target.get("expression"))
        ):
            return None
        arguments = node.get("arguments")
        if (
            isinstance(arguments, list)
            and len(arguments) == 1
            and _is_empty_bytes_literal(arguments[0])
        ):
            return ValueTransferKind.CALL_WITH_VALUE

    return None


def _src_byte_offset(node: Dict[str, Any]) -> Optional[int]:
    """Decode the byte offset from a node's packed ``src`` attribute (``offset:length:file``)."""
    src = node.get("src")
    if not isinstance(src, str):
        return None
    parts = src.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def _relativize(source_file: str, project_root: Path) -> Tuple[str, Path]:
    """Return (display path, readable path) for a source file recorded in the AST dump."""
    path = Path(source_file)
    if path.is_absolute():
        try:
            return str(path.relative_to(project_root.resolve())), path
        except ValueError:
            return source_file, path
    return source_file, project_root / path


def _line_at_offset(readable: Path, byte_offset: int) -> Optional[int]:
    """1-based line of a solc byte offset, or None when the file can't be read."""
    try:
        content = readable.read_bytes()
    except OSError:
        return None
    if byte_offset > len(content):
        return None
    return content.count(b"\n", 0, byte_offset) + 1


def find_native_value_transfer_sites(
    ast_path: Path,
    scene_contracts: AbstractSet[ContractName],
    project_root: Path,
) -> List[NativeValueTransferSite]:
    """Find native value-transfer call sites belonging to the given contracts.

    Only nodes stamped with a contract in ``scene_contracts`` are reported. Code a
    scene contract inherits is stamped with the *base* contract's name, so pass the
    scene's contracts together with their inheritance ancestors.

    Returns [] when the AST dump is missing — callers treat detection as best-effort.
    Sites are deduplicated by (source file, byte offset): the same file appears once
    per compilation unit that includes it.
    """
    if not ast_path.exists():
        return []
    with open(ast_path, "r") as f:
        asts_data = json.load(f)

    sites: List[NativeValueTransferSite] = []
    seen: Set[Tuple[str, int]] = set()

    for unit_asts in asts_data.values():
        if not isinstance(unit_asts, dict):
            continue
        for source_file, nodes in unit_asts.items():
            if not isinstance(nodes, dict):
                continue
            for node in nodes.values():
                if not isinstance(node, dict):
                    continue
                contract = node.get(CERTORA_CONTRACT_NAME_KEY)
                if not isinstance(contract, str) or contract not in scene_contracts:
                    continue
                kind = classify_native_value_transfer(node)
                if kind is None:
                    continue
                offset = _src_byte_offset(node)
                if offset is None:
                    continue
                key = (source_file, offset)
                if key in seen:
                    continue
                seen.add(key)
                display, readable = _relativize(source_file, project_root)
                sites.append(
                    NativeValueTransferSite(
                        contract=contract,
                        file=display,
                        line=_line_at_offset(readable, offset),
                        kind=kind,
                    )
                )

    return sorted(sites, key=lambda s: (s.file, s.line if s.line is not None else 0))
