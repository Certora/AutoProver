"""What a CVLR crate exports, extracted without asking anyone.

The generated crate reference (``docs/cvlr-backend-plan.md`` §5.4 item 2) is written by an agent, and
the standing lesson from the capture pass is that an agent's output needs a gate that tests a
*property* rather than a plausibility. For a reference, the property is **completeness**: every
public item the crates export must be named by some entry, or the reference has a hole exactly where
an agent will later guess.

That gate needs a list nobody wrote by hand, which is what this module produces. It is also the
list the generation is *scoped* by, so the same extraction decides what is asked for and what is
checked — a hole cannot open between the two.

**Regex, not a parser.** There is no Rust parser here and pulling one in for this would cost more
than it settles. The failure modes are therefore worth naming: an item this misses is one the
completeness gate cannot demand, and an item it invents is a gate failure nobody can satisfy. Both
are bounded by ``tests/test_cvlr_inventory.py``, which runs the extraction over hand-written Rust
covering every shape observed in the reference set.

Two exclusions are deliberate rather than incidental:

* **Test code.** ``#[cfg(test)]`` modules and the ``tests/`` tree are not API. The ``tests/expand``
  trees are the exception the reference *wants* — they are ``macrotest`` snapshot pairs, a macro
  beside its expansion — but they are input to the generator, not items to document, so they are
  found by :func:`expansion_pairs` rather than by the item scan.
* **Private ``macro_rules!``.** A macro without ``#[macro_export]`` is not reachable from a target's
  code, and documenting one invites exactly the call that will not compile.
"""

import dataclasses
import re
from collections.abc import Iterator, Sequence
from typing import Literal

from composer.spec.cvlr.source_tools import MountedCrates

#: What kind of thing an item is. ``macro`` covers both ``macro_rules!`` and the proc-macro crates'
#: exports: from a caller's side they are the same thing — a name invoked with ``!`` or as an
#: attribute — and the distinction lives in how they are *implemented*, which a reference reader
#: does not care about.
type ItemKind = Literal[
    "macro", "attribute", "derive", "fn", "struct", "enum", "trait", "type", "alias"
]

