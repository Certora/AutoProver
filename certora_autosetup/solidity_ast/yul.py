"""Yul AST nodes (the ``AST`` of an ``InlineAssembly`` node, solc >= 0.6).

Self-contained: unlike the Solidity node modules, the union aliases
(``YulLiteral``/``YulExpression``/``YulStatement``) are defined here and all models
are rebuilt at import time, so this module validates on its own.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Discriminator, Tag

from .base import UNKNOWN_TAG, UnknownNode, YulNode, tag_by_node_type


class YulAssignment(YulNode):
    value: YulExpression
    variableNames: list[YulIdentifier]
    nodeType: Literal["YulAssignment"]


class YulBlock(YulNode):
    statements: list[YulStatement]
    nodeType: Literal["YulBlock"]


class YulBreak(YulNode):
    nodeType: Literal["YulBreak"]


class YulCase(YulNode):
    body: YulBlock
    value: Literal["default"] | YulLiteral
    nodeType: Literal["YulCase"]


class YulContinue(YulNode):
    nodeType: Literal["YulContinue"]


class YulExpressionStatement(YulNode):
    expression: YulExpression
    nodeType: Literal["YulExpressionStatement"]


class YulForLoop(YulNode):
    body: YulBlock
    condition: YulExpression
    post: YulBlock
    pre: YulBlock
    nodeType: Literal["YulForLoop"]


class YulFunctionCall(YulNode):
    arguments: list[YulExpression]
    functionName: YulIdentifier
    nodeType: Literal["YulFunctionCall"]


class YulFunctionDefinition(YulNode):
    body: YulBlock
    name: str
    parameters: list[YulTypedName] | None = None
    returnVariables: list[YulTypedName] | None = None
    nodeType: Literal["YulFunctionDefinition"]


class YulIdentifier(YulNode):
    name: str
    nodeType: Literal["YulIdentifier"]


class YulIf(YulNode):
    body: YulBlock
    condition: YulExpression
    nodeType: Literal["YulIf"]


class YulLeave(YulNode):
    nodeType: Literal["YulLeave"]


class YulLiteralValue(YulNode):
    value: str
    kind: Literal["number", "string", "bool"]
    type: str
    nodeType: Literal["YulLiteral"]


class YulLiteralHexValue(YulNode):
    hexValue: str
    kind: Literal["number", "string", "bool"]
    type: str
    value: str | None = None
    nodeType: Literal["YulLiteral"]


class YulSwitch(YulNode):
    cases: list[YulCase]
    expression: YulExpression
    nodeType: Literal["YulSwitch"]


class YulTypedName(YulNode):
    name: str
    type: str
    nodeType: Literal["YulTypedName"]


class YulVariableDeclaration(YulNode):
    value: YulExpression | None = None
    variables: list[YulTypedName]
    nodeType: Literal["YulVariableDeclaration"]


# Union aliases mirroring the schema's helper definitions. Both YulLiteral variants
# share the "YulLiteral" tag, so within that branch pydantic picks by fields.
YulLiteral = YulLiteralValue | YulLiteralHexValue

YulExpression = Annotated[
    Union[
        Annotated[YulFunctionCall, Tag("YulFunctionCall")],
        Annotated[YulIdentifier, Tag("YulIdentifier")],
        Annotated[YulLiteralValue | YulLiteralHexValue, Tag("YulLiteral")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(
        tag_by_node_type(frozenset({"YulFunctionCall", "YulIdentifier", "YulLiteral"}))
    ),
]

YulStatement = Annotated[
    Union[
        Annotated[YulAssignment, Tag("YulAssignment")],
        Annotated[YulBlock, Tag("YulBlock")],
        Annotated[YulBreak, Tag("YulBreak")],
        Annotated[YulContinue, Tag("YulContinue")],
        Annotated[YulExpressionStatement, Tag("YulExpressionStatement")],
        Annotated[YulLeave, Tag("YulLeave")],
        Annotated[YulForLoop, Tag("YulForLoop")],
        Annotated[YulFunctionDefinition, Tag("YulFunctionDefinition")],
        Annotated[YulIf, Tag("YulIf")],
        Annotated[YulSwitch, Tag("YulSwitch")],
        Annotated[YulVariableDeclaration, Tag("YulVariableDeclaration")],
        Annotated[UnknownNode, Tag(UNKNOWN_TAG)],
    ],
    Discriminator(
        tag_by_node_type(
            frozenset(
                {
                    "YulAssignment",
                    "YulBlock",
                    "YulBreak",
                    "YulContinue",
                    "YulExpressionStatement",
                    "YulLeave",
                    "YulForLoop",
                    "YulFunctionDefinition",
                    "YulIf",
                    "YulSwitch",
                    "YulVariableDeclaration",
                }
            )
        )
    ),
]

# The Yul namespace is fully defined above, so forward refs resolve right here.
YulAssignment.model_rebuild()
YulBlock.model_rebuild()
YulBreak.model_rebuild()
YulCase.model_rebuild()
YulContinue.model_rebuild()
YulExpressionStatement.model_rebuild()
YulForLoop.model_rebuild()
YulFunctionCall.model_rebuild()
YulFunctionDefinition.model_rebuild()
YulIdentifier.model_rebuild()
YulIf.model_rebuild()
YulLeave.model_rebuild()
YulLiteralValue.model_rebuild()
YulLiteralHexValue.model_rebuild()
YulSwitch.model_rebuild()
YulTypedName.model_rebuild()
YulVariableDeclaration.model_rebuild()
