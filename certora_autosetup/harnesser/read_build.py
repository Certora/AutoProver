"""Read a library's API out of ``.certora_build.json``.

The build is the only trustworthy description of what a library declares: it has
resolved inheritance, overloads and type aliases that source text does not. The
harnesser therefore runs a probe compilation first and reads the result here, rather
than parsing Solidity (which is what ``utils/library_harness.py`` must do, because it
runs inside the compilation retry loop before any build artifact exists).

Two things about the build JSON are easy to get wrong and are load-bearing here:

- Internal functions live in ``allMethods``, not ``internalFunctions``. ``allMethods``
  is ``contract.methods + contract.internal_funcs`` (external plus internal plus
  private), while ``internalFunctions`` holds *autofinder instrumentation*. certora-cli
  deliberately generates no autofinders for library-hosted functions, so
  ``internalFunctions`` is systematically empty for libraries — reading it would yield
  zero wrappers for a library like OpenZeppelin's EnumerableSet, whose entire surface
  is internal.
- A library is located by ``(name, source file)``, never by name alone. Solady ships 17
  library names twice, under ``src/utils/`` and ``src/utils/g/``, with differently
  scoped structs; matching on the name alone picks an arbitrary one and emits types
  that do not resolve against the imported file.
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from certora_autosetup.harnesser.model import (
    KIND_ARRAY,
    KIND_MAPPING,
    KIND_STRUCT,
    KIND_VALUE,
    LibFunction,
    LibParam,
    LibraryApi,
    LibraryHarnessError,
    MemberNode,
)
from certora_autosetup.utils.types import TypeParseMode, parse_type_descriptor

#: Written by certoraRun under the run directory it reports as ``latest``.
BUILD_JSON_RELPATH = Path(".certora_internal/latest/.certora_build.json")


def _iter_contracts(build_data: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield every contract record across all compilation units in the build.

    A contract reached through several units appears once per unit; callers that need
    a single record must disambiguate themselves.
    """
    for obj in build_data.values():
        if isinstance(obj, dict):
            for contract in obj.get("contracts", []):
                if isinstance(contract, dict):
                    yield contract


def _same_file(candidate: str, wanted: str) -> bool:
    """Compare two build-reported paths that may differ in absoluteness.

    The build mixes project-relative and absolute paths for the same file depending on
    how it was reached, so equality is decided on the longest common suffix of path
    components.
    """
    if not candidate or not wanted:
        return False
    cand_parts = Path(candidate).parts
    want_parts = Path(wanted).parts
    depth = min(len(cand_parts), len(want_parts))
    return cand_parts[-depth:] == want_parts[-depth:]


def _param_list(raw: List[Dict[str, Any]], names: List[str], contract_name: str) -> tuple[LibParam, ...]:
    """Render one ``fullArgs``/``returns`` list into typed parameters.

    Types are rendered in SOLIDITY mode so nested types keep the qualification the
    generated source needs (``EnumerableSet.Bytes32Set``, not ``Bytes32Set``).
    """
    params: List[LibParam] = []
    for index, entry in enumerate(raw):
        type_desc = entry.get("typeDesc", {})
        solidity_type = parse_type_descriptor(type_desc, TypeParseMode.SOLIDITY, contract_name)
        if not solidity_type or solidity_type == "unknown":
            # Signalled to the caller as an unnamed unresolved type; the classifier
            # turns it into a skip rather than emitting source that will not compile.
            solidity_type = ""
        name = names[index] if index < len(names) else ""
        params.append(
            LibParam(
                name=name or f"arg{index}",
                solidity_type=solidity_type,
                location=entry.get("location", "") or "",
                desc_kind=str(type_desc.get("type", "")) if isinstance(type_desc, dict) else "",
            )
        )
    return tuple(params)


#: Guards against a self-referential struct sending the walk into a loop.
_MAX_MEMBER_DEPTH = 6


