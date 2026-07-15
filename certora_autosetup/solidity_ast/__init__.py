"""Typed pydantic models of the Solidity compact AST as dumped by
``certoraRun --dump_asts`` (see ``base.py`` for the modeling conventions and
``loader.py`` for the dump structure and degradation policy).

Typical use::

    from certora_autosetup.solidity_ast import AstDump, ContractDefinition, find_all

    dump = AstDump.load(".certora_internal/all_asts.json")
    for _, _, source_unit in dump.iter_parsed_roots():
        for contract in find_all(source_unit, ContractDefinition):
            ...
"""

__all__ = [
    # base
    "AstNode", "SolcNode", "YulNode", "UnknownNode", "SrcLocation", "parse_src",
    "TypeDescriptions", "Visibility", "StateMutability", "Mutability", "StorageLocation",
    # loader
    "AstDump", "FileAsts", "SourceAst",
    # traversal
    "iter_children", "walk", "find_all", "build_node_index", "build_parent_map",
    "build_parent_graph_json",
    # unions
    "unions", "MODEL_BY_SCHEMA_DEF", "Node", "Expression", "Statement", "TypeName",
    "SourceUnitNode", "ContractBodyNode",
    # types
    "ArrayTypeName", "ElementaryTypeName", "FunctionTypeName", "IdentifierPath",
    "Mapping", "UserDefinedTypeName",
    # expressions
    "Assignment", "BinaryOperation", "Conditional", "ElementaryTypeNameExpression",
    "FunctionCall", "FunctionCallOptions", "Identifier", "IndexAccess", "IndexRangeAccess",
    "Literal", "MemberAccess", "NewExpression", "TupleExpression", "UnaryOperation",
    # statements
    "Block", "Break", "Continue", "DoWhileStatement", "EmitStatement", "ExpressionStatement",
    "ForStatement", "IfStatement", "InlineAssembly", "PlaceholderStatement", "Return",
    "RevertStatement", "TryCatchClause", "TryStatement", "UncheckedBlock",
    "VariableDeclarationStatement", "WhileStatement",
    # declarations
    "ContractDefinition", "EnumDefinition", "EnumValue", "ErrorDefinition", "EventDefinition",
    "FunctionDefinition", "ImportDirective", "InheritanceSpecifier", "ModifierDefinition",
    "ModifierInvocation", "OverrideSpecifier", "ParameterList", "PragmaDirective", "SourceUnit",
    "StorageLayoutSpecifier", "StructDefinition", "StructuredDocumentation",
    "UserDefinedValueTypeDefinition", "UsingForDirective", "VariableDeclaration",
    # yul
    "YulAssignment", "YulBlock", "YulBreak", "YulCase", "YulContinue", "YulExpression",
    "YulExpressionStatement", "YulForLoop", "YulFunctionCall", "YulFunctionDefinition",
    "YulIdentifier", "YulIf", "YulLeave", "YulLiteral", "YulLiteralHexValue",
    "YulLiteralValue", "YulStatement", "YulSwitch", "YulTypedName", "YulVariableDeclaration",
]

# Importing .unions resolves all cross-module forward references and rebuilds the
# models; loader imports it, and the explicit re-import keeps the ordering obvious.
from . import unions as unions
from . import yul as yul
from .base import (
    AstNode,
    Mutability,
    SolcNode,
    SrcLocation,
    StateMutability,
    StorageLocation,
    TypeDescriptions,
    UnknownNode,
    Visibility,
    YulNode,
    parse_src,
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
from .loader import AstDump, FileAsts, SourceAst
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
from .traversal import (
    build_node_index,
    build_parent_graph_json,
    build_parent_map,
    find_all,
    iter_children,
    walk,
)
from .types import (
    ArrayTypeName,
    ElementaryTypeName,
    FunctionTypeName,
    IdentifierPath,
    Mapping,
    UserDefinedTypeName,
)
from .unions import (
    MODEL_BY_SCHEMA_DEF,
    ContractBodyNode,
    Expression,
    Node,
    SourceUnitNode,
    Statement,
    TypeName,
)
from .yul import (
    YulAssignment,
    YulBlock,
    YulBreak,
    YulCase,
    YulContinue,
    YulExpression,
    YulExpressionStatement,
    YulForLoop,
    YulFunctionCall,
    YulFunctionDefinition,
    YulIdentifier,
    YulIf,
    YulLeave,
    YulLiteral,
    YulLiteralHexValue,
    YulLiteralValue,
    YulStatement,
    YulSwitch,
    YulTypedName,
    YulVariableDeclaration,
)
