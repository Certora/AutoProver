"""Function symbols from a built SBF program, named as the prover names them.

The Solana Prover's tuning files address code by demangled symbol name. An
inlining directive or points-to summary is a regex over strings like
``<vault::VaultState as anchor_lang::AccountDeserialize>::try_deserialize``. A
directive that names a symbol the build does not define matches nothing, with
no diagnostic.

``llvm-readelf --demangle`` is not enough: it leaves ``$LT$``-style escapes and
the trailing hash, so the output does not match the tuning files. platform-tools
emits rustc's legacy mangling (``_ZN``…``E``); a v0 name (``_R``…) is returned
unchanged.
"""

import re
import subprocess
from pathlib import Path

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, PlatformToolsMissing

#: ``llvm-nm`` is not in platform-tools, and the system one mis-reads SBF names
#: (every symbol comes back empty). ``llvm-readelf`` is shipped and works.
_READELF = Path("platform-tools") / "llvm" / "bin" / "llvm-readelf"

_LEGACY = re.compile(r"^_ZN(?P<body>.*)E$")
_HASH_SEGMENT = re.compile(r"^h[0-9a-f]{16}$")
_UNICODE_ESCAPE = re.compile(r"\$u([0-9a-f]{2,6})\$")

#: rustc's legacy mangling. ``$u..$`` covers everything else.
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
    """One path segment, with escapes undone.

    A leading underscore is rustc's, not part of the name: it marks a segment
    that would otherwise start with ``$``.
    """
    if segment.startswith("_$"):
        segment = segment[1:]
    for escaped, plain in _ESCAPES.items():
        segment = segment.replace(escaped, plain)
    segment = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), segment)
    return segment.replace("..", "::")


def demangle(symbol: str) -> str:
    """A raw symbol as the prover's tuning files spell it, or unchanged if it is not mangled.

    Parses length prefixes: a segment can contain digits and an escaped ``$``, so
    the lengths are the only reliable delimiter. Anything that does not parse is
    returned unchanged.
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
    """Every function the program defines, demangled, unique, and sorted.

    Functions only: a summary or inlining directive addresses code. Raises
    :class:`PlatformToolsMissing` when the toolchain is absent — an empty set
    would look like "your directive matched nothing".
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
        # `Num: Value Size Type Bind Vis Ndx Name`
        if len(fields) < 8 or fields[3] != "FUNC" or fields[6] == "UND":
            continue
        names.add(demangle(fields[-1]))
    return tuple(sorted(names))


def unmatched(patterns: tuple[str, ...], symbols: tuple[str, ...]) -> tuple[str, ...]:
    """Which of ``patterns`` match no symbol, in the given order.

    A pattern the regex engine rejects counts as unmatched: the prover would
    reject it too.
    """
    def matches(pattern: str) -> bool:
        try:
            compiled = re.compile(pattern)
        except re.error:
            return False
        return any(compiled.search(s) for s in symbols)

    return tuple(p for p in patterns if not matches(p))


def nearest(pattern: str, symbols: tuple[str, ...], *, limit: int = 3) -> tuple[str, ...]:
    """Symbols to suggest for a pattern that matched nothing.

    Uses the pattern's longest run of identifier characters as a substring
    probe. For ``^<vault::VaultError as core::fmt::Display>::fmt$`` that is
    ``Display``.
    """
    literals = sorted(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pattern), key=len, reverse=True)
    for literal in literals:
        hits = [s for s in symbols if literal in s]
        if hits:
            return tuple(hits[:limit])
    return ()
