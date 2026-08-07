"""Type-name nodes of the Solidity compact AST (members of the TypeName union)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .base import SolcNode, StateMutability, TypeDescriptions, Visibility

if TYPE_CHECKING:
    from .declarations import ParameterList
    from .unions import Expression, TypeName


class ArrayTypeName(SolcNode):
    """A static or dynamic array type, e.g. ``uint256[]`` or ``bytes32[4]``."""

    typeDescriptions: TypeDescriptions
    baseType: "TypeName"
    length: "Expression | None" = None
    nodeType: Literal["ArrayTypeName"]


class ElementaryTypeName(SolcNode):
    """A built-in type name, e.g. ``uint256``, ``address``, ``bytes``."""

    typeDescriptions: TypeDescriptions
    name: str
    stateMutability: StateMutability | None = None
    nodeType: Literal["ElementaryTypeName"]


class FunctionTypeName(SolcNode):
    """A function type, e.g. ``function (uint) external returns (bool)``."""

    typeDescriptions: TypeDescriptions
    parameterTypes: "ParameterList"
    returnParameterTypes: "ParameterList"
    stateMutability: StateMutability
    visibility: Visibility
    nodeType: Literal["FunctionTypeName"]


class Mapping(SolcNode):
    """A mapping type, e.g. ``mapping(address owner => uint256 balance)``."""

    typeDescriptions: TypeDescriptions
    keyType: "TypeName"
    valueType: "TypeName"
    keyName: str | None = None
    keyNameLocation: str | None = None
    valueName: str | None = None
    valueNameLocation: str | None = None
    nodeType: Literal["Mapping"]


class IdentifierPath(SolcNode):
    """A (possibly dotted) path referring to a declaration, e.g. ``Lib.Struct``."""

    name: str
    nameLocations: list[str] | None = None
    referencedDeclaration: int
    nodeType: Literal["IdentifierPath"]


class UserDefinedTypeName(SolcNode):
    """A reference to a user-defined type (struct, enum, contract, UDVT)."""

    typeDescriptions: TypeDescriptions
    contractScope: None = None
    name: str | None = None
    pathNode: IdentifierPath | None = None
    referencedDeclaration: int
    nodeType: Literal["UserDefinedTypeName"]
