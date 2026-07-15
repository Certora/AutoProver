"""Expression nodes of the Solidity compact AST (members of the Expression union)."""

from __future__ import annotations

# The AST node class `Literal` below shadows typing.Literal, so this module uses
# `typing.Literal[...]` for all tag/enum annotations instead of importing the name.
import typing

from .base import SolcNode, TypeDescriptions

if typing.TYPE_CHECKING:
    from .types import ElementaryTypeName
    from .unions import Expression, TypeName


class Assignment(SolcNode):
    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    leftHandSide: "Expression"
    operator: typing.Literal[
        "=", "+=", "-=", "*=", "/=", "%=", "|=", "&=", "^=", ">>=", "<<="
    ]
    rightHandSide: "Expression"
    nodeType: typing.Literal["Assignment"]


class BinaryOperation(SolcNode):
    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    commonType: TypeDescriptions
    leftExpression: "Expression"
    operator: typing.Literal[
        "+", "-", "*", "/", "%", "**", "&&", "||", "!=", "==",
        "<", "<=", ">", ">=", "^", "&", "|", "<<", ">>",
    ]
    rightExpression: "Expression"
    function: int | None = None
    nodeType: typing.Literal["BinaryOperation"]


class Conditional(SolcNode):
    """A ternary ``condition ? trueExpression : falseExpression``."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    condition: "Expression"
    falseExpression: "Expression"
    trueExpression: "Expression"
    nodeType: typing.Literal["Conditional"]


class ElementaryTypeNameExpression(SolcNode):
    """An elementary type used as an expression, e.g. the callee in ``uint256(x)``."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    typeName: "ElementaryTypeName"
    nodeType: typing.Literal["ElementaryTypeNameExpression"]


class FunctionCall(SolcNode):
    """A call, type conversion, or struct constructor call (see ``kind``)."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    arguments: list["Expression"]
    expression: "Expression"
    kind: typing.Literal["functionCall", "typeConversion", "structConstructorCall"]
    names: list[str]
    nameLocations: list[str] | None = None
    tryCall: bool
    nodeType: typing.Literal["FunctionCall"]


class FunctionCallOptions(SolcNode):
    """Call options attached to a callee, e.g. ``f{value: 1, gas: 2}``."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool | None = None
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    expression: "Expression"
    names: list[str]
    options: list["Expression"]
    nodeType: typing.Literal["FunctionCallOptions"]


class Identifier(SolcNode):
    argumentTypes: list[TypeDescriptions] | None = None
    name: str
    overloadedDeclarations: list[int]
    referencedDeclaration: int | None = None
    typeDescriptions: TypeDescriptions
    nodeType: typing.Literal["Identifier"]


class IndexAccess(SolcNode):
    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    baseExpression: "Expression"
    indexExpression: "Expression | None" = None
    nodeType: typing.Literal["IndexAccess"]


class IndexRangeAccess(SolcNode):
    """An array slice, e.g. ``arr[1:3]`` (calldata arrays only)."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    baseExpression: "Expression"
    endExpression: "Expression | None" = None
    startExpression: "Expression | None" = None
    nodeType: typing.Literal["IndexRangeAccess"]


class Literal(SolcNode):
    """A literal value (number, string, bool, ...); shadows ``typing.Literal`` here."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    hexValue: str
    kind: typing.Literal["bool", "number", "string", "hexString", "unicodeString"]
    subdenomination: (
        typing.Literal[
            "seconds", "minutes", "hours", "days", "weeks",
            "wei", "gwei", "ether", "finney", "szabo",
        ]
        | None
    ) = None
    value: str | None = None
    nodeType: typing.Literal["Literal"]


class MemberAccess(SolcNode):
    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    expression: "Expression"
    memberName: str
    memberLocation: str | None = None
    referencedDeclaration: int | None = None
    nodeType: typing.Literal["MemberAccess"]


class NewExpression(SolcNode):
    """A ``new T`` expression (contract creation or dynamic-array allocation)."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool | None = None
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    typeName: "TypeName"
    nodeType: typing.Literal["NewExpression"]


class TupleExpression(SolcNode):
    """A tuple or inline array; ``components`` has None holes for omitted entries."""

    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    components: list["Expression | None"]
    isInlineArray: bool
    nodeType: typing.Literal["TupleExpression"]


class UnaryOperation(SolcNode):
    argumentTypes: list[TypeDescriptions] | None = None
    isConstant: bool
    isLValue: bool
    isPure: bool
    lValueRequested: bool
    typeDescriptions: TypeDescriptions
    operator: typing.Literal["++", "--", "-", "!", "delete", "~"]
    prefix: bool
    subExpression: "Expression"
    function: int | None = None
    nodeType: typing.Literal["UnaryOperation"]
