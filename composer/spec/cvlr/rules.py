"""Which rules a CVLR harness declares, read from the source.

A submission names these rules. A conf with no ``rule`` entry makes the cloud
job end without a report. Both declaration forms name their rules
deterministically:

* ``#[rule]`` adds ``#[no_mangle]`` (``cvlr-macros``), so the symbol is the
  function name.
* ``cvlr_rules!`` and ``cvlr_invariant_rules!`` expand to one
  ``cvlr_rule_for_spec!`` per base. That macro builds the name as
  ``snake_case(name) + "_" + base``, with a ``base_`` prefix stripped from
  the base. :func:`snake_case` matches the crate's own function. The tests
  check it against the examples in the crate's documentation.

:func:`rule_names` returns the names. A declaration it can see but cannot
name is refused (:class:`UnreadableRules`): the names it did read are not
returned, because a submission built from that partial list can stamp a draft
whose other rules were never checked.
"""

import re

from composer.spec.types import RuleName


class UnreadableRules(ValueError):
    """A declaration was visible, and its rule name could not be read.

    The names that were readable are not available. A stamp over that partial
    list would accept a draft whose other rules were never checked.
    """


class _Metavar:
    """The ``fn`` after ``#[rule]`` is a macro metavariable, not a rule."""


_METAVAR = _Metavar()

#: ``rule``, ``cvlr::rule``, and ``cvlr_rule``, with or without attribute
#: tokens. The proc macro ignores the tokens; the symbol is the function name.
#: Other attributes, a doc comment, and ``pub`` / ``unsafe`` may sit between
#: the attribute and the function. Not limited to one line.
_RULE_INTERIOR = re.compile(
    r"\s*(?:cvlr\s*::\s*)?(?:cvlr_rule|rule)\b\s*(?:\(.*\))?\s*",
    re.DOTALL,
)

#: ``cvlr_rule_for_spec`` is listed first so it is not read as another macro.
#: All three take ``name:``. The two list macros take ``bases:``; the direct
#: macro takes one ``base:``. How the spec is given does not affect the names.
_PARAMETRIC = re.compile(r"\b(cvlr_rule_for_spec|cvlr_invariant_rules|cvlr_rules)\s*!")

_NAME_FIELD = re.compile(r"\bname\s*:\s*\"([^\"]*)\"")
_BASES_FIELD = re.compile(r"\bbases\s*:\s*\[")
_ONE_BASE = re.compile(r"\bbase\s*:\s*([A-Za-z_][A-Za-z0-9_]*)")
_ONE_BASE_FIELD = re.compile(r"\bbase\s*:")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIERS = ("const", "async", "unsafe")

_OPENERS = {"{": "}", "(": ")", "[": "]"}


def snake_case(name: str) -> str:
    """The crate's ``to_snake_case``.

    Lowercase, every non-alphanumeric character to ``_``, then collapse runs
    and trim. ``"Vault Solvency"`` and ``"vault-solvency"`` both become
    ``vault_solvency``. The generated symbol is what the prover reports, so a
    different spelling would not match the report.
    """
    mapped = "".join(c if c.isalnum() or c == "_" else "_" for c in name.lower())
    return "_".join(part for part in mapped.split("_") if part)


def generated_name(spec_name: str, base: str) -> RuleName:
    """The rule ``cvlr_rule_for_spec!`` emits for one ``(name, base)`` pair."""
    stripped = base.removeprefix("base_")
    snake = snake_case(spec_name)
    return RuleName(f"{snake}_{stripped}" if stripped else snake)