_ITEM_PATTERNS: tuple[tuple[ItemKind, re.Pattern[str]], ...] = (
    ("macro", re.compile(r"^\s*macro_rules!\s+(?P<name>\w+)")),
    (
        "fn",
        re.compile(
            r"^\s*pub(?:\s*\([^)]*\))?\s+"
            r"(?:default\s+|const\s+|async\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*"
            r"fn\s+(?P<name>\w+)"
        ),
    ),
    ("struct", re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+struct\s+(?P<name>\w+)")),
    ("enum", re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+enum\s+(?P<name>\w+)")),
    ("trait", re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+(?:unsafe\s+)?trait\s+(?P<name>\w+)")),
    ("type", re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+type\s+(?P<name>\w+)")),
)

#: Proc-macro crates export through attributes on a private function, so the function's own name is
#: the export. ``proc_macro_derive`` names the derive separately and the function name is noise.
_PROC_MACRO = re.compile(r"^\s*#\[proc_macro(?P<sort>_derive|_attribute)?\s*(\((?P<arg>[^)]*)\))?\]")

#: A re-export, which on these crates is not a formality: the names an author actually writes are
#: often aliases whose definitions carry a different name — ``pub use super::log::cvlr_log as clog``
#: and ``pub use macros::cvlr_predicate as predicate`` are the two most-used entry points in the
#: whole library. A reference that documented only the definition's name would leave an agent unable
#: to look up the spelling every project uses. Globs are skipped: they export no name to document.
_REEXPORT = re.compile(
    r"^\s*pub\s+use\s+(?P<target>[\w:{}, ]+?)(?:\s+as\s+(?P<alias>\w+))?\s*;"
)

#: A ``macro_rules!`` whose body *defines* an exported macro from one of its parameters — a macro
#: that generates macros. ``cvlr-asserts`` uses four of them to emit twenty-four exported names,
#: ``cvlr_assert_eq!`` among them, and every one of those names appears in the source only as an
#: argument. A line scan sees none of them, so a completeness gate built on one would never demand
#: the comparison assertions at all — the family an authoring agent reaches for most.
_MACRO_GENERATOR = re.compile(r"macro_rules!\s+(?P<gen>\w+)\s*\{")
_GENERATES_EXPORT = re.compile(r"#\[macro_export\]\s*macro_rules!\s*\$")

_MACRO_EXPORT = re.compile(r"^\s*#\[macro_export")
_CFG_TEST = re.compile(r"^\s*#\[cfg\(test\)\]")
_IMPL = re.compile(r"^\s*impl(?:\s*<[^>]*>)?\s+(?:(?P<trait>[\w:]+)(?:\s*<[^>]*>)?\s+for\s+)?(?P<ty>[\w:]+)")


@dataclasses.dataclass(frozen=True)
class Item:
    """One public export, and where it is defined."""

    crate: str
    path: str
    line: int
    kind: ItemKind
    name: str
    #: The type whose ``impl`` block this sits in, when it has one. A method's bare name is often
    #: ambiguous across a crate (``nondet`` is defined nine times in ``cvlr-nondet``), and this is
    #: what tells a reader — and a reviewer of the generated entry — which one is meant.
    owner: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.owner}::{self.name}" if self.owner else self.name


def _strip_comments(line: str) -> str:
    """Enough to keep a doc comment's prose from matching an item pattern.

    Not a lexer: a ``//`` inside a string literal would be cut here too. That is acceptable because
    the result is only ever fed back to the same regexes, and no item pattern can match the tail of
    a string literal."""
    return line.split("//", 1)[0]


def _macro_generators(source: str) -> frozenset[str]:
    """The names of ``macro_rules!`` in ``source`` that define an exported macro from a parameter."""
    found: set[str] = set()
    for match in _MACRO_GENERATOR.finditer(source):
        # The body is unbraced-scanned rather than balanced: a generator's export appears in its
        # first few lines, and a balanced scan would buy nothing but a brace counter.
        if _GENERATES_EXPORT.search(source[match.end() : match.end() + 2000]):
            found.add(match.group("gen"))
    return frozenset(found)


def items_in(crate: str, path: str, source: str) -> Iterator[Item]:
    """Every public item defined in one file.

    ``#[cfg(test)]`` modules are skipped by brace depth, which is why this reads the file as a
    stream rather than as a set of independent line matches: a ``pub fn`` inside a test module looks
    exactly like one outside it, and the only thing distinguishing them is what came before.
    """
    generators = _macro_generators(source)
    generated = (
        re.compile(rf"^\s*(?:{'|'.join(re.escape(g) for g in generators)})!\(\s*(?P<name>\w+)")
        if generators
        else None
    )

    depth = 0
    test_depth: int | None = None
    pending_cfg_test = False
    pending_export = False
    pending_proc: tuple[ItemKind, str | None] | None = None
    owner: str | None = None
    owner_depth: int | None = None

    for number, raw in enumerate(source.splitlines(), start=1):
        line = _strip_comments(raw)

        if test_depth is None and not pending_cfg_test and _CFG_TEST.match(line):
            pending_cfg_test = True
        elif test_depth is None and pending_cfg_test and line.strip().startswith("mod "):
            test_depth = depth
        elif line.strip() and not line.lstrip().startswith("#["):
            pending_cfg_test = False

        if test_depth is None:
            if _MACRO_EXPORT.match(line):
                pending_export = True
            elif (proc := _PROC_MACRO.match(line)) is not None:
                sort = proc.group("sort")
                kind: ItemKind = (
                    "derive" if sort == "_derive" else "attribute" if sort == "_attribute" else "macro"
                )
                pending_proc = (kind, (proc.group("arg") or "").split(",")[0].strip() or None)
            elif generated is not None and (made := generated.match(line)) is not None:
                yield Item(crate, path, number, "macro", made.group("name"))
            elif (reexport := _REEXPORT.match(line)) is not None:
                target = reexport.group("target").strip()
                alias = reexport.group("alias")
                name = alias or target.rsplit("::", 1)[-1]
                if name.isidentifier():
                    yield Item(crate, path, number, "alias", name, target if alias else None)
            elif (impl := _IMPL.match(line)) is not None:
                owner, owner_depth = impl.group("ty"), depth
            else:
                for item_kind, pattern in _ITEM_PATTERNS:
                    match = pattern.match(line)
                    if match is None:
                        continue
                    name = match.group("name")
                    if item_kind == "macro" and not pending_export:
                        # An unexported macro_rules! is unreachable from a target's code.
                        break
                    if pending_proc is not None:
                        proc_kind, declared = pending_proc
                        yield Item(crate, path, number, proc_kind, declared or name)
                    else:
                        yield Item(
                            crate,
                            path,
                            number,
                            item_kind,
                            name,
                            owner if owner_depth is not None and depth > owner_depth else None,
                        )
                    break
                if line.strip() and not line.lstrip().startswith("#["):
                    pending_export = False
                    pending_proc = None

        depth += line.count("{") - line.count("}")
        if test_depth is not None and depth <= test_depth:
            test_depth = None
            pending_cfg_test = False
        if owner_depth is not None and depth <= owner_depth:
            owner, owner_depth = None, None


def _is_test_path(path: str) -> bool:
    return "/tests/" in path or path.endswith("/tests.rs")


def inventory(crates: MountedCrates) -> tuple[Item, ...]:
    """Every public item the mounted crates export, in path order, one entry per name per crate.

    Deduplicated by ``(crate, name)`` because a re-export and its definition are one thing to
    document, and a completeness gate that demanded both would be counting the same symbol twice.
    The *definition* wins when both are seen, since that is what a reader wants pointed at."""
    found: list[Item] = []
    seen: dict[tuple[str, str], int] = {}
    for path in crates.paths():
        if not path.endswith(".rs") or _is_test_path(path):
            continue
        source = crates.read(path)
        if source is None:
            continue
        for item in items_in(path.split("/", 1)[0], path, source):
            key = (item.crate, item.name)
            if key not in seen:
                seen[key] = len(found)
                found.append(item)
            elif found[seen[key]].kind == "alias" and item.kind != "alias":
                found[seen[key]] = item
    return tuple(found)


@dataclasses.dataclass(frozen=True)
class ExpansionPair:
    """A ``macrotest`` snapshot: a macro invocation beside what it expands to.

    The single most valuable thing in these crates for a reference, and the one question
    ``docs/cvlr-backend-plan.md`` §5.4 says a corpus is the wrong tool for — "what exactly does this
    macro expand to" — answered exactly, by the crate, in the version the reference set pins. It is
    quoted rather than described: an agent asked to describe an expansion would be inferring the
    thing that is sitting right there.
    """

    crate: str
    name: str
    invocation: str
    expansion: str


def expansion_pairs(crates: MountedCrates) -> tuple[ExpansionPair, ...]:
    """Every ``tests/expand/<name>.rs`` that has a ``<name>.expanded.rs`` beside it."""
    by_path = {p: p for p in crates.paths() if "/tests/expand/" in p and p.endswith(".rs")}
    pairs: list[ExpansionPair] = []
    for path in sorted(by_path):
        if path.endswith(".expanded.rs"):
            continue
        expanded = path.removesuffix(".rs") + ".expanded.rs"
        if expanded not in by_path:
            continue
        invocation, expansion = crates.read(path), crates.read(expanded)
        if invocation is None or expansion is None:
            continue
        crate, _, rest = path.partition("/")
        pairs.append(
            ExpansionPair(
                crate=crate,
                name=rest.rsplit("/", 1)[-1].removesuffix(".rs"),
                invocation=invocation,
                expansion=expansion,
            )
        )
    return tuple(pairs)


def uncovered(items: Sequence[Item], text: str) -> tuple[str, ...]:
    """The names in ``items`` that ``text`` never mentions, in first-seen order.

    The completeness gate. Deliberately a substring test rather than anything cleverer: the question
    is whether a reader searching for a symbol finds an entry that mentions it, and that is exactly
    what a substring test measures. A stricter check would fail entries that legitimately cover a
    family by naming its members in one line."""
    missing: list[str] = []
    for item in items:
        if item.name not in text and item.name not in missing:
            missing.append(item.name)
    return tuple(missing)
