"""Reading a built program's symbols the way the prover's tuning files spell them.

A summary or an inlining directive is a regex over demangled names. One that
names a symbol the build does not define matches nothing, and the prover does
not say so. A symbol the compiler inlined away is absent, so no spelling of
it matches.

The mangled forms below are from ``llvm-readelf --syms`` on an SBF build of
``test_scenarios/solana_vault_idl``. Demangling is ``rustfilt``, as it is for
the prover. Tests that run it skip when it is not installed.
:func:`composer.cargo.symbols.prover_function_names` is covered by the expensive gate.
"""

import asyncio
import shutil
import struct

import pytest

from composer.cargo import symbols as symbols_mod
from composer.cargo.symbols import (
    RustfiltMismatch,
    as_the_prover_names,
    demangle,
    nearest,
    prover_function_names,
    unmatched,
)

# (mangled, what rustfilt makes of it)
_REAL = [
    (
        "_ZN69_$LT$vault..VaultState$u20$as$u20$anchor_lang..AccountDeserialize$GT$"
        "15try_deserialize17h353392e41458f0dcE",
        "<vault::VaultState as anchor_lang::AccountDeserialize>::try_deserialize",
    ),
    (
        "_ZN68_$LT$anchor_lang..error..ErrorCode$u20$as$u20$core..fmt..Display$GT$"
        "3fmt17h244eb4f3c380a86dE",
        "<anchor_lang::error::ErrorCode as core::fmt::Display>::fmt",
    ),
    (
        "_ZN5vault13vault_program10initialize17h1a6ff2ac5aebd466E",
        "vault::vault_program::initialize",
    ),
    (
        "_ZN102_$LT$anchor_lang..error..Error$u20$as$u20$core..convert..From$LT$"
        "anchor_lang..error..ErrorCode$GT$$GT$4from17ha928573f00c8008dE",
        "<anchor_lang::error::Error as core::convert::From<anchor_lang::error::ErrorCode>>::from",
    ),
]


needs_rustfilt = pytest.mark.skipif(shutil.which("rustfilt") is None, reason="requires rustfilt")


@needs_rustfilt
@pytest.mark.asyncio
async def test_real_symbols_demangle_to_the_spelling_the_tuning_files_use():
    """``rustfilt`` without ``-h`` strips the hash and undoes the escapes.

    An unmangled name, such as a ``#[rule]``'s ``#[no_mangle]`` symbol, comes
    back unchanged, in its place. The result is matched to the input by position.
    """
    mangled = [m for m, _ in _REAL] + ["rule_authority_matches_signer"]
    expected = [e for _, e in _REAL] + ["rule_authority_matches_signer"]
    assert await demangle(mangled) == expected


def test_a_name_held_by_several_symbols_is_numbered_like_the_prover_numbers_it():
    """Several symbols that demangle to one string are renamed ``name_0`` onward.

    The bare name is not among them, so a directive cannot match it.
    """
    shared = "<anchor_lang::accounts::account::Account<T> as anchor_lang::Accounts<B>>::try_accounts"
    assert as_the_prover_names([shared, shared, shared]) == {f"{shared}_0", f"{shared}_1", f"{shared}_2"}


def test_an_anchored_directive_on_a_renamed_symbol_is_reported_as_inert():
    """An anchor on the bare name misses every renamed copy. A pattern that allows the suffix matches."""
    shared = "<T as core::convert::Into<U>>::into"
    symbols = tuple(sorted(as_the_prover_names([shared, shared])))
    assert unmatched((r"^<T as core::convert::Into<U>>::into$",), symbols) != ()
    assert unmatched((r"^<T as core::convert::Into<U>>::into(_\d+)?$",), symbols) == ()


# ---------------------------------------------------------------------------------------------
# which directives had no effect

_SYMBOLS = tuple(sorted(expected for _, expected in _REAL))


def test_an_invalid_regex_counts_as_unmatched_rather_than_raising():
    """An invalid regex is unmatched, not an exception. The prover rejects it too."""
    assert unmatched((r"^<vault::(unclosed$",), _SYMBOLS) == (r"^<vault::(unclosed$",)