def _matching(text: str, opener_at: int) -> int:
    """Index just past the delimiter matching the one at ``opener_at``.

    The spec expression between ``name:`` and ``bases:`` can contain its own
    brackets (``cvlr_and(a, b)``, an array, a closure). A regex for
    ``bases: [...]`` would match one of those.
    """
    opener = text[opener_at]
    closer = _OPENERS[opener]
    depth = 0
    index = opener_at
    while index < len(text):
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _blank_comments_and_strings(source: str) -> str:
    """Replace comments and string interiors with spaces, keeping newlines.

    A ``fn`` or ``#[rule]`` inside a doc comment or a string is then not a
    token. Quote characters stay, so a ``name: "..."`` field is still found
    and its text is read from the original source at the same span. Newlines
    stay so a ``//`` comment does not join the next line.
    """
    out = list(source)
    n = len(source)
    i = 0

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if source[k] != "\n":
                out[k] = " "

    while i < n:
        if source.startswith("//", i):
            end = source.find("\n", i)
            blank(i, n if end < 0 else end)
            i = n if end < 0 else end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            blank(i, n if end < 0 else end + 2)
            i = n if end < 0 else end + 2
            continue
        literal = _string_literal(source, i)
        if literal is not None:
            interior, resume = literal
            blank(*interior)
            i = resume
            continue
        i += 1
    return "".join(out)


def _string_literal(source: str, i: int) -> tuple[tuple[int, int], int] | None:
    """The interior span of a string starting at ``i``, and the index after it.

    ``None`` when ``i`` is not the first character of a string literal. Byte
    and C-string prefixes are included. A character literal is left alone: it
    cannot hold a ``fn`` token.
    """
    n = len(source)
    if i > 0 and (source[i - 1].isalnum() or source[i - 1] == "_"):
        return None
    j = i
    if j < n and source[j] in "bc":
        nxt = source[j + 1] if j + 1 < n else ""
        if nxt in {'"', "r"}:
            j += 1
    if j < n and source[j] == "r":
        k = j + 1
        hashes = 0
        while k < n and source[k] == "#":
            hashes += 1
            k += 1
        if k < n and source[k] == '"':
            interior_start = k + 1
            needle = '"' + ("#" * hashes)
            end = source.find(needle, interior_start)
            if end < 0:
                return (interior_start, n), n
            return (interior_start, end), end + len(needle)
        return None
    if j >= n or source[j] != '"':
        return None
    k = j + 1
    while k < n:
        if source[k] == "\\":
            k += 2
            continue
        if source[k] == '"':
            return (j + 1, k), k + 1
        k += 1
    return (j + 1, n), n


def _word(text: str, i: int, word: str) -> bool:
    end = i + len(word)
    if not text.startswith(word, i):
        return False
    return end >= len(text) or not (text[end].isalnum() or text[end] == "_")


def _snippet(source: str, index: int) -> str:
    line_start = source.rfind("\n", 0, index) + 1
    line_end = source.find("\n", index)
    return source[line_start : len(source) if line_end < 0 else line_end].strip()


