"""Discriminated unions over the AST node models, the schema-name registry, and the
forward-reference rebuild wiring.

Import this module (or the package) before validating any node model: importing it
resolves every cross-module forward reference and rebuilds all models. Each union
carries an UnknownNode fallback member selected for any unrecognized ``nodeType``,
so ASTs from solc versions newer than the vendored schema degrade per-node instead
of failing whole-file validation.
"""

from __future__ import annotations

from typing import Annotated, Union

from pydantic import Discriminator, Tag

from . import declarations, expressions, statements, types, yul
from .base import UNKNOWN_TAG, AstNode, UnknownNode, tag_by_node_type
from .yul import YulExpression, YulLiteral, YulStatement


from .types import (
    ArrayTypeName,
    ElementaryTypeName,
    FunctionTypeName,
    IdentifierPath,
    Mapping,
    UserDefinedTypeName,
)
from .expressions import (
    Assignment,
    BinaryOperation,
    Conditional,
    ElementaryTypeNameExpression,
    FunctionCall,
    FunctionCallOptions,
    Identifier,
    IndexAccess,
    IndexRangeAccess,
    Literal,
    MemberAccess,
    NewExpression,
    TupleExpression,
    UnaryOperation,
)
from .statements import (
    Block,
    Break,
    Continue,
    DoWhileStatement,
    EmitStatement,
    ExpressionStatement,
    ForStatement,
    IfStatement,
    InlineAssembly,
    PlaceholderStatement,
    Return,
    RevertStatement,
    TryCatchClause,
    TryStatement,
    UncheckedBlock,
    VariableDeclarationStatement,
    WhileStatement,
)
from .declarations import (
    ContractDefinition,
    EnumDefinition,
    EnumValue,
    ErrorDefinition,
    EventDefinition,
    FunctionDefinition,
    ImportDirective,
    InheritanceSpecifier,
    ModifierDefinition,
    ModifierInvocation,
    OverrideSpecifier,
    ParameterList,
    PragmaDirective,
    SourceUnit,
    StorageLayoutSpecifier,
    StructDefinition,
    StructuredDocumentation,
    UserDefinedValueTypeDefinition,
    UsingForDirective,
    VariableDeclaration,
)
from .yul import (
    YulAssignment,
    YulBlock,
    YulBreak,
    YulCase,
    YulContinue,
    YulExpressionStatement,
    YulForLoop,
    YulFunctionCall,
    YulFunctionDefinition,
    YulIdentifier,
    YulIf,
    YulLeave,
    YulLiteralHexValue,
    YulLiteralValue,
    YulSwitch,
    YulTypedName,
    YulVariableDeclaration,
)

_EXPRESSION_TAGS = frozenset({"Assignment", "BinaryOperation", "Conditional", "ElementaryTypeNameExpression", "FunctionCall", "FunctionCallOptions", "Identifier", "IndexAccess", "IndexRangeAccess", "Literal", "MemberAccess", "NewExpression", "TupleExpression", "UnaryOperation"})