def test_the_suggestion_points_at_what_the_build_does_define():
    """A miss names a symbol the build does define. The probe tries identifier runs, longest first."""
    suggestions = nearest(r"^<vault::VaultError as core::fmt::Display>::fmt$", _SYMBOLS)
    assert "<anchor_lang::error::ErrorCode as core::fmt::Display>::fmt" in suggestions


@pytest.mark.asyncio
async def test_a_rustfilt_result_with_the_wrong_number_of_lines_is_refused(monkeypatch):
    """The prover discards a result whose line count differs from the names it sent."""
    monkeypatch.setattr(shutil, "which", lambda _name: "rustfilt")

    class OneLine:
        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            return b"only-one\n", b""

        async def wait(self) -> int:
            return 0

    async def spawn(*_args, **_kwargs):
        return OneLine()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RustfiltMismatch):
        await demangle(["_ZN3foo", "_ZN3bar"])


@pytest.mark.asyncio
async def test_an_undefined_function_symbol_counts_in_the_collision(tmp_path, monkeypatch):
    """A defined function that demangles like an undefined call is numbered, and the bare name is dropped."""

    async def demangle_all_alike(names: list[str]) -> list[str]:
        return ["shared"] * len(names)

    monkeypatch.setattr(symbols_mod, "demangle", demangle_all_alike)
    path = tmp_path / "prog.so"
    path.write_bytes(_elf_with_functions(defined=("_ZN3fooE",), undefined=("_ZN3barE",)))
    assert await prover_function_names(path) == ("shared_0", "shared_1")


_SHT_STRTAB = 3
_SHT_SYMTAB = 2
_STT_FUNC = 2
_STB_GLOBAL = 1


def _elf_with_functions(*, defined: tuple[str, ...], undefined: tuple[str, ...]) -> bytes:
    """An ELF64 whose symbol table holds these function symbols and nothing else."""
    strtab = bytearray(b"\x00")
    symbols = bytearray(24)  # the null symbol
    for name, shndx in (*((n, 1) for n in defined), *((n, 0) for n in undefined)):
        name_off = len(strtab)
        strtab.extend(name.encode() + b"\x00")
        symbols += struct.pack(
            "<IBBHQQ", name_off, (_STB_GLOBAL << 4) | _STT_FUNC, 0, shndx, 0, 0
        )

    shstr = bytearray(b"\x00")
    offsets = {".shstrtab": 1}
    shstr.extend(b".shstrtab\x00")
    offsets[".strtab"] = len(shstr)
    shstr.extend(b".strtab\x00")
    offsets[".symtab"] = len(shstr)
    shstr.extend(b".symtab\x00")

    strtab_off = 64
    sym_off = strtab_off + len(strtab)
    shstr_off = sym_off + len(symbols)
    shoff = shstr_off + len(shstr)

    def shdr(name_off: int, sh_type: int, offset: int, size: int, link: int = 0, info: int = 0, entsize: int = 0) -> bytes:
        return struct.pack("<IIQQQQIIQQ", name_off, sh_type, 0, 0, offset, size, link, info, 1, entsize)

    headers = bytearray()
    headers += shdr(0, 0, 0, 0)
    headers += shdr(offsets[".shstrtab"], _SHT_STRTAB, shstr_off, len(shstr))
    headers += shdr(offsets[".strtab"], _SHT_STRTAB, strtab_off, len(strtab))
    headers += shdr(offsets[".symtab"], _SHT_SYMTAB, sym_off, len(symbols), link=2, info=1, entsize=24)

    e_ident = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        e_ident, 3, 0x3E, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 4, 1,
    )
    blob = bytearray(shoff + len(headers))
    blob[:64] = header
    blob[strtab_off:strtab_off + len(strtab)] = strtab
    blob[sym_off:sym_off + len(symbols)] = symbols
    blob[shstr_off:shstr_off + len(shstr)] = shstr
    blob[shoff:] = headers
    return bytes(blob)
