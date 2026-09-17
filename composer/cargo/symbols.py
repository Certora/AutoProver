"""Reading the function symbols out of a built SBF program, spelled the way the prover spells them.

The Solana Prover's tuning files address code by *demangled* symbol name — an inlining directive or a
points-to summary is a regex over strings like
``<vault::VaultState as anchor_lang::AccountDeserialize>::try_deserialize``. So a directive that
names a symbol the build does not define matches nothing and does nothing, and there is no
diagnostic: the run reports the same error it reported before, and the author is left guessing at
spellings. An end-to-end run wrote five variants of one directive for exactly that reason —
``(_\\d+)?`` for a suspected demangling suffix, ``.*Display`` for an unknown trait path, ``std::`` in
case it was not ``core::`` — and every one of them missed, because the symbol it was reaching for had
been inlined out of existence and no spelling would have found it.

Two things are worth knowing about the names, both learned by comparing against ``rustfilt``:

* ``llvm-readelf --demangle`` is *not* enough. It strips the ``_ZN``/length framing and joins
  segments, but leaves the ``$LT$``-style escapes and the trailing hash — so its output does not
  match what the tuning files contain. The demangling here is done from the raw names instead.
* rustc's *legacy* mangling is what platform-tools emits (``_ZN``…``E``). A v0 name (``_R``…) is
  returned untouched rather than mangled further by a decoder that does not understand it.
"""

import re
import subprocess
from pathlib import Path

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, PlatformToolsMissing

#: ``llvm-nm`` is not in the platform-tools distribution and the system one mis-reads SBF names
#: (every symbol comes back empty), so the reader is ``llvm-readelf``, which is shipped.
_READELF = Path("platform-tools") / "llvm" / "bin" / "llvm-readelf"

_LEGACY = re.compile(r"^_ZN(?P<body>.*)E$")
_HASH_SEGMENT = re.compile(r"^h[0-9a-f]{16}$")
_UNICODE_ESCAPE = re.compile(r"\$u([0-9a-f]{2,6})\$")

#: The punctuation rustc's legacy mangling escapes. ``$u..$`` covers everything else.
_ESCAPES = {
    "$SP$": "@",
    "$BP$": "*",
    "$RF$": "&",
    "$LT$": "<",
    "$GT$": ">",
    "$LP$": "(",
    "$RP$": ")",
    "$C$": ",",
}


def _decode(segment: str) -> str:
    """One path segment: drop the disambiguating underscore, decode escapes, ``..`` back to ``::``."""
    if segment.startswith("_$"):
        segment = segment[1:]
    for escaped, plain in _ESCAPES.items():
        segment = segment.replace(escaped, plain)
    segment = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), segment)
    return segment.replace("..", "::")


def demangle(symbol: str) -> str:
    """A raw symbol as the prover's tuning files spell it, or unchanged if it is not mangled.

    Length-prefixed parsing rather than a regex over the whole name, because a segment may itself
    contain digits and an escaped ``$``; the lengths are the only unambiguous delimiter. Anything
    that does not parse comes back as it went in — a wrong guess here would be a symbol name nobody
    could match, which is the failure this module exists to report.
    """
    found = _LEGACY.match(symbol)
    if found is None:
        return symbol
    body, segments, index = found.group("body"), [], 0
    while index < len(body):
        digits = index
        while digits < len(body) and body[digits].isdigit():
            digits += 1
        if digits == index:
            return symbol
        length = int(body[index:digits])
        segment = body[digits : digits + length]
        if len(segment) != length:
            return symbol
        segments.append(segment)
        index = digits + length
    if segments and _HASH_SEGMENT.match(segments[-1]):
        segments.pop()
    return "::".join(_decode(s) for s in segments) if segments else symbol


def defined_functions(shared_object: Path, *, tools_version: str) -> tuple[str, ...]:
    """Every function the program defines, demangled, deduplicated and sorted.

    Functions only: a summary or an inlining directive addresses code, and including data symbols
    would let a directive "match" something it can never affect.

    Raises :class:`PlatformToolsMissing` when the toolchain is absent — a caller that cannot read
    symbols must say so rather than report an empty set, which reads identically to "your directive
    matched nothing" and would send the author after the wrong problem.
    """
    reader = PLATFORM_TOOLS_ROOT / tools_version / _READELF
    if not reader.is_file():
        raise PlatformToolsMissing(tools_version, PLATFORM_TOOLS_ROOT)
    run = subprocess.run(
        [str(reader), "--syms", str(shared_object)],
        capture_output=True,
        text=True,
        check=False,
    )
    names: set[str] = set()
    for line in run.stdout.splitlines():
        fields = line.split()
        # `Num: Value Size Type Bind Vis Ndx Name` — a defined function has a section index, so a
        # name-less or undefined row is skipped rather than guessed at.
        if len(fields) < 8 or fields[3] != "FUNC" or fields[6] == "UND":
            continue
        names.add(demangle(fields[-1]))
    return tuple(sorted(names))


def unmatched(patterns: tuple[str, ...], symbols: tuple[str, ...]) -> tuple[str, ...]:
    """Which of ``patterns`` match no symbol at all, in the order given.

    A pattern the regex engine rejects counts as unmatched: the prover would reject it too, and
    "your directive is not valid" and "your directive found nothing" want the same response from the
    author.
    """
    def matches(pattern: str) -> bool:
        try:
            compiled = re.compile(pattern)
        except re.error:
            return False
        return any(compiled.search(s) for s in symbols)

    return tuple(p for p in patterns if not matches(p))


def nearest(pattern: str, symbols: tuple[str, ...], *, limit: int = 3) -> tuple[str, ...]:
    """Symbols worth suggesting for a pattern that matched nothing.

    The pattern's longest run of plain identifier characters is used as a substring probe — for
    ``^<vault::VaultError as core::fmt::Display>::fmt$`` that is ``Display``, which finds the
    ``Display`` impls that *do* survive the build. Crude on purpose: the useful answer is usually
    "the type you named was inlined away, here is what is actually there", and a ranked edit
    distance would not say that any better.
    """
    literals = sorted(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pattern), key=len, reverse=True)
    for literal in literals:
        hits = [s for s in symbols if literal in s]
        if hits:
            return tuple(hits[:limit])
    return ()
