#!/usr/bin/env python3
"""
Type Analyzer for Solidity Types

This module loads type and method information from pre-generated JSON files
in .certora_internal/ (all_methods.json and all_user_defined_types.json).
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from certora_autosetup.utils import logger
from certora_autosetup.utils.types import TypeDescKind, parse_type_desc_kind


class TypeCategory(Enum):
    """Categories of Solidity types."""
    PRIMITIVE = "primitive"  # uint, int, bool, address, etc.
    BYTES = "bytes"
    STRING = "string"
    ARRAY = "array"
    STRUCT = "struct"
    ENUM = "enum"
    CONTRACT = "contract"  # Contract/interface types (resolve to address)
    MAPPING = "mapping"  # Not supported for input validation
    UNKNOWN = "unknown"


@dataclass
class TypeInfo:
    """Base class for type information."""
    category: TypeCategory
    solidity_name: str  # e.g., "uint256", "MyStruct", "uint256[]"
    cvl_name: str  # CVL representation

    def is_primitive(self) -> bool:
        return self.category == TypeCategory.PRIMITIVE

    def is_bytes_or_string(self) -> bool:
        return self.category in [TypeCategory.BYTES, TypeCategory.STRING]

    def is_array(self) -> bool:
        return self.category == TypeCategory.ARRAY

    def is_struct(self) -> bool:
        return self.category == TypeCategory.STRUCT

    def is_enum(self) -> bool:
        return self.category == TypeCategory.ENUM


@dataclass
class StructFieldInfo:
    """Information about a struct field."""
    name: str
    type_info: TypeInfo
    field_path: str  # e.g., "myStruct.field1.nestedField"


@dataclass
class StructInfo(TypeInfo):
    """Information about a struct type."""
    qualified_name: str  # e.g., "MyContract.MyStruct"
    fields: List[StructFieldInfo] = field(default_factory=list)
    containing_contract: Optional[str] = None
    _raw_fields: List[Dict] = field(default_factory=list, repr=False)  # Temporary storage for field resolution

    def get_all_leaf_fields(self, prefix: str = "") -> List[StructFieldInfo]:
        """Get all leaf fields (primitives, bytes, string, arrays) recursively."""
        leaf_fields = []
        for field_info in self.fields:
            field_path = f"{prefix}.{field_info.name}" if prefix else field_info.name
            if field_info.type_info.is_struct():
                # Recursively get nested fields
                nested_struct = field_info.type_info
                assert isinstance(nested_struct, StructInfo), f"Expected StructInfo but got {type(nested_struct)}"
                leaf_fields.extend(nested_struct.get_all_leaf_fields(field_path))
            else:
                # This is a leaf field
                leaf_fields.append(StructFieldInfo(
                    name=field_info.name,
                    type_info=field_info.type_info,
                    field_path=field_path
                ))
        return leaf_fields


@dataclass
class ArrayInfo(TypeInfo):
    """Information about an array type."""
    element_type: TypeInfo
    is_dynamic: bool = True  # True for T[], False for T[N]
    fixed_size: Optional[int] = None


@dataclass
class ParameterInfo:
    """Information about a method parameter."""
    name: str
    index: int
    type_info: TypeInfo


@dataclass
class MethodInfo:
    """Information about a method."""
    contract_name: str
    method_name: str
    parameters: List[ParameterInfo]
    visibility: str
    state_mutability: str
    source_file: str = ""
    source_line: int = 0

    def is_external_or_public(self) -> bool:
        return self.visibility in ["external", "public"]

    def is_view_or_pure(self) -> bool:
        return self.state_mutability in ["view", "pure"]

    def get_signature_hash(self) -> str:
        """Generate a unique signature hash for the method (valid CVL identifier)."""
        param_types = "_".join([p.type_info.solidity_name.replace("[]", "Array").replace(".", "_").replace(" ", "")
                                for p in self.parameters])
        return f"{self.method_name}_{param_types}" if param_types else self.method_name


class TypeRegistry:
    """Registry for managing type definitions."""

    def __init__(self):
        self.structs: Dict[str, StructInfo] = {}
        self.enums: Dict[str, TypeInfo] = {}
        # All valid Solidity primitive types
        # uint/int: 8 to 256 in steps of 8
        uint_types = {"uint"} | {f"uint{i}" for i in range(8, 257, 8)}
        int_types = {"int"} | {f"int{i}" for i in range(8, 257, 8)}
        # bytes: 1 to 32
        bytes_types = {f"bytes{i}" for i in range(1, 33)}
        self._primitive_types = uint_types | int_types | bytes_types | {"bool", "address"}

    def register_struct(self, qualified_name: str, struct_info: StructInfo):
        """Register a struct type."""
        self.structs[qualified_name] = struct_info

    def register_enum(self, qualified_name: str, enum_info: TypeInfo):
        """Register an enum type."""
        self.enums[qualified_name] = enum_info

    def get_struct(self, qualified_name: str) -> Optional[StructInfo]:
        """Get struct by qualified name."""
        return self.structs.get(qualified_name)

    def get_struct_by_simple_name(self, simple_name: str) -> Optional[StructInfo]:
        """Get struct by simple name (without contract prefix)."""
        for qualified_name, struct_info in self.structs.items():
            if qualified_name.endswith(f".{simple_name}") or qualified_name == simple_name:
                return struct_info
        return None

    def get_enum_by_simple_name(self, simple_name: str) -> Optional[TypeInfo]:
        """Get enum by simple name (without contract prefix)."""
        for qualified_name, enum_info in self.enums.items():
            if qualified_name.endswith(f".{simple_name}") or qualified_name == simple_name:
                return enum_info
        return None

    def is_primitive_type(self, type_name: str) -> bool:
        """Check if a type is primitive."""
        # Remove array brackets for checking
        base_type = type_name.rstrip("[]").split()[-1]
        return base_type in self._primitive_types


class TypeAnalyzer:
    """Main type analyzer that loads from pre-generated JSON files."""

    def __init__(self, certora_internal_path: str = ".certora_internal"):
        """
        Initialize the type analyzer.

        Args:
            certora_internal_path: Path to the .certora_internal directory containing
                                   all_methods.json and all_user_defined_types.json
        """
        self.certora_internal = Path(certora_internal_path)
        self.registry = TypeRegistry()
        self.methods: List[MethodInfo] = []

        # JSON file paths
        self.methods_json = self.certora_internal / "all_methods.json"
        self.types_json = self.certora_internal / "all_user_defined_types.json"

    def parse_all(self) -> bool:
        """Load and parse all type information from pre-generated JSON files.

        Returns:
            bool: True if parsing succeeded, False on failure. Callers should
                  treat False as a fatal error since type resolution will not
                  work correctly without the parsed data.
        """
        # Load user-defined types first (structs, enums)
        if not self._load_user_defined_types():
            return False

        # Resolve struct fields (still needed since structMembers has raw type data)
        self._resolve_struct_fields()

        # Load methods (properly scoped by contractName)
        if not self._load_methods():
            return False

        return True

    def _load_user_defined_types(self) -> bool:
        """Load user-defined types from all_user_defined_types.json."""
        if not self.types_json.exists():
            logger.error(f"Types JSON not found at {self.types_json}", "TypeAnalyzer")
            return False

        try:
            with open(self.types_json) as f:
                types_data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading types JSON: {e}", "TypeAnalyzer")
            return False

        for type_info in types_data:
            category = type_info.get("typeCategory")
            qualified_name = type_info.get("qualifiedName", "")

            if category == "UserDefinedStruct":
                struct_info = StructInfo(
                    category=TypeCategory.STRUCT,
                    solidity_name=type_info.get("typeName", ""),
                    cvl_name=qualified_name,
                    qualified_name=qualified_name,
                    containing_contract=type_info.get("containingContract")
                )
                # Store raw members for later resolution
                struct_info._raw_fields = type_info.get("structMembers", [])
                self.registry.register_struct(qualified_name, struct_info)

            elif category == "UserDefinedEnum":
                enum_info = TypeInfo(
                    category=TypeCategory.ENUM,
                    solidity_name=type_info.get("typeName", ""),
                    cvl_name=qualified_name
                )
                self.registry.register_enum(qualified_name, enum_info)

            elif category == "UserDefinedValueType":
                # Value types are treated as their underlying primitive
                value_type_info = TypeInfo(
                    category=TypeCategory.PRIMITIVE,
                    solidity_name=type_info.get("typeName", ""),
                    cvl_name=qualified_name
                )
                # Store in enums dict for lookup (they behave like enums - named aliases)
                self.registry.enums[qualified_name] = value_type_info

        return True

    def _resolve_struct_fields(self):
        """Resolve all struct field types after all structs are registered."""
        for _qualified_name, struct_info in self.registry.structs.items():
            if not struct_info._raw_fields:
                continue

            for raw_field in struct_info._raw_fields:
                # Note: in structMembers, fields use "name" and "type" keys
                field_name = raw_field.get("name", "unknown")
                field_type_data = raw_field.get("type", {})

                # Resolve the field type
                field_type_info = self._resolve_type_from_data(field_type_data)

                if field_type_info:
                    struct_info.fields.append(StructFieldInfo(
                        name=field_name,
                        type_info=field_type_info,
                        field_path=field_name
                    ))

            # Clear the temporary data
            struct_info._raw_fields = []

    def _resolve_type_from_data(self, type_data: Dict) -> Optional[TypeInfo]:
        """Resolve a type from its JSON representation (TypeDescKind format)."""
        kind = parse_type_desc_kind(type_data)

        if kind is None:
            return TypeInfo(
                category=TypeCategory.UNKNOWN,
                solidity_name=str(type_data),
                cvl_name=str(type_data)
            )

        if kind == TypeDescKind.PRIMITIVE:
            prim_name = type_data.get("primitiveName", "uint256")
            return TypeInfo(
                category=TypeCategory.PRIMITIVE,
                solidity_name=prim_name,
                cvl_name=prim_name
            )
        elif kind == TypeDescKind.PACKED_BYTES:
            return TypeInfo(
                category=TypeCategory.BYTES,
                solidity_name="bytes",
                cvl_name="bytes"
            )
        elif kind == TypeDescKind.STRING_TYPE:
            return TypeInfo(
                category=TypeCategory.STRING,
                solidity_name="string",
                cvl_name="string"
            )
        elif kind == TypeDescKind.CONTRACT:
            return TypeInfo(
                category=TypeCategory.PRIMITIVE,
                solidity_name="address",
                cvl_name="address"
            )
        elif kind == TypeDescKind.ARRAY:
            element_type_data = type_data.get("dynamicArrayBaseType") or type_data.get("elementType", {})
            element_type = self._resolve_type_from_data(element_type_data)
            if element_type:
                return ArrayInfo(
                    category=TypeCategory.ARRAY,
                    solidity_name=f"{element_type.solidity_name}[]",
                    cvl_name=f"{element_type.cvl_name}[]",
                    element_type=element_type,
                    is_dynamic=True
                )
            return None
        elif kind == TypeDescKind.USER_DEFINED_STRUCT:
            qualified_name = type_data.get("qualifiedName", "")
            if not qualified_name:
                struct_name = type_data.get("structName", "")
                containing_contract = type_data.get("containingContract", "")
                qualified_name = f"{containing_contract}.{struct_name}"
            struct_info = self.registry.get_struct(qualified_name)
            if struct_info:
                return struct_info
            # Fallback: try simple name lookup (handles qualified name mismatches)
            simple_name = qualified_name.split(".")[-1] if "." in qualified_name else qualified_name
            struct_info = self.registry.get_struct_by_simple_name(simple_name)
            if struct_info:
                logger.warning(
                    f"Struct '{qualified_name}' not found by qualified name, "
                    f"resolved via simple name fallback to '{struct_info.qualified_name}'",
                    "TypeAnalyzer"
                )
                return struct_info
            # Return a StructInfo placeholder so isinstance checks work
            return StructInfo(
                category=TypeCategory.STRUCT,
                solidity_name=qualified_name.split(".")[-1],
                cvl_name=qualified_name,
                qualified_name=qualified_name
            )
        elif kind == TypeDescKind.USER_DEFINED_VALUE_TYPE:
            qualified_name = type_data.get("qualifiedName", "")
            if not qualified_name:
                type_name = type_data.get("typeName", "")
                containing_contract = type_data.get("containingContract", "")
                qualified_name = f"{containing_contract}.{type_name}"
            return TypeInfo(
                category=TypeCategory.PRIMITIVE,
                solidity_name=qualified_name.split(".")[-1],
                cvl_name=qualified_name
            )
        elif kind == TypeDescKind.USER_DEFINED_ENUM:
            qualified_name = type_data.get("qualifiedName", "")
            if not qualified_name:
                enum_name = type_data.get("enumName", "")
                containing_contract = type_data.get("containingContract", "")
                qualified_name = f"{containing_contract}.{enum_name}"
            return TypeInfo(
                category=TypeCategory.ENUM,
                solidity_name=qualified_name.split(".")[-1],
                cvl_name=qualified_name
            )
        elif kind == TypeDescKind.MAPPING:
            return TypeInfo(
                category=TypeCategory.MAPPING,
                solidity_name="mapping",
                cvl_name="mapping"
            )
        elif kind == TypeDescKind.TUPLE:
            return TypeInfo(
                category=TypeCategory.UNKNOWN,
                solidity_name="tuple",
                cvl_name="tuple"
            )
        elif kind == TypeDescKind.STRUCT:
            return TypeInfo(
                category=TypeCategory.STRUCT,
                solidity_name=type_data.get("name", "struct"),
                cvl_name=type_data.get("name", "struct")
            )
        else:
            return TypeInfo(
                category=TypeCategory.UNKNOWN,
                solidity_name=str(type_data),
                cvl_name=str(type_data)
            )

    def _load_methods(self) -> bool:
        """Load methods from all_methods.json."""
        if not self.methods_json.exists():
            logger.error(f"Methods JSON not found at {self.methods_json}", "TypeAnalyzer")
            return False

        try:
            with open(self.methods_json) as f:
                methods_data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading methods JSON: {e}", "TypeAnalyzer")
            return False

        for method_data in methods_data:
            # Skip constructors - they can't be called after deployment
            if method_data.get("name") == "constructor":
                continue

            # Parse parameters from fullSignature
            type_strings = method_data.get("fullSignature", [])
            parameters = self._parse_parameters_from_signature(type_strings)

            method_info = MethodInfo(
                contract_name=method_data.get("contractName", ""),
                method_name=method_data.get("name", ""),
                parameters=parameters,
                visibility=method_data.get("visibility", "public"),
                state_mutability=method_data.get("stateMutability", "nonpayable"),
                source_file=method_data.get("originalFile", ""),
                source_line=method_data.get("sourceLine", 0),
            )
            self.methods.append(method_info)

        return True

    def _parse_parameters_from_signature(self, type_strings: list[str]) -> List[ParameterInfo]:
        """Parse fullSignature type list (e.g., ['uint256', 'address', 'bytes32[]']) into ParameterInfo list."""
        parameters = []
        for idx, type_str in enumerate(type_strings):
            type_info = self._resolve_type_from_string(type_str)
            parameters.append(ParameterInfo(
                name=f"param{idx}",
                index=idx,
                type_info=type_info
            ))

        return parameters

    def _resolve_type_from_string(self, type_str: str) -> TypeInfo:
        """Resolve a type from its string representation (e.g., 'uint256', 'bytes32[]')."""
        # Handle arrays (including nested)
        if type_str.endswith("[]"):
            base_type_str = type_str[:-2]
            element_type = self._resolve_type_from_string(base_type_str)
            return ArrayInfo(
                category=TypeCategory.ARRAY,
                solidity_name=f"{element_type.solidity_name}[]",
                cvl_name=f"{element_type.cvl_name}[]",
                element_type=element_type,
                is_dynamic=True
            )

        # Handle primitives
        if self.registry.is_primitive_type(type_str):
            return TypeInfo(
                category=TypeCategory.PRIMITIVE,
                solidity_name=type_str,
                cvl_name=type_str
            )

        # Handle bytes/string
        if type_str == "bytes":
            return TypeInfo(category=TypeCategory.BYTES, solidity_name="bytes", cvl_name="bytes")
        if type_str == "string":
            return TypeInfo(category=TypeCategory.STRING, solidity_name="string", cvl_name="string")

        # Handle qualified struct/enum names (e.g., "Contract.StructName")
        if "." in type_str:
            struct_info = self.registry.get_struct(type_str)
            if struct_info:
                return struct_info
            if type_str in self.registry.enums:
                return self.registry.enums[type_str]
            # Fallback: try simple name lookup (handles qualified name mismatches
            # between all_methods.json and all_user_defined_types.json)
            simple_name = type_str.split(".")[-1]
            struct_info = self.registry.get_struct_by_simple_name(simple_name)
            if struct_info:
                logger.warning(
                    f"Struct '{type_str}' not found by qualified name, "
                    f"resolved via simple name fallback to '{struct_info.qualified_name}'",
                    "TypeAnalyzer"
                )
                return struct_info
            enum_info = self.registry.get_enum_by_simple_name(simple_name)
            if enum_info:
                logger.warning(
                    f"Enum '{type_str}' not found by qualified name, "
                    f"resolved via simple name fallback to '{enum_info.cvl_name}'",
                    "TypeAnalyzer"
                )
                return enum_info
            # Return a placeholder struct
            return StructInfo(
                category=TypeCategory.STRUCT,
                solidity_name=type_str.split(".")[-1],
                cvl_name=type_str,
                qualified_name=type_str
            )

        # Check if it's a struct by simple name (without qualifier)
        struct_info = self.registry.get_struct_by_simple_name(type_str)
        if struct_info:
            return struct_info

        # Check if it's an enum by simple name
        enum_info = self.registry.get_enum_by_simple_name(type_str)
        if enum_info:
            return enum_info

        # Unknown type that is not a struct or enum must be a contract type
        # Contract types (interfaces like IERC20, IERC721) resolve to address in CVL
        return TypeInfo(
            category=TypeCategory.CONTRACT,
            solidity_name=type_str,
            cvl_name="address"
        )

    def get_methods_for_contract(self, contract_name: str) -> List[MethodInfo]:
        """Get all methods for a specific contract."""
        return [m for m in self.methods if m.contract_name == contract_name]

    def get_public_non_view_methods(self, contract_name: str) -> List[MethodInfo]:
        """Get public/external non-view methods for input validation checking."""
        methods = self.get_methods_for_contract(contract_name)
        return [m for m in methods
                if m.is_external_or_public() and not m.is_view_or_pure()]
