"""Reading Rust source far enough to find a body's end and a type's derives.

The end of a definition is its matching close brace, and that is where a regex stops being enough: a
brace inside a string, a raw string, a character literal such as ``'\\u{1F600}'`` or a nested
comment opens and closes nothing. :func:`code_positions` is what tells the two apart, and
:func:`body_span` is built on it.

**Why it is not a Rust parser.** Its one reader, :mod:`composer.spec.cvlr.anchor_surface`, reads the
program's own source for the names Anchor's macros generate from it and shows them to the author,
who hands them to the compiler; a misjudged span is a wrong name the build rejects, not a silent
change to the program.
"""

import re
from collections.abc import Iterator


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


#: A ``#[derive(..)]`` attribute and the list inside it: the whole attribute is the match and
#: ``items`` is the payload.
DERIVE_LIST = re.compile(r"#\[derive\(\s*(?P<items>[^)]*?)\s*\)\]")
