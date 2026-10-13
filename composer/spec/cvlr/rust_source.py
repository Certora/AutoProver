"""Reading the program's Rust source far enough to apply a munge to it.

Every bit of Rust reading the munge backend does is here, and it exists because of how munges are
stored and applied, not because of anything about Rust.

**Why a munge has to read the source at all.** All of a run's units build in one shared copy of the
project (:mod:`composer.spec.cvlr.tree`), so the dependency graph is compiled once per run instead
of once per unit. Sharing is sound only if one unit's edit to the program cannot change what a
sibling's rules see, which means every edit has to be gated on the recording unit's cargo feature
and be inert with it off. A free-form text edit has no feature gate, so a munge is not text but a
**record** — "this attribute on ``deposit`` in ``src/lib.rs``, gated on ``unit_x``" — and the tree is
derived by replaying the union of every unit's records onto the project's own copy of the file. It
is replayed at every gate, on a fresh run, on a resume and after a rewind, onto source that may have
moved since the record was made, and that already holds every other unit's munges of the same file.
So every application of a munge starts from a record and has to find, in text nobody has looked at
in this run, the place the record is about.

What each kind has to find:

* **An attribute** (:class:`~composer.spec.cvlr.munge.FunctionMunge`) is inserted on the line above
  a signature, so it needs only where the signature of the named function starts and how it is
  indented — :func:`find_signature`.
* **An extraction** (:class:`~composer.spec.cvlr.munge.FunctionExtraction`) replaces the whole
  definition with a feature-gated pair, and needs where the definition *ends* as well —
  :func:`function_item`. It quotes the original verbatim in the ``#[cfg(not(..))]`` half, so the
  deployed build compiles exactly what shipped; and it stores that quote, so a replay onto source
  that has moved finds no match and refuses rather than rewriting a region nobody read.
* **A derive swap** (:class:`~composer.spec.cvlr.munge.DeriveSwap`) is located by captured text like
  an extraction, but has to split the ``#[derive(..)]`` it captured into the derives that move
  behind the gate and the ones that stay — :data:`DERIVE_LIST` and :func:`derive_lists`. And since a
  landed swap can no longer be found by its captured text, it recognizes itself by the gate in the
  attributes above the type — :func:`attributes_above`.
* **A module redirect** (:class:`~composer.spec.cvlr.munge.ModuleRedirect`) is inserted on the line
  above a ``mod name;`` declaration, so it needs only where that declaration is —
  :func:`module_declarations`.
* **An import swap** (:class:`~composer.spec.cvlr.munge.ImportSwap`) is located by the ``use``
  declaration that binds a name, so it has to know what each declaration binds, including inside a
  brace group it must take one name out of, and whether the declaration is already under some unit's
  ``#[cfg(feature)]`` — :func:`use_declarations`.

The end of a definition is its matching close brace, and that is where a regex stops being enough: a
brace inside a string, a raw string, a character literal such as ``'\\u{1F600}'`` or a nested
comment opens and closes nothing. :func:`code_positions` is what tells the two apart.

**Why it is not a Rust parser.** Everything located here is handed straight to the compiler, which is
what decides whether it was right; a misjudged span is a compile failure at the gate, not a silent
mis-edit. What a compile *cannot* catch is the right edit in the wrong place, so the readers here
are strict about saying when they cannot tell — two functions of one name, a ``use`` that binds a
name without spelling it, a group nested too deeply to take one leaf out of — and lenient about
everything else.

The EVM munge editor needs none of this: it writes edited text straight into a per-author overlay,
and applying an edit swaps that text in, so nothing is ever looked up.
"""

import difflib
import re
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionNotFound:
    """No function of that name in the file. ``nearby`` is what the file does define."""

    function: str
    nearby: tuple[str, ...]


@dataclass(frozen=True)
class FunctionAmbiguous:
    """More than one place the munge could mean, and so none of them.

    Several functions of one name — a trait impl and an inherent one, or two impl blocks — or, for
    the munges that are not about a function, two copies of the text a munge captured, or two ``use``
    declarations binding one name. Refused rather than guessed at: munging the wrong one of two
    produces a build that compiles, a rule that still fails, and no indication which was changed.
    """

    #: What the munge is about: a function, a type, an imported name.
    subject: str
    lines: tuple[int, ...]


