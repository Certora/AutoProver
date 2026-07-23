"""Base classes and shared value types for the Solidity compact-AST pydantic models.

The models in this subpackage describe the solc "compact AST" (the ``ast`` entry of
solc standard-json output for each source file), as it appears inside the
``.asts.json`` dump produced by ``certoraRun --dump_asts``. Field sets are
transcribed from the vendored OpenZeppelin ``solidity-ast`` JSON Schema
(see ``schema/schema.json`` and ``schema/NOTICE``), which unions solc versions
>= 0.6 into a single schema. ``tests/solidity_ast/test_schema_conformance.py``
machine-checks every model against that schema.

Transcription conventions (uniform across all node modules):

- schema-required property            -> plain annotated field, no default
- schema-optional property (absent
  from ``required``; version-gated)   -> ``T | None = None``
- required-but-nullable property
  (``anyOf [T, null]``)               -> ``T | None`` with NO default
- ``nodeType``                        -> ``Literal["X"]`` (the union discriminator)
- reference to a schema union helper
  (Expression/Statement/TypeName/...) -> string forward ref to the union alias,
                                         resolved by ``unions.model_rebuild`` wiring
- schema enum property                -> shared ``Literal`` alias below when it matches a
                                         helper definition, inline ``Literal[...]`` otherwise
- python-keyword property name        -> trailing-underscore field with ``alias=`` (the only
                                         known case is ``UsingForDirective.global``)
"""

from typing import Callable, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

# Shared enum aliases, mirroring the schema's helper definitions of the same names.
Visibility = Literal["external", "public", "internal", "private"]
StateMutability = Literal["payable", "pure", "nonpayable", "view"]
Mutability = Literal["mutable", "immutable", "constant"]
StorageLocation = Literal["calldata", "default", "memory", "storage", "transient"]


class SrcLocation(NamedTuple):
    """Decoded solc source location ("offset:length:fileIndex", byte-based)."""

    offset: int
    length: int
    file_index: int


def parse_src(src: str) -> SrcLocation:
    """Parse a solc ``src`` string ("offset:length:fileIndex") into byte offsets.

    Raises ValueError on malformed input. ``fileIndex`` may be -1 for nodes solc
    synthesizes without a source file.
    """
    offset, length, file_index = src.split(":")
    return SrcLocation(int(offset), int(length), int(file_index))


class AstNode(BaseModel):
    """Common base of every Solidity and Yul compact-AST node.

    ``extra="allow"`` keeps fields from newer solc releases (not yet in the vendored
    schema) available via ``model_extra`` instead of failing validation.
    """

    model_config = ConfigDict(
        extra="allow", validate_by_name=True, serialize_by_alias=True
    )

    src: str
    # Injected by certoraRun into the dump on nodes enclosed in a ContractDefinition;
    # never present in raw solc output.
    certora_contract_name: str | None = None

    @property
    def src_location(self) -> SrcLocation:
        return parse_src(self.src)


class SolcNode(AstNode):
    """A Solidity-language node: always carries a numeric ``id``."""

    id: int


class YulNode(AstNode):
    """A Yul node (inside ``InlineAssembly.AST``): no ``id``; ``src`` points into the
    original Solidity source and ``nativeSrc`` (solc >= 0.8.21) into the generated Yul.
    """

    nativeSrc: str | None = None


class UnknownNode(AstNode):
    """Fallback member of every node union: a node whose ``nodeType`` this model set
    does not know (newer solc than the vendored schema, or an exotic construct).

    All its fields land in ``model_extra`` and its children stay raw dicts; typed
    queries skip it, so an unknown node degrades gracefully instead of failing the
    whole source file.
    """

    nodeType: str
    id: int | None = None
    # Lenient: synthesized nodes may lack src; "" fails any src-offset use downstream
    # the same way a missing node would.
    src: str = ""


UNKNOWN_TAG = "__unknown__"


def tag_by_node_type(known: frozenset[str]) -> Callable[[object], str]:
    """Discriminator function for a node union: returns the ``nodeType`` tag when it is
    one of ``known``, else ``UNKNOWN_TAG`` (routing to the union's UnknownNode member).
    Handles both dicts (validation) and model instances (serialization).
    """

    def tag(value: object) -> str:
        node_type = (
            value.get("nodeType")
            if isinstance(value, dict)
            else getattr(value, "nodeType", None)
        )
        return node_type if isinstance(node_type, str) and node_type in known else UNKNOWN_TAG

    return tag


class TypeDescriptions(BaseModel):
    """The ``typeDescriptions`` object attached to expressions and type names."""

    model_config = ConfigDict(extra="allow")

    typeIdentifier: str | None = None
    typeString: str | None = None
