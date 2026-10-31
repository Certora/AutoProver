"""Arbitrary values of a Soroban contract's own types, written into the source that defines them.

A rule over a contract function takes the function's arguments as its own parameters, and a
``#[contracttype]`` argument needs a ``Nondet`` implementation to be arbitrary. Writing one by hand
is mechanical and easy to get subtly wrong, so the scaffold writes them: for every
``#[contracttype]`` and ``#[contracterror]`` in the verified package, an ``impl Nondet`` built from
the SDK helpers in ``src/certora/soroban_nondet.rs``.

The implementations go at the end of the file that defines the types, in a child module gated on
the ``certora`` feature. A child module can construct a type whose fields are private, which is why
it has to be that file, and ``use super::*`` gives it every name the file can name, so no import has
to be resolved here. A normal build does not compile it.

An implementation is written only when every field can be built: from a primitive, an SDK type the
helpers cover, an ``Option``/``Vec``/``Map``/tuple of those, or another contract type that gets one.
Anything else — a generic type, a type from another crate, a fixed-size array, a recursive type — is
skipped, and so is every type that needs a skipped one. The preflight gate compiles what is written
here, so a guess that does not compile would stop the run; a skipped type costs only the author's
effort.

Adapted from the type parser in Julian's ``scripts/soroban`` (``julian/soroban-setup``).
"""

import dataclasses
import re
from collections.abc import Iterable
from pathlib import Path

#: The ``mod`` the implementations are written into. Its presence in a file is how the scaffold
#: knows that file is done.
NONDET_MODULE = "certora_nondet"


@dataclasses.dataclass(frozen=True)
class RustType:
    """A field's type: ``name`` with its generic ``params``. A tuple is named ``()``."""

    name: str
    params: tuple["RustType", ...] = ()


@dataclasses.dataclass(frozen=True)
class Field:
    #: ``None`` for a positional field.
    name: str | None
    ty: RustType


@dataclasses.dataclass(frozen=True)
class Variant:
    name: str
    fields: tuple[Field, ...] = ()
    #: ``{ a: T }`` rather than ``(T)``.
    named: bool = False


@dataclasses.dataclass(frozen=True)
class ContractType:
    name: str
    #: A struct's fields, empty for a unit struct.
    fields: tuple[Field, ...] = ()
    #: An enum's variants; ``None`` for a struct.
    variants: tuple[Variant, ...] | None = None
    #: ``{ a: T }`` rather than ``(T)``, for a struct.
    named: bool = True
    #: Declared with type parameters. Never implemented: which ones a rule wants is not known here.
    generic: bool = False

    def field_types(self) -> Iterable[RustType]:
        yield from (f.ty for f in self.fields)
        for v in self.variants or ():
            yield from (f.ty for f in v.fields)


# ---------------------------------------------------------------------------------------------
# Parsing


_CONTRACT_ATTR = re.compile(r"#\s*\[\s*contract(?:type|error)\s*(?:\([^)]*\))?\s*\]")
_TEST_MOD = re.compile(r"#\[[^\]]*\btest\b[^\]]*\]\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+\w+\s*\{")
_CFG_IF = re.compile(r"\bcfg_if\s*(?:::\s*cfg_if\s*)?!\s*\{")
_HEAD = re.compile(
    r"(?:pub\s*(?:\([^)]*\)\s*)?)?(struct|enum)\s+([A-Za-z_]\w*)\s*(<[^{(;]*>)?\s*(?:where[^{(;]*)?"
)
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", src)


def _matching(s: str, start: int, open_: str, close: str) -> int:
    depth = 0
    for i in range(start, len(s)):
        if s[i] == open_:
            depth += 1
        elif s[i] == close:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _strip_blocks(src: str, pattern: re.Pattern[str]) -> str:
    """``src`` without each ``{ … }`` block ``pattern`` opens (it must end at the ``{``)."""
    out: list[str] = []
    pos = 0
    while (m := pattern.search(src, pos)) is not None:
        out.append(src[pos : m.start()])
        close = _matching(src, m.end() - 1, "{", "}")
        if close == -1:
            return "".join(out)
        pos = close + 1
    out.append(src[pos:])
    return "".join(out)


