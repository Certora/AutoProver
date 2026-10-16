"""Function symbols from a built SBF program, named as the prover names them.

The Solana Prover's tuning files address code by demangled symbol name. An
inlining directive or points-to summary is a regex over strings like
``<vault::VaultState as anchor_lang::AccountDeserialize>::try_deserialize``. A
directive that names a symbol the build does not define matches nothing, with
no diagnostic.

The prover demangles by piping names through ``rustfilt`` (without ``-h``, so
hashes are stripped), and so does this module: any other demangler is a second
opinion on a spelling only ``rustfilt`` gets to decide. ``llvm-readelf
--demangle``, ``llvm-cxxfilt`` and the LLVM-based demanglers do not understand
rustc's legacy ``$LT$``-style escapes; GNU ``c++filt`` does, but keeps the hash.
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
    """The build's function names cannot be read. Distinct from an empty set,
    which would look like "your directive matched nothing"."""


class RustfiltMissing(SymbolsUnavailable):
    def __init__(self):
        super().__init__("rustfilt is not on PATH; install it with `cargo install rustfilt`.")


class NoSymbolTable(SymbolsUnavailable):
    def __init__(self, shared_object: Path):
        super().__init__(f"{shared_object} has no symbol table; was it stripped?")


def defined_functions(shared_object: Path) -> tuple[str, ...]:
    """Every function the program defines, named as the prover names it, sorted.

    Functions only: a summary or inlining directive addresses code.
    """
    demangled = demangle(_mangled_functions(shared_object))
    return tuple(sorted(as_the_prover_names(demangled)))


def as_the_prover_names(demangled: Iterable[str]) -> set[str]:
    """Demangled names, disambiguated the way the prover disambiguates them.

    Dropping the hash is not injective: every instantiation of a generic, and
    every duplicate copy of a crate, demangles to the same string. The prover
    then renames all ``n`` holders of a shared name to ``name_0`` … ``name_{n-1}``,
    so a directive anchored with ``$`` matches none of them.
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
    """``names`` as ``rustfilt`` spells them, in order; one that is not mangled comes back as it went in."""
    rustfilt = shutil.which("rustfilt")
    if rustfilt is None:
        raise RustfiltMissing()
    run = subprocess.run(
        [rustfilt], input="".join(f"{n}\n" for n in names), capture_output=True, text=True, check=True
    )
    return run.stdout.splitlines()


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