@dataclass(frozen=True)
class NoFunctionBody:
    """The signature has no body — a trait method declaration, or an ``extern`` block entry.

    An attribute on one of those is legal and occasionally wanted; an extraction of one is not a
    thing, because there is nothing to extract.
    """

    function: str


# ---------------------------------------------------------------------------------------------
# signatures


def _signature_pattern(function: str) -> re.Pattern[str]:
    """Where a named function's signature starts.

    Deliberately anchored at the line and permissive about what precedes ``fn``: visibility, and the
    ``const`` / ``async`` / ``unsafe`` / ``extern "C"`` qualifiers, in the order Rust requires them.
    The trailing ``[(<]`` is what keeps ``fn deposit`` from matching ``fn deposit_fee``.
    """
    return re.compile(
        r"^(?P<indent>[ \t]*)"
        r"(?:pub(?:\([^)]*\))?[ \t]+)?"
        r"(?:default[ \t]+)?"
        r"(?:const[ \t]+)?"
        r"(?:async[ \t]+)?"
        r"(?:unsafe[ \t]+)?"
        r"(?:extern[ \t]+\"[^\"]*\"[ \t]+)?"
        rf"fn[ \t]+{re.escape(function)}[ \t]*[(<]",
        re.MULTILINE,
    )


_ANY_FN = re.compile(r"\bfn[ \t]+([A-Za-z_][A-Za-z0-9_]*)")


def function_names(source: str) -> tuple[str, ...]:
    """Every function name the file mentions a definition of, in order, deduplicated."""
    return tuple(dict.fromkeys(_ANY_FN.findall(source)))


def _line_of(source: str, offset: int) -> int:
    """The 1-indexed line ``offset`` falls on."""
    return source.count("\n", 0, offset) + 1


@dataclass(frozen=True)
class Signature:
    """Where the one function of some name begins."""

    #: Offset of the start of the signature's line, qualifiers and indentation included.
    start: int
    indent: str
    #: 1-indexed.
    line: int


def find_signature(source: str, function: str) -> Signature | FunctionNotFound | FunctionAmbiguous:
    """The signature of the one function named ``function`` in ``source``, or why there is not one.

    A name that matches nothing is reported with the names the file does define, since it is
    usually a typo or a function that has been renamed since the munge was recorded.
    """
    matches = list(_signature_pattern(function).finditer(source))
    match matches:
        case []:
            return FunctionNotFound(
                function=function,
                nearby=tuple(
                    difflib.get_close_matches(function, function_names(source), n=3, cutoff=0.5)
                ),
            )
        case [only]:
            return Signature(
                start=only.start(), indent=only.group("indent"), line=_line_of(source, only.start())
            )
        case _:
            return FunctionAmbiguous(function, tuple(_line_of(source, m.start()) for m in matches))


# ---------------------------------------------------------------------------------------------
# whole definitions


@dataclass(frozen=True)
class FunctionItem:
    """A whole function definition as it stands in some source.

    ``signature`` is whitespace-collapsed so two spellings of one contract compare equal. That is
    the check that makes an extraction a *refactoring*: the enclosing function keeps its name, its
    parameters and its return type, so every caller in the program — including the ones no rule
    mentions — is unaffected by which half of the gated pair it compiles.
    """

    text: str
    signature: str
    line: int


def function_item(
    source: str, function: str
) -> FunctionItem | FunctionNotFound | FunctionAmbiguous | NoFunctionBody:
    """The one definition of ``function`` in ``source``, or why it cannot be pinned down."""
    match find_signature(source, function):
        case Signature() as signature:
            pass
        case refused:
            return refused
    body = body_span(source, signature.start)
    if body is None:
        return NoFunctionBody(function)
    opened, end = body
    return FunctionItem(
        text=source[signature.start : end],
        signature=" ".join(source[signature.start : opened].split()),
        line=signature.line,
    )


#: A raw string opener, ``r"``/``r#"``/``br##"`` — the closing delimiter carries the same hash count.
_RAW_STRING = re.compile(r'b?r(?P<hashes>#*)"')

#: A character or byte-character literal. The escapes are spelled out rather than left to ``\\.``
#: because ``'\u{1F600}'`` contains a brace, and a scanner that fell through to counting it would
#: mismatch every brace after it.
_CHAR_LITERAL = re.compile(r"b?'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]{1,6}\}|.)|[^\\'\n])'")