def _member_node(name: str, type_desc: Dict[str, Any], contract_name: str, depth: int = 0) -> Optional[MemberNode]:
    """Describe one struct member as a tree node, recursing through its shape.

    Kept structural rather than flattened to a type string: a reader has to reach
    *through* a mapping or array, supplying a key or index, and that is only decidable
    from the shape.
    """
    if depth > _MAX_MEMBER_DEPTH or not isinstance(type_desc, dict):
        return None

    rendered = parse_type_descriptor(type_desc, TypeParseMode.SOLIDITY, contract_name)
    if not rendered or rendered == "unknown":
        return None

    kind = type_desc.get("type")

    if kind == "UserDefinedStruct":
        children = _member_nodes(type_desc.get("structMembers", []), contract_name, depth + 1)
        return MemberNode(name=name, solidity_type=rendered, kind=KIND_STRUCT, members=children)

    if kind == "Mapping":
        key_desc = type_desc.get("mappingKeyType") or type_desc.get("key") or {}
        value_desc = type_desc.get("mappingValueType") or type_desc.get("value") or {}
        key_type = parse_type_descriptor(key_desc, TypeParseMode.SOLIDITY, contract_name)
        value_node = _member_node("", value_desc, contract_name, depth + 1)
        if not key_type or key_type == "unknown" or value_node is None:
            return None
        return MemberNode(
            name=name, solidity_type=rendered, kind=KIND_MAPPING, key_type=key_type, value=value_node
        )

    if kind == "Array":
        base_desc = type_desc.get("dynamicArrayBaseType") or type_desc.get("base") or {}
        element = _member_node("", base_desc, contract_name, depth + 1)
        if element is None:
            return None
        return MemberNode(name=name, solidity_type=rendered, kind=KIND_ARRAY, value=element)

    return MemberNode(name=name, solidity_type=rendered, kind=KIND_VALUE)


def _member_nodes(
    raw_members: List[Dict[str, Any]], contract_name: str, depth: int = 0
) -> tuple[MemberNode, ...]:
    nodes: List[MemberNode] = []
    for member in raw_members:
        if not isinstance(member, dict):
            continue
        member_name = member.get("name") or member.get("fieldName")
        if not member_name:
            continue
        node = _member_node(
            member_name, member.get("type", {}) or member.get("typeDesc", {}), contract_name, depth
        )
        if node is not None:
            nodes.append(node)
    return tuple(nodes)


def _struct_members(contract: Dict[str, Any]) -> Dict[str, tuple[MemberNode, ...]]:
    """Collect every struct's member tree, keyed by its qualified Solidity type."""
    members: Dict[str, tuple[MemberNode, ...]] = {}
    for type_info in contract.get("solidityTypes", []):
        if not isinstance(type_info, dict) or type_info.get("type") != "UserDefinedStruct":
            continue
        struct_name = type_info.get("structName")
        if not struct_name:
            continue
        containing = type_info.get("containingContract")
        qualified = f"{containing}.{struct_name}" if containing else struct_name
        members[qualified] = _member_nodes(
            type_info.get("structMembers", []), containing or ""
        )
    return members


def read_library_api(
    build_json: Path,
    library_name: str,
    library_source_file: str,
) -> LibraryApi:
    """Extract ``library_name``'s full declared API from a completed build.

    ``library_source_file`` disambiguates same-named libraries; it is matched against
    the build's own path for the contract.
    """
    if not build_json.exists():
        raise LibraryHarnessError(
            f"probe build produced no {build_json} — cannot read the library's API"
        )

    with open(build_json, "r") as f:
        build_data = json.load(f)

    matched: Optional[Dict[str, Any]] = None
    seen_names: List[str] = []
    for contract in _iter_contracts(build_data):
        name = contract.get("name", "")
        if name != library_name:
            continue
        seen_names.append(contract.get("original_file") or contract.get("file") or "")
        candidate_file = contract.get("original_file") or contract.get("file") or ""
        if _same_file(candidate_file, library_source_file):
            matched = contract
            break

    if matched is None:
        if seen_names:
            raise LibraryHarnessError(
                f"library {library_name} is in the build, but none of its records match "
                f"{library_source_file} (found: {', '.join(sorted(set(seen_names)))})"
            )
        raise LibraryHarnessError(
            f"library {library_name} ({library_source_file}) is absent from the probe "
            f"build — the harness stub does not reference it"
        )

    functions: List[LibFunction] = []
    for method in matched.get("allMethods", []):
        if not isinstance(method, dict):
            continue
        name = method.get("name", "")
        if not name or name == "constructor":
            continue
        functions.append(
            LibFunction(
                name=name,
                visibility=method.get("visibility", "") or "internal",
                state_mutability=method.get("stateMutability", "") or "nonpayable",
                params=_param_list(
                    method.get("fullArgs", []), method.get("paramNames", []), library_name
                ),
                returns=_param_list(method.get("returns", []), [], library_name),
                source_line=method.get("sourceLine", 0) or 0,
            )
        )

    if not functions:
        raise LibraryHarnessError(
            f"library {library_name} declares no functions in the build output"
        )

    return LibraryApi(
        name=library_name,
        source_file=matched.get("original_file") or matched.get("file") or library_source_file,
        functions=tuple(functions),
        struct_members=_struct_members(matched),
    )