Expression = Annotated[
    Union[
        Annotated[Assignment, Tag("Assignment")],
        Annotated[BinaryOperation, Tag("BinaryOperation")],
        Annotated[Conditional, Tag("Conditional")],
        Annotated[ElementaryTypeNameExpression, Tag("ElementaryTypeNameExpression")],
        Annotated[FunctionCall, Tag("FunctionCall")],
        Annotated[FunctionCallOptions, Tag("FunctionCallOptions")],
        Annotated[Identifier, Tag("Identifier")],
        Annotated[IndexAccess, Tag("IndexAccess")],
        Annotated[IndexRangeAccess, Tag("IndexRangeAccess")],
        Annotated[Literal, Tag("Literal")],
        Annotated[MemberAccess, Tag("MemberAccess")],
        Annotated[NewExpression, Tag("NewExpression")],
        Annotated[TupleExpression, Tag("TupleExpression")],
        Annotated[UnaryOperation, Tag("UnaryOperation")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(tag_by_node_type(_EXPRESSION_TAGS)),
]
"""Any Solidity expression node (or UnknownNode)."""

_STATEMENT_TAGS = frozenset({"Block", "Break", "Continue", "DoWhileStatement", "EmitStatement", "ExpressionStatement", "ForStatement", "IfStatement", "InlineAssembly", "PlaceholderStatement", "Return", "RevertStatement", "TryStatement", "UncheckedBlock", "VariableDeclarationStatement", "WhileStatement"})

Statement = Annotated[
    Union[
        Annotated[Block, Tag("Block")],
        Annotated[Break, Tag("Break")],
        Annotated[Continue, Tag("Continue")],
        Annotated[DoWhileStatement, Tag("DoWhileStatement")],
        Annotated[EmitStatement, Tag("EmitStatement")],
        Annotated[ExpressionStatement, Tag("ExpressionStatement")],
        Annotated[ForStatement, Tag("ForStatement")],
        Annotated[IfStatement, Tag("IfStatement")],
        Annotated[InlineAssembly, Tag("InlineAssembly")],
        Annotated[PlaceholderStatement, Tag("PlaceholderStatement")],
        Annotated[Return, Tag("Return")],
        Annotated[RevertStatement, Tag("RevertStatement")],
        Annotated[TryStatement, Tag("TryStatement")],
        Annotated[UncheckedBlock, Tag("UncheckedBlock")],
        Annotated[VariableDeclarationStatement, Tag("VariableDeclarationStatement")],
        Annotated[WhileStatement, Tag("WhileStatement")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(tag_by_node_type(_STATEMENT_TAGS)),
]
"""Any Solidity statement node (or UnknownNode)."""

_TYPENAME_TAGS = frozenset({"ArrayTypeName", "ElementaryTypeName", "FunctionTypeName", "Mapping", "UserDefinedTypeName"})

TypeName = Annotated[
    Union[
        Annotated[ArrayTypeName, Tag("ArrayTypeName")],
        Annotated[ElementaryTypeName, Tag("ElementaryTypeName")],
        Annotated[FunctionTypeName, Tag("FunctionTypeName")],
        Annotated[Mapping, Tag("Mapping")],
        Annotated[UserDefinedTypeName, Tag("UserDefinedTypeName")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(tag_by_node_type(_TYPENAME_TAGS)),
]
"""Any type-name node (or UnknownNode)."""

# EventDefinition is absent from the vendored schema's SourceUnit.nodes union, but
# solc >= 0.8.22 allows file-level events (seen in the wild; conformance deviation
# DELIBERATELY_OPEN on SourceUnit.nodes).
_SOURCEUNITNODE_TAGS = frozenset({"ContractDefinition", "EnumDefinition", "ErrorDefinition", "EventDefinition", "FunctionDefinition", "ImportDirective", "PragmaDirective", "StructDefinition", "UserDefinedValueTypeDefinition", "UsingForDirective", "VariableDeclaration"})

SourceUnitNode = Annotated[
    Union[
        Annotated[ContractDefinition, Tag("ContractDefinition")],
        Annotated[EnumDefinition, Tag("EnumDefinition")],
        Annotated[ErrorDefinition, Tag("ErrorDefinition")],
        Annotated[EventDefinition, Tag("EventDefinition")],
        Annotated[FunctionDefinition, Tag("FunctionDefinition")],
        Annotated[ImportDirective, Tag("ImportDirective")],
        Annotated[PragmaDirective, Tag("PragmaDirective")],
        Annotated[StructDefinition, Tag("StructDefinition")],
        Annotated[UserDefinedValueTypeDefinition, Tag("UserDefinedValueTypeDefinition")],
        Annotated[UsingForDirective, Tag("UsingForDirective")],
        Annotated[VariableDeclaration, Tag("VariableDeclaration")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(tag_by_node_type(_SOURCEUNITNODE_TAGS)),
]
"""Any node that may appear directly in SourceUnit.nodes (or UnknownNode)."""

_CONTRACTBODYNODE_TAGS = frozenset({"EnumDefinition", "ErrorDefinition", "EventDefinition", "FunctionDefinition", "ModifierDefinition", "StructDefinition", "UserDefinedValueTypeDefinition", "UsingForDirective", "VariableDeclaration"})

ContractBodyNode = Annotated[
    Union[
        Annotated[EnumDefinition, Tag("EnumDefinition")],
        Annotated[ErrorDefinition, Tag("ErrorDefinition")],
        Annotated[EventDefinition, Tag("EventDefinition")],
        Annotated[FunctionDefinition, Tag("FunctionDefinition")],
        Annotated[ModifierDefinition, Tag("ModifierDefinition")],
        Annotated[StructDefinition, Tag("StructDefinition")],
        Annotated[UserDefinedValueTypeDefinition, Tag("UserDefinedValueTypeDefinition")],
        Annotated[UsingForDirective, Tag("UsingForDirective")],
        Annotated[VariableDeclaration, Tag("VariableDeclaration")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(tag_by_node_type(_CONTRACTBODYNODE_TAGS)),
]
"""Any node that may appear directly in ContractDefinition.nodes (or UnknownNode)."""

_NODE_TAGS = frozenset({"ArrayTypeName", "Assignment", "BinaryOperation", "Block", "Break", "Conditional", "Continue", "ContractDefinition", "DoWhileStatement", "ElementaryTypeName", "ElementaryTypeNameExpression", "EmitStatement", "EnumDefinition", "EnumValue", "ErrorDefinition", "EventDefinition", "ExpressionStatement", "ForStatement", "FunctionCall", "FunctionCallOptions", "FunctionDefinition", "FunctionTypeName", "Identifier", "IdentifierPath", "IfStatement", "ImportDirective", "IndexAccess", "IndexRangeAccess", "InheritanceSpecifier", "InlineAssembly", "Literal", "Mapping", "MemberAccess", "ModifierDefinition", "ModifierInvocation", "NewExpression", "OverrideSpecifier", "ParameterList", "PlaceholderStatement", "PragmaDirective", "Return", "RevertStatement", "SourceUnit", "StorageLayoutSpecifier", "StructDefinition", "StructuredDocumentation", "TryCatchClause", "TryStatement", "TupleExpression", "UnaryOperation", "UncheckedBlock", "UserDefinedTypeName", "UserDefinedValueTypeDefinition", "UsingForDirective", "VariableDeclaration", "VariableDeclarationStatement", "WhileStatement", "YulAssignment", "YulBlock", "YulBreak", "YulCase", "YulContinue", "YulExpressionStatement", "YulForLoop", "YulFunctionCall", "YulFunctionDefinition", "YulIdentifier", "YulIf", "YulLeave", "YulLiteral", "YulSwitch", "YulTypedName", "YulVariableDeclaration"})

Node = Annotated[
    Union[
        Annotated[ArrayTypeName, Tag("ArrayTypeName")],
        Annotated[Assignment, Tag("Assignment")],
        Annotated[BinaryOperation, Tag("BinaryOperation")],
        Annotated[Block, Tag("Block")],
        Annotated[Break, Tag("Break")],
        Annotated[Conditional, Tag("Conditional")],
        Annotated[Continue, Tag("Continue")],
        Annotated[ContractDefinition, Tag("ContractDefinition")],
        Annotated[DoWhileStatement, Tag("DoWhileStatement")],
        Annotated[ElementaryTypeName, Tag("ElementaryTypeName")],
        Annotated[ElementaryTypeNameExpression, Tag("ElementaryTypeNameExpression")],
        Annotated[EmitStatement, Tag("EmitStatement")],
        Annotated[EnumDefinition, Tag("EnumDefinition")],
        Annotated[EnumValue, Tag("EnumValue")],
        Annotated[ErrorDefinition, Tag("ErrorDefinition")],
        Annotated[EventDefinition, Tag("EventDefinition")],
        Annotated[ExpressionStatement, Tag("ExpressionStatement")],
        Annotated[ForStatement, Tag("ForStatement")],
        Annotated[FunctionCall, Tag("FunctionCall")],
        Annotated[FunctionCallOptions, Tag("FunctionCallOptions")],
        Annotated[FunctionDefinition, Tag("FunctionDefinition")],
        Annotated[FunctionTypeName, Tag("FunctionTypeName")],
        Annotated[Identifier, Tag("Identifier")],
        Annotated[IdentifierPath, Tag("IdentifierPath")],
        Annotated[IfStatement, Tag("IfStatement")],
        Annotated[ImportDirective, Tag("ImportDirective")],
        Annotated[IndexAccess, Tag("IndexAccess")],
        Annotated[IndexRangeAccess, Tag("IndexRangeAccess")],
        Annotated[InheritanceSpecifier, Tag("InheritanceSpecifier")],
        Annotated[InlineAssembly, Tag("InlineAssembly")],
        Annotated[Literal, Tag("Literal")],
        Annotated[Mapping, Tag("Mapping")],
        Annotated[MemberAccess, Tag("MemberAccess")],
        Annotated[ModifierDefinition, Tag("ModifierDefinition")],
        Annotated[ModifierInvocation, Tag("ModifierInvocation")],
        Annotated[NewExpression, Tag("NewExpression")],
        Annotated[OverrideSpecifier, Tag("OverrideSpecifier")],
        Annotated[ParameterList, Tag("ParameterList")],
        Annotated[PlaceholderStatement, Tag("PlaceholderStatement")],
        Annotated[PragmaDirective, Tag("PragmaDirective")],
        Annotated[Return, Tag("Return")],
        Annotated[RevertStatement, Tag("RevertStatement")],
        Annotated[SourceUnit, Tag("SourceUnit")],
        Annotated[StorageLayoutSpecifier, Tag("StorageLayoutSpecifier")],
        Annotated[StructDefinition, Tag("StructDefinition")],
        Annotated[StructuredDocumentation, Tag("StructuredDocumentation")],
        Annotated[TryCatchClause, Tag("TryCatchClause")],
        Annotated[TryStatement, Tag("TryStatement")],
        Annotated[TupleExpression, Tag("TupleExpression")],
        Annotated[UnaryOperation, Tag("UnaryOperation")],
        Annotated[UncheckedBlock, Tag("UncheckedBlock")],
        Annotated[UserDefinedTypeName, Tag("UserDefinedTypeName")],
        Annotated[UserDefinedValueTypeDefinition, Tag("UserDefinedValueTypeDefinition")],
        Annotated[UsingForDirective, Tag("UsingForDirective")],
        Annotated[VariableDeclaration, Tag("VariableDeclaration")],
        Annotated[VariableDeclarationStatement, Tag("VariableDeclarationStatement")],
        Annotated[WhileStatement, Tag("WhileStatement")],
        Annotated[YulAssignment, Tag("YulAssignment")],
        Annotated[YulBlock, Tag("YulBlock")],
        Annotated[YulBreak, Tag("YulBreak")],
        Annotated[YulCase, Tag("YulCase")],
        Annotated[YulContinue, Tag("YulContinue")],
        Annotated[YulExpressionStatement, Tag("YulExpressionStatement")],
        Annotated[YulForLoop, Tag("YulForLoop")],
        Annotated[YulFunctionCall, Tag("YulFunctionCall")],
        Annotated[YulFunctionDefinition, Tag("YulFunctionDefinition")],
        Annotated[YulIdentifier, Tag("YulIdentifier")],
        Annotated[YulIf, Tag("YulIf")],
        Annotated[YulLeave, Tag("YulLeave")],
        Annotated[YulLiteralValue | YulLiteralHexValue, Tag("YulLiteral")],
        Annotated[YulSwitch, Tag("YulSwitch")],
        Annotated[YulTypedName, Tag("YulTypedName")],
        Annotated[YulVariableDeclaration, Tag("YulVariableDeclaration")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(tag_by_node_type(_NODE_TAGS)),
]
"""Any concrete AST node of any kind (or UnknownNode)."""

# Schema definition name -> model class ("SourceUnit" is the schema root, the two
# YulLiteral* variants share nodeType "YulLiteral").
MODEL_BY_SCHEMA_DEF: dict[str, type[AstNode]] = {
    "ArrayTypeName": types.ArrayTypeName,
    "Assignment": expressions.Assignment,
    "BinaryOperation": expressions.BinaryOperation,
    "Block": statements.Block,
    "Break": statements.Break,
    "Conditional": expressions.Conditional,
    "Continue": statements.Continue,
    "ContractDefinition": declarations.ContractDefinition,
    "DoWhileStatement": statements.DoWhileStatement,
    "ElementaryTypeName": types.ElementaryTypeName,
    "ElementaryTypeNameExpression": expressions.ElementaryTypeNameExpression,
    "EmitStatement": statements.EmitStatement,
    "EnumDefinition": declarations.EnumDefinition,
    "EnumValue": declarations.EnumValue,
    "ErrorDefinition": declarations.ErrorDefinition,
    "EventDefinition": declarations.EventDefinition,
    "ExpressionStatement": statements.ExpressionStatement,
    "ForStatement": statements.ForStatement,
    "FunctionCall": expressions.FunctionCall,
    "FunctionCallOptions": expressions.FunctionCallOptions,
    "FunctionDefinition": declarations.FunctionDefinition,
    "FunctionTypeName": types.FunctionTypeName,
    "Identifier": expressions.Identifier,
    "IdentifierPath": types.IdentifierPath,
    "IfStatement": statements.IfStatement,
    "ImportDirective": declarations.ImportDirective,
    "IndexAccess": expressions.IndexAccess,
    "IndexRangeAccess": expressions.IndexRangeAccess,
    "InheritanceSpecifier": declarations.InheritanceSpecifier,
    "InlineAssembly": statements.InlineAssembly,
    "Literal": expressions.Literal,
    "Mapping": types.Mapping,
    "MemberAccess": expressions.MemberAccess,
    "ModifierDefinition": declarations.ModifierDefinition,
    "ModifierInvocation": declarations.ModifierInvocation,
    "NewExpression": expressions.NewExpression,
    "OverrideSpecifier": declarations.OverrideSpecifier,
    "ParameterList": declarations.ParameterList,
    "PlaceholderStatement": statements.PlaceholderStatement,
    "PragmaDirective": declarations.PragmaDirective,
    "Return": statements.Return,
    "RevertStatement": statements.RevertStatement,
    "SourceUnit": declarations.SourceUnit,
    "StorageLayoutSpecifier": declarations.StorageLayoutSpecifier,
    "StructDefinition": declarations.StructDefinition,
    "StructuredDocumentation": declarations.StructuredDocumentation,
    "TryCatchClause": statements.TryCatchClause,
    "TryStatement": statements.TryStatement,
    "TupleExpression": expressions.TupleExpression,
    "UnaryOperation": expressions.UnaryOperation,
    "UncheckedBlock": statements.UncheckedBlock,
    "UserDefinedTypeName": types.UserDefinedTypeName,
    "UserDefinedValueTypeDefinition": declarations.UserDefinedValueTypeDefinition,
    "UsingForDirective": declarations.UsingForDirective,
    "VariableDeclaration": declarations.VariableDeclaration,
    "VariableDeclarationStatement": statements.VariableDeclarationStatement,
    "WhileStatement": statements.WhileStatement,
    "YulAssignment": yul.YulAssignment,
    "YulBlock": yul.YulBlock,
    "YulBreak": yul.YulBreak,
    "YulCase": yul.YulCase,
    "YulContinue": yul.YulContinue,
    "YulExpressionStatement": yul.YulExpressionStatement,
    "YulForLoop": yul.YulForLoop,
    "YulFunctionCall": yul.YulFunctionCall,
    "YulFunctionDefinition": yul.YulFunctionDefinition,
    "YulIdentifier": yul.YulIdentifier,
    "YulIf": yul.YulIf,
    "YulLeave": yul.YulLeave,
    "YulLiteralHexValue": yul.YulLiteralHexValue,
    "YulLiteralValue": yul.YulLiteralValue,
    "YulSwitch": yul.YulSwitch,
    "YulTypedName": yul.YulTypedName,
    "YulVariableDeclaration": yul.YulVariableDeclaration,
}


_UNION_ALIASES: dict[str, object] = {
    "Expression": Expression,
    "Statement": Statement,
    "TypeName": TypeName,
    "SourceUnitNode": SourceUnitNode,
    "ContractBodyNode": ContractBodyNode,
    "Node": Node,
    "YulStatement": YulStatement,
    "YulExpression": YulExpression,
    "YulLiteral": YulLiteral,
}

_NAMESPACE: dict[str, object] = {
    **{cls.__name__: cls for cls in MODEL_BY_SCHEMA_DEF.values()},
    **_UNION_ALIASES,
}

# Node modules reference classes and unions from sibling modules as string forward
# refs only (they import nothing from each other at runtime). Resolve everything by
# injecting the shared namespace into each module's globals — never clobbering a
# name the module already defines (e.g. typing.Literal vs the Literal node class) —
# and rebuild every model once.
for _mod in (types, expressions, statements, declarations, yul):
    for _name, _obj in _NAMESPACE.items():
        if not hasattr(_mod, _name):
            setattr(_mod, _name, _obj)

for _cls in {*MODEL_BY_SCHEMA_DEF.values(), UnknownNode}:
    _cls.model_rebuild(force=True)