def code_positions(source: str, start: int) -> Iterator[int]:
    """Indices of the characters of ``source`` from ``start`` that are code.

    Skips line and (nesting) block comments, strings, raw strings, and character literals — the four
    places a brace can appear without opening or closing a block. A lifetime is *not* skipped and
    does not need to be: ``'a`` fails :data:`_CHAR_LITERAL` for want of a closing quote, and the
    characters it yields are not braces.
    """
    i, n = start, len(source)
    while i < n:
        c = source[i]
        if source.startswith("//", i):
            newline = source.find("\n", i)
            if newline < 0:
                return
            i = newline
            continue
        if source.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if source.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif source.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue
        if (raw := _RAW_STRING.match(source, i)) is not None:
            closing = '"' + raw["hashes"]
            end = source.find(closing, raw.end())
            i = n if end < 0 else end + len(closing)
            continue
        if c == '"' or source.startswith('b"', i):
            i += 2 if c == "b" else 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            continue
        if (char := _CHAR_LITERAL.match(source, i)) is not None:
            i = char.end()
            continue
        yield i
        i += 1


def body_span(source: str, signature_start: int) -> tuple[int, int] | None:
    """The body of the signature at ``signature_start``: its opening brace, and one past its close.

    The body's opening brace is the first one at bracket depth zero, which is what keeps a ``{`` in
    a parameter's type or a ``pub(crate)`` from being mistaken for it. A ``;`` at depth zero first
    means there is no body at all.
    """
    depth, opened = 0, None
    for i in code_positions(source, signature_start):
        match source[i]:
            case "(" | "[":
                depth += 1
            case ")" | "]":
                depth -= 1
            case "{" if depth == 0:
                opened = i
                break
            case ";" if depth == 0:
                return None
            case _:
                pass
    if opened is None:
        return None
    depth = 0
    for i in code_positions(source, opened):
        match source[i]:
            case "{":
                depth += 1
            case "}":
                depth -= 1
                if depth == 0:
                    return opened, i + 1
            case _:
                pass
    return None


# ---------------------------------------------------------------------------------------------
# attributes and derives


def attributes_above(source: str, offset: int) -> tuple[str, ...]:
    """The attribute lines stacked directly above the line ``offset`` starts, nearest first.

    Blank lines between them are skipped; anything else that is not an attribute — a doc comment, an
    item — ends the stack.
    """
    found: list[str] = []
    for line in reversed(source[:offset].splitlines()):
        if not line.strip():
            continue
        if not line.lstrip().startswith("#["):
            break
        found.append(line.strip())
    return tuple(found)


#: A ``#[derive(..)]`` attribute and the list inside it. Read to ask what a type derives, and
#: rewritten to change it, so the whole attribute is the match and ``items`` is the payload.
DERIVE_LIST = re.compile(r"#\[derive\(\s*(?P<items>[^)]*?)\s*\)\]")


def derive_lists(text: str) -> tuple[tuple[str, ...], ...]:
    """Every ``#[derive(..)]`` in ``text``, each as the derives it names.

    Nested generics do not occur in a derive list, so splitting on commas is exact rather than
    approximate.
    """
    return tuple(
        tuple(d.strip() for d in items.split(",") if d.strip())
        for items in DERIVE_LIST.findall(text)
    )


# ---------------------------------------------------------------------------------------------
# module declarations


