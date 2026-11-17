"""Typed rustdoc JSON (``cargo rustdoc --output-format json``): the parts the CVLR API producer reads.

The format is nightly-only and unstable, and this reads one :data:`FORMAT_VERSION`. Every record the
producer walks is validated on load, so a field that moves fails the load instead of reading as
absent. Type expressions (:data:`TypeNode`) stay JSON: they are a deep tree the producer renders
with a marker for any shape it does not model.
"""

import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    ValidationError,
)

#: The rustdoc JSON schema this module speaks.
FORMAT_VERSION = 60


class UnreadableRustdoc(RuntimeError):
    """The JSON is not the format this module reads."""


class _Rustdoc(BaseModel):
    """rustdoc adds fields across nightlies; only the ones read here are declared."""

    model_config = ConfigDict(frozen=True, extra="ignore")


type Id = int
type TypeNode = JsonValue


class Deprecation(_Rustdoc):
    since: str | None = None
    note: str | None = None


class RestrictedTo(_Rustdoc):
    parent: Id
    path: str


class Restricted(_Rustdoc):
    restricted: RestrictedTo


type Visibility = Literal["public", "default", "crate"] | Restricted


class GenericParam(_Rustdoc):
    name: str


class Generics(_Rustdoc):
    params: tuple[GenericParam, ...]


class FunctionHeader(_Rustdoc):
    is_const: bool
    is_unsafe: bool


class FunctionSignature(_Rustdoc):
    inputs: tuple[tuple[str, TypeNode], ...]
    output: TypeNode


class Function(_Rustdoc):
    sig: FunctionSignature
    generics: Generics
    header: FunctionHeader


class Module(_Rustdoc):
    items: tuple[Id, ...]


class Use(_Rustdoc):
    """``name`` is the name the ``use`` binds, which is the rename when there is one."""

    name: str
    id: Id | None
    is_glob: bool


class TraitPath(_Rustdoc):
    path: str
    id: Id
    args: JsonValue = None


class Impl(_Rustdoc):
    trait_: TraitPath | None = Field(alias="trait")
    for_: TypeNode = Field(alias="for")
    items: tuple[Id, ...]


class ProcMacro(_Rustdoc):
    kind: Literal["bang", "attr", "derive"]


class Declaration(_Rustdoc):
    """A struct, enum, union, trait or trait alias: what a signature shows of one is its generics."""

    generics: Generics


class TypeAlias(_Rustdoc):
    type_: TypeNode = Field(alias="type")
    generics: Generics


class Typed(_Rustdoc):
    """A constant or a static."""

    type_: TypeNode = Field(alias="type")


class ModuleInner(_Rustdoc):
    module: Module


class UseInner(_Rustdoc):
    use: Use


class ImplInner(_Rustdoc):
    impl: Impl


class FunctionInner(_Rustdoc):
    function: Function


class ProcMacroInner(_Rustdoc):
    proc_macro: ProcMacro


class MacroInner(_Rustdoc):
    """A ``macro_rules!`` macro, given as its declaration with the bodies elided."""

    macro: str


class StructInner(_Rustdoc):
    struct: Declaration


class EnumInner(_Rustdoc):
    enum: Declaration


class UnionInner(_Rustdoc):
    union: Declaration


class TraitInner(_Rustdoc):
    trait_: Declaration = Field(alias="trait")


class TraitAliasInner(_Rustdoc):
    trait_alias: Declaration


class TypeAliasInner(_Rustdoc):
    type_alias: TypeAlias


class ConstantInner(_Rustdoc):
    constant: Typed


class StaticInner(_Rustdoc):
    static: Typed


class OtherInner(_Rustdoc):
    """A kind the producer does not read: a field, a variant, an associated item, a primitive."""


_INNER_TAGS = frozenset(
    {
        "module", "use", "impl", "function", "proc_macro", "macro", "struct", "enum", "union",
        "trait", "trait_alias", "type_alias", "constant", "static",
    }
)


def _inner_tag(value: object) -> str | None:
    """rustdoc tags an item's kind as the single key of ``inner``."""
    if not isinstance(value, dict) or len(value) != 1:
        return None
    (tag,) = value
    return tag if tag in _INNER_TAGS else "other"


type Inner = Annotated[
    Annotated[ModuleInner, Tag("module")]
    | Annotated[UseInner, Tag("use")]
    | Annotated[ImplInner, Tag("impl")]
    | Annotated[FunctionInner, Tag("function")]
    | Annotated[ProcMacroInner, Tag("proc_macro")]
    | Annotated[MacroInner, Tag("macro")]
    | Annotated[StructInner, Tag("struct")]
    | Annotated[EnumInner, Tag("enum")]
    | Annotated[UnionInner, Tag("union")]
    | Annotated[TraitInner, Tag("trait")]
    | Annotated[TraitAliasInner, Tag("trait_alias")]
    | Annotated[TypeAliasInner, Tag("type_alias")]
    | Annotated[ConstantInner, Tag("constant")]
    | Annotated[StaticInner, Tag("static")]
    | Annotated[OtherInner, Tag("other")],
    Discriminator(_inner_tag),
]


class Item(_Rustdoc):
    id: Id
    crate_id: int
    name: str | None
    visibility: Visibility
    docs: str | None
    #: Bare strings (``macro_export``) or one-key objects; ``{"other": "#[...]"}`` carries the
    #: attribute's source text.
    attrs: tuple[str | dict[str, JsonValue], ...]
    deprecation: Deprecation | None
    inner: Inner

    @property
    def is_public(self) -> bool:
        return self.visibility == "public"


class ItemSummary(_Rustdoc):
    """An entry of ``paths``: where an item is defined."""

    crate_id: int
    path: tuple[str, ...]
    kind: str


class ExternalCrate(_Rustdoc):
    name: str


class Crate(_Rustdoc):
    format_version: int
    root: Id
    index: dict[Id, Item]
    paths: dict[Id, ItemSummary]
    external_crates: dict[int, ExternalCrate]


def parse_crate(text: str) -> Crate:
    """One crate's rustdoc JSON, or :class:`UnreadableRustdoc` naming what does not match.

    The version is checked before anything else, so a format bump is reported as one rather than as
    whichever field happened to move first.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnreadableRustdoc(f"not JSON: {exc}") from exc
    found = raw.get("format_version") if isinstance(raw, dict) else None
    if found != FORMAT_VERSION:
        raise UnreadableRustdoc(
            f"rustdoc JSON format_version {found}, but this parser speaks {FORMAT_VERSION}. The "
            f"format is nightly-only and unstable; update composer/cargo/rustdoc.py against the "
            f"new schema rather than reading it partly."
        )
    try:
        return Crate.model_validate(raw)
    except ValidationError as exc:
        raise UnreadableRustdoc(
            f"rustdoc JSON does not match the format {FORMAT_VERSION} schema this parser reads:\n"
            f"{exc}"
        ) from exc