def _split_top(s: str) -> list[str]:
    """``s`` split on commas outside any bracket."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in s:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def parse_type(s: str) -> RustType:
    """``Map<Address, Vec<u64>>`` as a :class:`RustType`. A path keeps only its last segment, which
    is what ``use super::*`` names it by."""
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("(") and s.endswith(")"):
        return RustType("()", tuple(parse_type(p) for p in _split_top(s[1:-1])))
    if s.startswith("["):
        return RustType(s)
    angle = s.find("<")
    if angle == -1:
        return RustType(s.split("::")[-1].strip())
    base = s[:angle].split("::")[-1].strip()
    close = s.rfind(">")
    return RustType(base, tuple(parse_type(p) for p in _split_top(s[angle + 1 : close])))


_ATTRIBUTE = re.compile(r"#\s*\[")


def _without_attributes(s: str) -> str:
    out: list[str] = []
    pos = 0
    while (m := _ATTRIBUTE.search(s, pos)) is not None:
        out.append(s[pos : m.start()])
        end = _matching(s, m.end() - 1, "[", "]")
        pos = len(s) if end == -1 else end + 1
    out.append(s[pos:])
    return "".join(out)


def _strip_visibility(s: str) -> str:
    return re.sub(r"^\s*pub\s*(?:\([^)]*\))?\s*", "", s)


def _named_fields(body: str) -> tuple[Field, ...]:
    fields: list[Field] = []
    for part in _split_top(_without_attributes(body)):
        name, colon, ty = _strip_visibility(part).partition(":")
        if colon and _IDENT.fullmatch(name.strip()):
            fields.append(Field(name.strip(), parse_type(ty)))
    return tuple(fields)


def _positional_fields(body: str) -> tuple[Field, ...]:
    return tuple(
        Field(None, parse_type(_strip_visibility(p))) for p in _split_top(_without_attributes(body))
    )


def _variants(body: str) -> tuple[Variant, ...]:
    variants: list[Variant] = []
    body = _without_attributes(body)
    i = 0
    while (m := _IDENT.search(body, i)) is not None:
        name = m.group(0)
        i = m.end()
        while i < len(body) and body[i].isspace():
            i += 1
        if i < len(body) and body[i] == "{":
            close = _matching(body, i, "{", "}")
            variants.append(Variant(name, _named_fields(body[i + 1 : close]), named=True))
            i = close + 1
        elif i < len(body) and body[i] == "(":
            close = _matching(body, i, "(", ")")
            variants.append(Variant(name, _positional_fields(body[i + 1 : close])))
            i = close + 1
        else:
            variants.append(Variant(name))
        # Past the discriminant, if any, and the separating comma.
        comma = body.find(",", i)
        i = len(body) if comma == -1 else comma + 1
    return tuple(variants)


def parse_contract_types(src: str) -> list[ContractType]:
    """Every ``#[contracttype]`` / ``#[contracterror]`` defined in ``src``, outside test modules and
    ``cfg_if!`` blocks."""
    cleaned = _strip_blocks(_strip_blocks(_strip_comments(src), _TEST_MOD), _CFG_IF)
    found: list[ContractType] = []
    for attr in _CONTRACT_ATTR.finditer(cleaned):
        pos = attr.end()
        # Past the other attributes between this one and the item.
        while True:
            pos += len(cleaned[pos:]) - len(cleaned[pos:].lstrip())
            if not cleaned.startswith("#", pos):
                break
            end = _matching(cleaned, cleaned.index("[", pos), "[", "]")
            if end == -1:
                break
            pos = end + 1
        head = _HEAD.match(cleaned, pos)
        if head is None:
            continue
        kind, name, generics = head.group(1), head.group(2), head.group(3)
        body = head.end()
        opener = cleaned[body : body + 1]
        if kind == "enum" and opener == "{":
            close = _matching(cleaned, body, "{", "}")
            found.append(
                ContractType(
                    name, variants=_variants(cleaned[body + 1 : close]), generic=bool(generics)
                )
            )
        elif kind == "struct" and opener == "{":
            close = _matching(cleaned, body, "{", "}")
            found.append(
                ContractType(
                    name, fields=_named_fields(cleaned[body + 1 : close]), generic=bool(generics)
                )
            )
        elif kind == "struct" and opener == "(":
            close = _matching(cleaned, body, "(", ")")
            found.append(
                ContractType(
                    name,
                    fields=_positional_fields(cleaned[body + 1 : close]),
                    named=False,
                    generic=bool(generics),
                )
            )
        elif kind == "struct":
            found.append(ContractType(name, generic=bool(generics)))
    return found


# ---------------------------------------------------------------------------------------------
# Rendering


#: SDK types and the helper that builds one (``soroban_nondet.rs``).
_SDK_HELPERS = {
    "Address": "nondet_address()",
    "Symbol": "nondet_symbol()",
    "String": "nondet_string()",
    "Bytes": "nondet_bytes()",
    "U256": "nondet_u256()",
    "I256": "nondet_i256()",
    "Duration": "nondet_duration()",
    "Timepoint": "nondet_timepoint()",
    "Val": "nondet_val()",
}

#: Sizes ``soroban_nondet.rs`` has a ``nondet_bytesn_<N>`` for.
_BYTESN_SIZES = frozenset({"20", "32", "64", "65"})

#: Primitives ``cvlr::nondet::Nondet`` is implemented for.
_PRIMITIVES = frozenset(
    {"bool", "u8", "u16", "u32", "u64", "u128", "i8", "i16", "i32", "i64", "i128", "usize"}
)


def _expr(ty: RustType, implemented: frozenset[str], muxed: bool) -> str | None:
    """A Rust expression for an arbitrary ``ty``, or ``None`` when nothing here can build one."""
    # Parameterised by a size rather than a type, so matched before the parameters are built.
    if ty.name == "BytesN" and len(ty.params) == 1 and ty.params[0].name in _BYTESN_SIZES:
        return f"nondet_bytesn_{ty.params[0].name}()"
    if ty.name == "Hash" and ty.params == (RustType("32"),):
        return "nondet_hash()"
    sub = [_expr(p, implemented, muxed) for p in ty.params]
    if any(e is None for e in sub):
        return None
    match ty.name, sub:
        case name, [] if name in _PRIMITIVES:
            return f"cvlr::nondet::nondet::<{name}>()"
        case name, [] if name in _SDK_HELPERS:
            return _SDK_HELPERS[name]
        case "MuxedAddress", [] if muxed:
            return "nondet_muxedaddress()"
        case "Option", [inner]:
            return f"if nondet_bool() {{ Some({inner}) }} else {{ None }}"
        case "Vec", [inner]:
            return f"nondet_vec_internal(|| {inner})"
        case "Map", [key, value]:
            return f"nondet_map_internal(|| {key}, || {value})"
        case "()", parts if parts:
            return f"({', '.join(e for e in parts if e is not None)},)"
        case name, [] if name in implemented:
            return f"{name}::nondet()"
        case _:
            return None


def _references(ty: RustType) -> Iterable[str]:
    yield ty.name
    for p in ty.params:
        yield from _references(p)


def implementable(types: Iterable[ContractType], *, muxed: bool) -> list[ContractType]:
    """The types every field of which can be built, in their given order.

    A fixed point over two refusals, until neither drops anything: a type with a field no remaining
    type or helper builds, and a type that can reach itself through its fields, whose ``nondet()``
    would not terminate.
    """
    types = list(types)
    candidates = {t.name: t for t in types if not t.generic}
    while True:
        names = frozenset(candidates)
        buildable = {
            name: t
            for name, t in candidates.items()
            if all(_expr(ty, names, muxed) is not None for ty in t.field_types())
        }
        edges = {
            name: {r for ty in t.field_types() for r in _references(ty) if r in buildable}
            for name, t in buildable.items()
        }
        keep = {name: t for name, t in buildable.items() if not _reaches(edges, name)}
        if len(keep) == len(candidates):
            return [t for t in types if t.name in keep]
        candidates = keep


def _reaches(edges: dict[str, set[str]], start: str) -> bool:
    """Whether ``start`` reaches itself along ``edges``."""
    seen: set[str] = set()
    frontier = list(edges[start])
    while frontier:
        node = frontier.pop()
        if node == start:
            return True
        if node not in seen:
            seen.add(node)
            frontier.extend(edges.get(node, ()))
    return False


def _construction(t: ContractType, implemented: frozenset[str], muxed: bool) -> str:
    def build(path: str, fields: tuple[Field, ...], named: bool) -> str:
        values = [_expr(f.ty, implemented, muxed) for f in fields]
        if not fields:
            return path
        if named:
            inner = ", ".join(f"{f.name}: {v}" for f, v in zip(fields, values))
            return f"{path} {{ {inner} }}"
        return f"{path}({', '.join(v for v in values if v is not None)})"

    if t.variants is None:
        return build(t.name, t.fields, t.named)
    if not t.variants:
        return "unreachable!()"
    arms = [build(f"{t.name}::{v.name}", v.fields, v.named) for v in t.variants]
    expr = arms[-1]
    for arm in reversed(arms[:-1]):
        expr = f"if nondet_bool() {{ {arm} }} else {{ {expr} }}"
    return expr


def render_nondet_module(types: list[ContractType], implemented: frozenset[str], *, muxed: bool,
                         feature: str) -> str:
    """The child module holding ``types``' implementations, ready to append to their file.

    ``implemented`` is every type that gets one anywhere in the package: a field may name a type
    another file defines, and that file's module implements it.
    """
    impls = "\n".join(
        f"    impl Nondet for {t.name} {{\n"
        f"        fn nondet() -> Self {{\n"
        f"            {_construction(t, implemented, muxed)}\n"
        f"        }}\n"
        f"    }}\n"
        for t in types
    )
    return (
        "\n"
        "// Added by AutoProver: arbitrary values of this file's contract types, for verification.\n"
        f'#[cfg(feature = "{feature}")]\n'
        f"mod {NONDET_MODULE} {{\n"
        "    #![allow(unused_imports)]\n"
        "    use super::*;\n"
        "    use crate::certora::soroban_nondet::*;\n"
        "    use cvlr::nondet::Nondet;\n"
        "\n"
        f"{impls}"
        "}\n"
    )


def has_nondet_module(src: str) -> bool:
    return re.search(rf"\bmod\s+{NONDET_MODULE}\b", src) is not None


def is_test_source(path: Path, src_root: Path) -> bool:
    """Whether ``path`` is compiled only by ``cargo test``, as far as its place says: under a
    ``test``/``tests`` directory, or a ``test.rs``, ``tests.rs`` or ``test_*.rs``."""
    parts = path.relative_to(src_root).parts
    return (
        any(p in ("test", "tests") for p in parts[:-1])
        or path.stem in ("test", "tests")
        or path.stem.startswith("test_")
    )


def package_sources(src_root: Path, skip: Path) -> list[Path]:
    """The package's non-test ``.rs`` files, in a stable order, without ``skip`` (the harness)."""
    return sorted(
        p
        for p in src_root.rglob("*.rs")
        if not p.is_relative_to(skip) and not is_test_source(p, src_root)
    )
