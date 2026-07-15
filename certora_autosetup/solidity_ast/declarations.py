"""Declaration nodes of the Solidity compact AST (source-unit and contract-body level).

Also defines ``SourceUnit`` itself, transcribed from the schema root (it is the
schema's top-level object, not a member of ``definitions``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .base import (
    Mutability,
    SolcNode,
    StateMutability,
    StorageLocation,
    TypeDescriptions,
    Visibility,
)

if TYPE_CHECKING:
    from .expressions import Identifier
    from .statements import Block
    from .types import IdentifierPath, UserDefinedTypeName
    from .unions import ContractBodyNode, Expression, SourceUnitNode, TypeName


class StructuredDocumentation(SolcNode):
    """A NatSpec documentation node."""

    text: str
    nodeType: Literal["StructuredDocumentation"]


class OverrideSpecifier(SolcNode):
    """An ``override(...)`` specifier on a function, modifier, or state variable."""

    overrides: "list[UserDefinedTypeName] | list[IdentifierPath]"
    nodeType: Literal["OverrideSpecifier"]


class VariableDeclaration(SolcNode):
    """A variable declaration: state variable, parameter, struct member, or local."""

    name: str
    nameLocation: str | None = None
    baseFunctions: list[int] | None = None
    constant: bool
    documentation: StructuredDocumentation | None = None
    functionSelector: str | None = None
    indexed: bool | None = None
    mutability: Mutability
    overrides: OverrideSpecifier | None = None
    scope: int
    stateVariable: bool
    storageLocation: StorageLocation
    typeDescriptions: TypeDescriptions
    typeName: "TypeName | None" = None
    value: "Expression | None" = None
    visibility: Visibility
    nodeType: Literal["VariableDeclaration"]


class ParameterList(SolcNode):
    """The parenthesized list of parameters or return values."""

    parameters: list[VariableDeclaration]
    nodeType: Literal["ParameterList"]


class EnumValue(SolcNode):
    """A single member of an enum definition."""

    name: str
    nameLocation: str | None = None
    documentation: StructuredDocumentation | None = None
    nodeType: Literal["EnumValue"]


class EnumDefinition(SolcNode):
    """An ``enum`` definition."""

    name: str
    nameLocation: str | None = None
    canonicalName: str
    members: list[EnumValue]
    documentation: StructuredDocumentation | None = None
    nodeType: Literal["EnumDefinition"]


class ErrorDefinition(SolcNode):
    """A custom ``error`` definition (solc >= 0.8.4)."""

    name: str
    nameLocation: str
    documentation: StructuredDocumentation | None = None
    errorSelector: str | None = None
    parameters: ParameterList
    nodeType: Literal["ErrorDefinition"]


class EventDefinition(SolcNode):
    """An ``event`` definition."""

    name: str
    nameLocation: str | None = None
    anonymous: bool
    eventSelector: str | None = None
    documentation: StructuredDocumentation | None = None
    parameters: ParameterList
    nodeType: Literal["EventDefinition"]


class ModifierInvocation(SolcNode):
    """A modifier (or base-constructor) invocation on a function definition."""

    arguments: "list[Expression] | None" = None
    kind: Literal["modifierInvocation", "baseConstructorSpecifier"] | None = None
    modifierName: "Identifier | IdentifierPath"
    nodeType: Literal["ModifierInvocation"]


class ModifierDefinition(SolcNode):
    """A ``modifier`` definition."""

    name: str
    nameLocation: str | None = None
    baseModifiers: list[int] | None = None
    body: "Block | None" = None
    documentation: StructuredDocumentation | None = None
    overrides: OverrideSpecifier | None = None
    parameters: ParameterList
    virtual: bool
    visibility: Visibility
    nodeType: Literal["ModifierDefinition"]


class FunctionDefinition(SolcNode):
    """A function, constructor, receive/fallback, or free-function definition."""

    name: str
    nameLocation: str | None = None
    baseFunctions: list[int] | None = None
    body: "Block | None" = None
    documentation: StructuredDocumentation | None = None
    functionSelector: str | None = None
    implemented: bool
    kind: Literal["function", "receive", "constructor", "fallback", "freeFunction"]
    modifiers: list[ModifierInvocation]
    overrides: OverrideSpecifier | None = None
    parameters: ParameterList
    returnParameters: ParameterList
    scope: int
    stateMutability: StateMutability
    virtual: bool
    visibility: Visibility
    nodeType: Literal["FunctionDefinition"]


class SymbolAlias(BaseModel):
    """One ``{symbol as local}`` entry of an ImportDirective's symbolAliases."""

    model_config = ConfigDict(extra="allow")

    foreign: "Identifier"
    local: str | None = None
    nameLocation: str | None = None