def _rule_attributes(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each rule attribute. ``end`` is just past its ``]``."""
    found: list[tuple[int, int]] = []
    i = 0
    while True:
        start = text.find("#[", i)
        if start < 0:
            break
        end = _matching(text, start + 1)
        interior = text[start + 2 : end - 1] if end > start + 2 else ""
        if _RULE_INTERIOR.fullmatch(interior):
            found.append((start, end))
        i = end if end > start else start + 2
    return found


def _function_after(text: str, pos: int) -> str | _Metavar | None:
    """The function the attribute ending at ``pos`` decorates.

    ``None`` when the next item is not a function. A ``$`` name is a macro
    metavariable: the template declares no rule until an invocation pastes one.
    """
    i = pos
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return None
        if text.startswith("#[", i):
            i = _matching(text, i + 1)
            continue
        if _word(text, i, "pub"):
            i += 3
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] == "(":
                i = _matching(text, i)
            continue
        step = next((len(kw) for kw in _QUALIFIERS if _word(text, i, kw)), 0)
        if step:
            i += step
            continue
        if _word(text, i, "extern"):
            i += 6
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] == '"':
                close = text.find('"', i + 1)
                if close < 0:
                    return None
                i = close + 1
            continue
        if _word(text, i, "fn"):
            i += 2
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] == "$":
                return _METAVAR
            matched = _IDENT.match(text, i)
            return None if matched is None else matched.group(0)
        return None
    return None


def _spec_name(original: str, blanked: str) -> str | None:
    """The ``name:`` literal, read from ``original`` at the span found in ``blanked``."""
    match = _NAME_FIELD.search(blanked)
    if match is None:
        return None
    return original[match.start(1) : match.end(1)]


def _macro_body(text: str, bang_end: int) -> tuple[int, int] | None:
    """The span of the group after ``!``, including its delimiters."""
    body_start = next(
        (i for i in range(bang_end, len(text)) if not text[i].isspace()), len(text)
    )
    if body_start >= len(text) or text[body_start] not in _OPENERS:
        return None
    return body_start, _matching(text, body_start)


def _listed_bases(listed: str) -> tuple[str, ...] | None:
    """Identifiers in a ``bases:`` list. ``None`` when an entry is not an identifier."""
    bases: list[str] = []
    for raw in listed.split(","):
        piece = raw.strip()
        if not piece:
            continue
        if _IDENT.fullmatch(piece) is None:
            return None
        bases.append(piece)
    return tuple(bases)


def _parametric_names(
    source: str, blanked: str, kind: str, bang_end: int
) -> tuple[RuleName, ...] | str | None:
    """Names one invocation expands to, ``None`` when it declares none, or why it could not be read."""
    span = _macro_body(blanked, bang_end)
    if span is None:
        return None
    start, end = span
    body = blanked[start:end]
    name = _spec_name(source[start:end], body)
    if kind == "cvlr_rule_for_spec":
        base = _ONE_BASE.search(body)
        if name is not None and base is not None:
            return (generated_name(name, base.group(1)),)
        if _ONE_BASE_FIELD.search(body) is not None and base is None:
            return f"`{_snippet(source, start)}` has a base that is not an identifier"
        if name is None and base is not None:
            return f"`{_snippet(source, start)}` does not give the rule a name"
        return None

    bases_match = _BASES_FIELD.search(body)
    if bases_match is None:
        return None
    bracket = bases_match.end() - 1
    listed = body[bracket + 1 : _matching(body, bracket) - 1]
    bases = _listed_bases(listed)
    if bases is None:
        return f"`{_snippet(source, start)}` has a base that is not an identifier"
    if not bases:
        return None
    if name is None:
        return f"`{_snippet(source, start)}` does not give the rule a name"
    return tuple(generated_name(name, base) for base in bases)


def rule_names(source: str) -> tuple[RuleName, ...]:
    """Every rule name ``source`` declares, in declaration order, without duplicates.

    A duplicated name is a compiler error. This function reports it once.
    Comments and string literals are blanked before matching, and ``#[rule]``
    binds to the function it decorates. A ``#[rule]`` inside a macro template
    (``fn $name``) declares nothing until an invocation pastes a name. Names
    from one parametric invocation stay in ``bases`` order.

    Raises :class:`UnreadableRules` when a declaration is visible and does not
    yield a name. The names that were readable are withheld.
    """
    blanked = _blank_comments_and_strings(source)
    found: list[tuple[int, tuple[RuleName, ...]]] = []
    problems: list[str] = []

    for start, end in _rule_attributes(blanked):
        bound = _function_after(blanked, end)
        if isinstance(bound, _Metavar):
            continue
        if bound is None:
            problems.append(f"`{_snippet(source, start)}` does not decorate a function")
            continue
        found.append((start, (RuleName(bound),)))

    for macro in _PARAMETRIC.finditer(blanked):
        parsed = _parametric_names(source, blanked, macro.group(1), macro.end())
        if isinstance(parsed, str):
            problems.append(parsed)
        elif parsed is not None:
            found.append((macro.start(), parsed))

    if problems:
        raise UnreadableRules(
            "a rule declaration could not be read ("
            + "; ".join(problems)
            + "). The names that were read are withheld."
        )

    seen: dict[RuleName, None] = {}
    for _, names in sorted(found, key=lambda pair: pair[0]):
        for name in names:
            seen.setdefault(name, None)
    return tuple(seen)