#: A ``mod`` **declaration** — the form ``#[path]`` can redirect. ``mod name { .. }`` is excluded by
#: requiring the semicolon: an inline module is not loaded from a file, so the attribute would be
#: accepted by rustc and change nothing.
_MODULE_DECLARATION = re.compile(
    r"^(?P<indent>[ \t]*)(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ModuleDeclaration:
    """One ``mod name;`` declaration."""

    name: str
    #: Offset of the start of the declaration's line.
    start: int
    indent: str
    #: 1-indexed.
    line: int


def module_declarations(source: str) -> Iterator[ModuleDeclaration]:
    """Every ``mod name;`` declaration in ``source``, in order."""
    for at in _MODULE_DECLARATION.finditer(source):
        yield ModuleDeclaration(
            name=at["name"], start=at.start(), indent=at["indent"], line=_line_of(source, at.start())
        )


# ---------------------------------------------------------------------------------------------
# use declarations


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A ``use`` declaration, whole. ``[^;]*`` crosses newlines, which a braced group needs.
_USE_DECLARATION = re.compile(
    r"^(?P<indent>[ \t]*)(?P<vis>pub(?:\s*\([^)]*\))?\s+)?use\s+(?P<tree>[^;]*);",
    re.MULTILINE,
)

#: A whole-attribute ``cfg`` on one cargo feature, as a munge writes them and as a developer gating
#: an import for verification writes them too. Anything richer — an ``all``, a target predicate — is
#: not matched, and a declaration under one is read as ungated.
_CFG_FEATURE = re.compile(
    r'^[ \t]*#\[cfg\((?P<negated>not\()?feature\s*=\s*"(?P<feature>[^"]+)"\)?\)\][ \t]*$'
)


@dataclass(frozen=True)
class UseTree:
    """A ``use`` declaration's payload, flattened far enough to take one leaf out of it."""

    #: Everything before the brace group, ``::``-terminated. Empty for a declaration that imports
    #: one path with no group.
    prefix: str
    #: ``(bound name, the item as written)``, in source order, whitespace-collapsed.
    items: tuple[tuple[str, str], ...]

    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.items)


@dataclass(frozen=True)
class FeatureGate:
    """``#[cfg(feature = "..")]``, or with ``negated`` ``#[cfg(not(feature = ".."))]``."""

    feature: str
    negated: bool


@dataclass(frozen=True)
class UseDeclaration:
    """One ``use`` declaration, and what it binds."""

    #: Offsets of the declaration in the source it was read from, indentation through semicolon.
    start: int
    end: int
    #: 1-indexed.
    line: int
    indent: str
    #: ``pub``, ``pub(crate)`` and so on.
    visibility: str | None
    #: The declaration verbatim.
    text: str
    #: What it binds, or ``None`` when this reader cannot say: a glob binds names it never spells,
    #: and a nested group cannot have one leaf taken out of it by a split on commas.
    tree: UseTree | None
    #: The ``cfg`` on the line directly above, when it is a single-feature one.
    gate: FeatureGate | None

    def mentions(self, name: str) -> bool:
        """Whether ``name`` appears in the declaration as an identifier, whatever it binds."""
        return name in _IDENT.findall(self.text)


def _bound_name(item: str, prefix: str) -> str | None:
    """The name one item of a ``use`` puts in scope, or ``None`` if this reader cannot say."""
    head, aliased, alias = item.partition(" as ")
    if aliased:
        name = alias.strip()
    elif head.strip() == "self":
        name = prefix.strip().rstrip(":").rsplit("::", 1)[-1]
    else:
        name = head.strip().rsplit("::", 1)[-1]
    return name if _IDENT.fullmatch(name) else None


def _parse_use(tree: str) -> UseTree | None:
    """The names a ``use`` binds, or ``None`` when this reader cannot say."""
    flat = " ".join(tree.split())
    if "*" in flat:
        return None
    if "{" not in flat:
        name = _bound_name(flat, "")
        return None if name is None else UseTree(prefix="", items=((name, flat),))
    prefix, _, rest = flat.partition("{")
    if "{" in rest or not rest.endswith("}"):
        return None
    items: list[tuple[str, str]] = []
    for raw in rest[:-1].split(","):
        if not (item := raw.strip()):
            continue
        if (name := _bound_name(item, prefix)) is None:
            return None
        items.append((name, item))
    return UseTree(prefix=prefix, items=tuple(items)) if items else None


def _gate_above(source: str, offset: int) -> FeatureGate | None:
    preceding = source[:offset].rstrip("\n").rsplit("\n", 1)[-1]
    if (gate := _CFG_FEATURE.match(preceding)) is None:
        return None
    return FeatureGate(feature=gate["feature"], negated=gate["negated"] is not None)


def use_declarations(source: str) -> Iterator[UseDeclaration]:
    """Every ``use`` declaration in ``source``, in order."""
    for at in _USE_DECLARATION.finditer(source):
        vis = (at["vis"] or "").strip()
        yield UseDeclaration(
            start=at.start(),
            end=at.end(),
            line=_line_of(source, at.start()),
            indent=at["indent"],
            visibility=vis or None,
            text=at.group(0),
            tree=_parse_use(at["tree"]),
            gate=_gate_above(source, at.start()),
        )