class ImportDirective(SolcNode):
    """An ``import`` directive."""

    absolutePath: str
    file: str
    nameLocation: str | None = None
    scope: int
    sourceUnit: int
    symbolAliases: list[SymbolAlias]
    unitAlias: str
    nodeType: Literal["ImportDirective"]


class InheritanceSpecifier(SolcNode):
    """A base contract in a contract's inheritance list."""

    arguments: "list[Expression] | None" = None
    baseName: "UserDefinedTypeName | IdentifierPath"
    nodeType: Literal["InheritanceSpecifier"]


class PragmaDirective(SolcNode):
    """A ``pragma`` directive; ``literals`` holds its tokenized pieces."""

    literals: list[str]
    nodeType: Literal["PragmaDirective"]


class StorageLayoutSpecifier(SolcNode):
    """A ``layout at <expr>`` storage-layout specifier (solc >= 0.8.29)."""

    baseSlotExpression: "Expression"
    nodeType: Literal["StorageLayoutSpecifier"]


class StructDefinition(SolcNode):
    """A ``struct`` definition."""

    name: str
    nameLocation: str | None = None
    canonicalName: str
    members: list[VariableDeclaration]
    scope: int
    visibility: Visibility
    documentation: StructuredDocumentation | None = None
    nodeType: Literal["StructDefinition"]


class UserDefinedValueTypeDefinition(SolcNode):
    """A ``type X is <elementary>`` definition (solc >= 0.8.8)."""

    name: str
    nameLocation: str | None = None
    canonicalName: str | None = None
    underlyingType: "TypeName"
    nodeType: Literal["UserDefinedValueTypeDefinition"]


class UsingForFunction(BaseModel):
    """A plain ``{function: <path>}`` entry of a UsingForDirective's functionList."""

    model_config = ConfigDict(extra="allow")

    function: "IdentifierPath"


class UsingForOperator(BaseModel):
    """An ``{operator as <path>}`` entry of a UsingForDirective's functionList."""

    model_config = ConfigDict(extra="allow")

    operator: Literal[
        "&", "|", "^", "~", "+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">="
    ]
    definition: "IdentifierPath"


class UsingForDirective(SolcNode):
    """A ``using ... for ...`` directive."""

    functionList: list[UsingForFunction | UsingForOperator] | None = None
    global_: bool | None = Field(default=None, alias="global")
    libraryName: "UserDefinedTypeName | IdentifierPath | None" = None
    typeName: "TypeName | None" = None
    nodeType: Literal["UsingForDirective"]


class ContractDefinition(SolcNode):
    """A contract, interface, or library definition."""

    name: str
    nameLocation: str | None = None
    abstract: bool
    baseContracts: list[InheritanceSpecifier]
    canonicalName: str | None = None
    contractDependencies: list[int]
    contractKind: Literal["contract", "interface", "library"]
    documentation: StructuredDocumentation | None = None
    fullyImplemented: bool
    linearizedBaseContracts: list[int]
    nodes: list["ContractBodyNode"]
    scope: int
    usedErrors: list[int] | None = None
    usedEvents: list[int] | None = None
    internalFunctionIDs: dict[str, int] | None = None
    storageLayout: StorageLayoutSpecifier | None = None
    nodeType: Literal["ContractDefinition"]

    @field_validator("internalFunctionIDs", mode="before")
    @classmethod
    def _drop_injected_contract_name(cls, value: object) -> object:
        # certoraRun's certora_contract_name stamping walks every dict under a
        # ContractDefinition, including this plain function-id map; drop the injected
        # string entry so the values stay int-typed.
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k != "certora_contract_name"}
        return value


class SourceUnit(SolcNode):
    """The root node of one source file's AST (the schema's top-level object)."""

    absolutePath: str
    exportedSymbols: dict[str, list[int]]
    experimentalSolidity: bool | None = None
    license: str | None = None
    nodes: list["SourceUnitNode"]
    nodeType: Literal["SourceUnit"]
