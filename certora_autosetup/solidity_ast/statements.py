"""Statement nodes of the Solidity compact AST (members of the Statement union)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from .base import SolcNode

if TYPE_CHECKING:
    from .declarations import ParameterList, VariableDeclaration
    from .expressions import FunctionCall
    from .unions import Expression, Statement
    from .yul import YulBlock


class Block(SolcNode):
    """A curly-braced statement block."""

    documentation: str | None = None
    statements: "list[Statement] | None" = None
    nodeType: Literal["Block"]


class Break(SolcNode):
    documentation: str | None = None
    nodeType: Literal["Break"]


class Continue(SolcNode):
    documentation: str | None = None
    nodeType: Literal["Continue"]


class DoWhileStatement(SolcNode):
    documentation: str | None = None
    body: "Block | Statement"
    condition: "Expression"
    nodeType: Literal["DoWhileStatement"]


class EmitStatement(SolcNode):
    documentation: str | None = None
    eventCall: "FunctionCall"
    nodeType: Literal["EmitStatement"]


class ExpressionStatement(SolcNode):
    documentation: str | None = None
    expression: "Expression"
    nodeType: Literal["ExpressionStatement"]


class ForStatement(SolcNode):
    documentation: str | None = None
    body: "Block | Statement"
    condition: "Expression | None" = None
    initializationExpression: "ExpressionStatement | VariableDeclarationStatement | None" = None
    loopExpression: ExpressionStatement | None = None
    isSimpleCounterLoop: bool | None = None
    nodeType: Literal["ForStatement"]


class IfStatement(SolcNode):
    documentation: str | None = None
    condition: "Expression"
    falseBody: "Statement | Block | None" = None
    trueBody: "Statement | Block"
    nodeType: Literal["IfStatement"]


class ExternalReference(BaseModel):
    """An entry of ``InlineAssembly.externalReferences``: a Yul identifier that refers
    to a Solidity declaration."""

    model_config = ConfigDict(extra="allow")

    declaration: int
    isOffset: bool
    isSlot: bool
    src: str
    valueSize: int
    suffix: Literal["slot", "offset", "length"] | None = None


class InlineAssembly(SolcNode):
    """An ``assembly { ... }`` block; its Yul body lives under the ``AST`` field."""

    documentation: str | None = None
    AST: "YulBlock"
    # The schema enumerates the EVM fork names, but each new fork would make every
    # assembly-containing source fail whole-file validation until the vendored
    # schema catches up — deliberately open (allowlisted in the conformance test).
    evmVersion: str
    externalReferences: list[ExternalReference]
    # Same reasoning: new assembly flags arrive with new solc releases.
    flags: list[str] | None = None
    nodeType: Literal["InlineAssembly"]


class PlaceholderStatement(SolcNode):
    """The ``_;`` placeholder inside a modifier body."""

    documentation: str | None = None
    nodeType: Literal["PlaceholderStatement"]


class Return(SolcNode):
    documentation: str | None = None
    expression: "Expression | None" = None
    functionReturnParameters: int
    nodeType: Literal["Return"]


class RevertStatement(SolcNode):
    """A ``revert SomeError(...)`` statement (solc >= 0.8.4)."""

    documentation: str | None = None
    errorCall: "FunctionCall"
    nodeType: Literal["RevertStatement"]


class TryStatement(SolcNode):
    documentation: str | None = None
    clauses: "list[TryCatchClause]"
    externalCall: "FunctionCall"
    nodeType: Literal["TryStatement"]


class TryCatchClause(SolcNode):
    """A ``try``-success or ``catch`` clause of a TryStatement."""

    block: Block
    errorName: str
    parameters: "ParameterList | None" = None
    nodeType: Literal["TryCatchClause"]


class UncheckedBlock(SolcNode):
    """An ``unchecked { ... }`` block (solc >= 0.8.0)."""

    documentation: str | None = None
    statements: "list[Statement]"
    nodeType: Literal["UncheckedBlock"]


class VariableDeclarationStatement(SolcNode):
    documentation: str | None = None
    assignments: list[int | None]
    declarations: "list[VariableDeclaration | None]"
    initialValue: "Expression | None" = None
    nodeType: Literal["VariableDeclarationStatement"]


class WhileStatement(SolcNode):
    documentation: str | None = None
    body: "Block | Statement"
    condition: "Expression"
    nodeType: Literal["WhileStatement"]
