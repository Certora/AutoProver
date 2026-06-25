#!/usr/bin/env python3
"""
Shared types and data structures for autosetup.

This module contains common data structures used across different phases
and components of the autosetup system.
"""

from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict, Set, assert_never
from pathlib import Path
from enum import Enum


class ContractKind(Enum):
    """Types of contracts that can be defined in Solidity."""

    CONTRACT = "contract"
    INTERFACE = "interface"
    LIBRARY = "library"
    ABSTRACT = "abstract"
    UNKNOWN = "unknown"


class TypeDescKind(Enum):
    """Type kinds that appear in .certora_build.json typeDesc field.

    These are the values found in the 'type' field of typeDesc objects.
    Using this enum ensures consistent handling across all type parsing code.
    """
    PRIMITIVE = "Primitive"           # uint256, int128, bool, address, bytes32, etc.
    STRING_TYPE = "StringType"        # string
    PACKED_BYTES = "PackedBytes"      # bytes (dynamic)
    CONTRACT = "Contract"             # contract types (treated as address)
    USER_DEFINED_STRUCT = "UserDefinedStruct"
    USER_DEFINED_VALUE_TYPE = "UserDefinedValueType"
    USER_DEFINED_ENUM = "UserDefinedEnum"
    ARRAY = "Array"                   # dynamic arrays (uses dynamicArrayBaseType)
    STATIC_ARRAY = "StaticArray"      # static arrays (uses staticArrayBaseType + staticArraySize)
    MAPPING = "Mapping"               # mapping types
    TUPLE = "Tuple"                   # tuple types
    STRUCT = "Struct"                 # inline struct definition


def parse_type_desc_kind(type_desc: Any) -> TypeDescKind | None:
    """Extract TypeDescKind from a typeDesc dict.

    Returns None if:
    - type_desc is not a dict
    - type_desc has no 'type' field
    - 'type' value doesn't match any known TypeDescKind
    """
    if not isinstance(type_desc, dict):
        return None
    type_str = type_desc.get("type")
    if type_str is None:
        return None
    try:
        return TypeDescKind(type_str)
    except ValueError:
        return None


class TypeParseMode(Enum):
    """Controls how leaf types are resolved when parsing type descriptors.

    CANONICAL  - Resolves all types to ABI primitives (contract→address, struct→tuple,
                 enum→uint8). Used for ABI selector computation.
    INTERNAL   - Preserves user-defined type names unqualified.
                 Used for internal type signatures.
    DISPATCHER - Replaces contract types with 'address', qualifies user-defined types
                 via canonicalId. Used for dispatcher entry names.
    QUALIFIED  - Qualifies user-defined types via canonicalId, preserves contract names.
                 Used for fullSignature in all_methods.json. Note - all_methods.json will
                 be deprecated at some point, and then this mode might become redundant.
    """

    CANONICAL = "canonical"
    INTERNAL = "internal"
    DISPATCHER = "dispatcher"
    QUALIFIED = "qualified"


def _qualify_user_defined_type(type_desc: dict, contract_name: str) -> str:
    """Qualify a user-defined type name using canonicalId, falling back to contract_name.

    canonicalId format: "contracts/path/File.sol|ContractName.TypeName"
    - If right side of | contains a dot, it's already qualified (e.g. "IAdapter.Params")
    - Otherwise, qualify with contract_name (e.g. top-level "MarketId" → "MyContract.MarketId")
    - If no canonicalId, fall back to contract_name.name
    """
    canonical_id = type_desc.get("canonicalId", "")
    if "|" in canonical_id:
        right_side = canonical_id.split("|", 1)[1]
        if "." in right_side:
            return right_side
        return f"{contract_name}.{right_side}"
    name = type_desc.get("name")
    if not name:
        raise ValueError(f"type descriptor {type_desc} has no canonical name")
    return f"{contract_name}.{name}"


