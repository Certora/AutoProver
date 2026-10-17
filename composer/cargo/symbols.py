"""Function symbols from a built SBF program, named as the prover names them.

Tuning files address code by demangled symbol. A directive that names a symbol
the build does not define matches nothing, and the prover does not say so.

The prover demangles with ``rustfilt`` and no ``-h``, which strips hashes.
This module does the same. ``llvm-readelf --demangle``, ``llvm-cxxfilt``, and
the other LLVM demanglers do not understand rustc's ``$LT$`` escapes. GNU
``c++filt`` understands them and keeps the hash.
"""

import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection


class SymbolsUnavailable(RuntimeError):
    """The build's function names cannot be read.

    This is not an empty symbol list. An empty list would make every directive
    look unmatched.
    """


class RustfiltMissing(SymbolsUnavailable):
    def __init__(self):
        super().__init__("rustfilt is not on PATH; install it with `cargo install rustfilt`.")


class NoSymbolTable(SymbolsUnavailable):
    def __init__(self, shared_object: Path):
        super().__init__(f"{shared_object} has no symbol table; was it stripped?")


def defined_functions(shared_object: Path) -> tuple[str, ...]:
    """Every function the program defines, named as the prover names it, sorted."""
    demangled = demangle(_mangled_functions(shared_object))
    return tuple(sorted(as_the_prover_names(demangled)))


def as_the_prover_names(demangled: Iterable[str]) -> set[str]:
    """Demangled names, renamed the way the prover renames them.

    Dropping the hash collides. Every instantiation of a generic, and every
    duplicate copy of a crate, demangles to the same string. The prover renames
    all ``n`` holders of a shared name to ``name_0`` … ``name_{n-1}``. A
    directive anchored with ``$`` then matches none of them.
    """
    counts = Counter(demangled)
    return {
        renamed
        for name, count in counts.items()
        for renamed in ([name] if count == 1 else (f"{name}_{i}" for i in range(count)))
    }


def _mangled_functions(shared_object: Path) -> list[str]:
    with shared_object.open("rb") as stream:
        symtab = ELFFile(stream).get_section_by_name(".symtab")
        if not isinstance(symtab, SymbolTableSection):
            raise NoSymbolTable(shared_object)
        return sorted({
            symbol.name
            for symbol in symtab.iter_symbols()
            if symbol["st_info"]["type"] == "STT_FUNC" and symbol["st_shndx"] != "SHN_UNDEF"
        })


def demangle(names: list[str]) -> list[str]:
    """``names`` as ``rustfilt`` spells them, in the same order.

    A name that is not mangled comes back unchanged. Callers match by position.
    """
    rustfilt = shutil.which("rustfilt")
    if rustfilt is None:
        raise RustfiltMissing()
    run = subprocess.run(
        [rustfilt], input="".join(f"{n}\n" for n in names), capture_output=True, text=True, check=True
    )
    return run.stdout.splitlines()


def unmatched(patterns: tuple[str, ...], symbols: tuple[str, ...]) -> tuple[str, ...]:
    """Which of ``patterns`` match no symbol, in the given order.

    A pattern the regex engine rejects counts as unmatched. The prover rejects
    it too.
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