def parse_type_descriptor(type_desc: dict, mode: TypeParseMode, contract_name: str = "") -> str:
    """Parse a Certora typeDesc dict into a type string.

    This is the single entry point for all type descriptor parsing. The mode parameter
    controls how leaf types (contracts, user-defined types) are resolved.
    NOTE - The type_desc is a dict that comes from the ast outputted by the solc
    compiler. We don't control it, and it can vary between solc versions.

    Args:
        type_desc: A typeDesc dict from .certora_build.json
        mode: Controls leaf type resolution behavior
        contract_name: Contract name for qualifying user-defined types (used by DISPATCHER/QUALIFIED)
    """
    kind = parse_type_desc_kind(type_desc)

    if kind is None:
        if mode == TypeParseMode.CANONICAL:
            # Legacy fallback: some type_descs have no 'type' field but have primitiveName/contractName
            if "primitiveName" in type_desc:
                return type_desc["primitiveName"]
            if "contractName" in type_desc:
                return "address"
            if "name" in type_desc:
                return type_desc["name"]
        return "unknown"

    recurse = lambda td: parse_type_descriptor(td, mode, contract_name)

    match kind:
        case TypeDescKind.PRIMITIVE:
            return type_desc.get("primitiveName", "")
        case TypeDescKind.STRING_TYPE:
            return "string"
        case TypeDescKind.PACKED_BYTES:
            return "bytes"
        case TypeDescKind.CONTRACT:
            if mode in (TypeParseMode.CANONICAL, TypeParseMode.DISPATCHER):
                return "address"
            return type_desc.get("contractName", "address")
        case TypeDescKind.USER_DEFINED_STRUCT:
            if mode == TypeParseMode.CANONICAL:
                members = [recurse(m.get("type", {})) for m in type_desc.get("members", [])]
                return f"({','.join(members)})"
            elif mode == TypeParseMode.INTERNAL:
                return type_desc.get("name", "unknown")
            else:  # DISPATCHER or QUALIFIED
                return _qualify_user_defined_type(type_desc, contract_name)
        case TypeDescKind.USER_DEFINED_VALUE_TYPE:
            if mode == TypeParseMode.CANONICAL:
                return recurse(type_desc.get("underlying", {}))
            elif mode == TypeParseMode.INTERNAL:
                return type_desc.get("name", "unknown")
            else:  # DISPATCHER or QUALIFIED
                return _qualify_user_defined_type(type_desc, contract_name)
        case TypeDescKind.USER_DEFINED_ENUM:
            if mode == TypeParseMode.CANONICAL:
                return "uint8"
            elif mode == TypeParseMode.INTERNAL:
                return type_desc.get("name", "unknown")
            else:  # DISPATCHER or QUALIFIED
                return _qualify_user_defined_type(type_desc, contract_name)
        case TypeDescKind.ARRAY:
            base_desc = type_desc.get("dynamicArrayBaseType") or type_desc.get("base", {})
            return f"{recurse(base_desc)}[]"
        case TypeDescKind.STATIC_ARRAY:
            base_desc = type_desc.get("staticArrayBaseType", {})
            array_size = type_desc.get("staticArraySize", "")
            return f"{recurse(base_desc)}[{array_size}]"
        case TypeDescKind.MAPPING:
            key_desc = type_desc.get("key") or type_desc.get("mappingKeyType", {})
            value_desc = type_desc.get("value") or type_desc.get("mappingValueType", {})
            return f"mapping({recurse(key_desc)} => {recurse(value_desc)})"
        case TypeDescKind.TUPLE:
            members = [recurse(m) for m in type_desc.get("members", [])]
            return f"({','.join(members)})"
        case TypeDescKind.STRUCT:
            if mode == TypeParseMode.CANONICAL:
                members = [recurse(m.get("type", {})) for m in type_desc.get("members", [])]
                return f"({','.join(members)})"
            return type_desc.get("name", "unknown")
    assert_never(kind)


@dataclass(frozen=True)
class ContractHandle:
    contract_name: str
    source_file: str

    @classmethod
    def from_filepath(cls, file_path: str) -> "ContractHandle":
        """Create ContractHandle from file path by extracting contract name from filename."""
        basename = Path(file_path).stem  # Gets filename without extension
        return cls(contract_name=basename, source_file=file_path)

    def to_config_str(self) -> str:
        if Path(self.source_file).stem == self.contract_name:
            return self.source_file
        return f"{self.source_file}:{self.contract_name}"

    def matches_map_key(self, key: str) -> bool:
        """Check if this contract matches a compiler_map/solc_via_ir_map key.

        Uses the same logic as the Certora prover (certoraContext.py):
        - If key has no file suffix (Path(key).suffix == "") -> contract name pattern, use fnmatch
        - If key has file suffix (e.g., .sol) -> file path pattern, use glob with GLOBSTAR
        """
        import fnmatch
        from wcmatch import glob

        # Strip any ":field" suffix from key (e.g., "Contract:field" -> "Contract")
        pattern = key.split(":")[0]

        if Path(pattern).suffix == "":
            # No file suffix -> contract name pattern
            return fnmatch.fnmatch(self.contract_name, pattern)
        else:
            # Has file suffix -> file path pattern
            return glob.globmatch(self.source_file, pattern, flags=glob.GLOBSTAR)


@dataclass
class ContractInfo:
    """Information about a contract definition with compilation artifacts."""

    name: str  # Contract name
    source_file: Path  # Path to source file containing this contract
    kind: ContractKind = ContractKind.CONTRACT  # Type of contract
    inherits_from: List[str] = field(
        default_factory=list
    )  # Names of inherited contracts/interfaces

    # Compilation information
    is_compilable: bool = True  # Whether this contract can be compiled
    compilation_error: Optional[str] = None  # Error message if compilation fails
    solidity_version: Optional[str] = None  # Required Solidity version (from pragma)

    # Artifact and signature data (populated after compilation)
    artifact_path: Optional[Path] = None  # Path to compiled .json artifact
    bytecode: Optional[str] = None  # Compiled bytecode (hex string)
    function_signatures: Dict[str, "FunctionSignature"] = field(
        default_factory=dict
    )  # Selector -> FunctionSignature
    compilation_metadata: Optional[Dict] = None  # Additional metadata from compilation

    # Linking-specific data (used by contract_linker.py)
    state_vars: Optional[Set[tuple]] = None  # (var_name, type_name) for user-defined types
    constructor_params: Optional[List[tuple[str, str]]] = None  # [(solidity_type, name), ...]


@dataclass
class FunctionSignature:
    """Represents a function signature with its selector (no specific contract)."""

    signature: str  # Full function signature like "transfer(address,uint256)"
    selector: str  # 4-byte selector like "0xa9059cbb"
    is_view: bool = False
    is_pure: bool = False
    internal_type_signature: Optional[str] = None  # User-defined type signature like "updateUnwindSwapFeeRate(MarketId,uint256)"
    internal_type_selector: Optional[str] = None  # Selector for user-defined type signature
    dispatcher_entry_name: Optional[str] = None  # Contract-qualified name for dispatcher like "market(CorkPool.MarketId)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "selector": self.selector,
            "is_view": self.is_view,
            "is_pure": self.is_pure,
            "internal_type_signature": self.internal_type_signature,
            "internal_type_selector": self.internal_type_selector,
            "dispatcher_entry_name": self.dispatcher_entry_name,
        }
