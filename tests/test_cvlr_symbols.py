"""Reading a built program's symbols the way the prover's tuning files spell them.

A summary or an inlining directive is a regex over demangled symbol names, so one that names a
symbol the build does not define matches nothing, does nothing, and says nothing. An end-to-end run
wrote five variants of a single directive hunting for a spelling that would take —

    ^<vault::VaultError as core::fmt::Display>::fmt$
    ^<vault::VaultError as core::fmt::Display>::fmt(_\\d+)?$
    ^<anchor_lang::error::AnchorError as core::fmt::Display>::fmt(_\\d+)?$
    ^<vault::VaultError as .*Display>::fmt
    ^<vault::VaultError as std::fmt::Display>::fmt

— and all five missed. Not because of the spelling: the program's own ``VaultError::Display`` had
been inlined out of existence, so no spelling would have matched, and the symbol it needed was
Anchor's ``ErrorCode::Display``, which does survive. The point of reporting a miss is to say that.

The mangled forms below are verbatim from ``llvm-readelf --syms`` on a real SBF build of
``test_scenarios/solana_vault_idl``. Demangling is ``rustfilt``'s, as it is the prover's; the tests
that run it skip when it is not installed. :func:`defined_functions` is exercised by the expensive
gate.
"""

import shutil

import pytest

from composer.cargo.symbols import as_the_prover_names, demangle, nearest, unmatched

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
def test_real_symbols_demangle_to_the_spelling_the_tuning_files_use():
    """The prover runs ``rustfilt`` without ``-h``, so the hash goes and the escapes are undone.
    An unmangled name is a ``#[rule]``'s ``#[no_mangle]`` symbol and must come back verbatim, in
    its place: the result is matched to the input by position."""
    mangled = [m for m, _ in _REAL] + ["rule_authority_matches_signer"]
    expected = [e for _, e in _REAL] + ["rule_authority_matches_signer"]
    assert demangle(mangled) == expected


def test_a_name_held_by_one_symbol_is_kept_as_is():
    assert as_the_prover_names(["vault::vault_program::initialize"]) == {"vault::vault_program::initialize"}


def test_a_name_held_by_several_symbols_is_numbered_like_the_prover_numbers_it():
    """Each instantiation of a generic demangles to the same string once its hash is dropped, and
    the prover renames every holder. The bare name no longer exists for a directive to match."""
    shared = "<anchor_lang::accounts::account::Account<T> as anchor_lang::Accounts<B>>::try_accounts"
    assert as_the_prover_names([shared, shared, shared]) == {f"{shared}_0", f"{shared}_1", f"{shared}_2"}


def test_an_anchored_directive_on_a_renamed_symbol_is_reported_as_inert():
    """``$`` after the bare name is the natural way to write it, and it misses every renamed copy."""
    shared = "<T as core::convert::Into<U>>::into"
    symbols = tuple(sorted(as_the_prover_names([shared, shared])))
    assert unmatched((r"^<T as core::convert::Into<U>>::into$",), symbols) != ()
    assert unmatched((r"^<T as core::convert::Into<U>>::into(_\d+)?$",), symbols) == ()


# ---------------------------------------------------------------------------------------------
# which directives had no effect

_SYMBOLS = tuple(sorted(expected for _, expected in _REAL))


def test_a_directive_naming_a_present_symbol_is_not_reported():
    assert unmatched((r"^<anchor_lang::error::ErrorCode as core::fmt::Display>::fmt$",), _SYMBOLS) == ()


def test_the_directive_the_run_actually_wrote_is_reported_as_inert():
    """The measured case. Every variant the run tried names ``vault::VaultError``, which is in no
    build because it was inlined."""
    missed = unmatched((r"^<vault::VaultError as core::fmt::Display>::fmt$",), _SYMBOLS)
    assert len(missed) == 1


def test_an_invalid_regex_counts_as_unmatched_rather_than_raising():
    """The prover would reject it too, and both want the same response from the author. Raising here
    would instead take down the tool that was reporting on it."""
    assert unmatched((r"^<vault::(unclosed$",), _SYMBOLS) == (r"^<vault::(unclosed$",)


def test_the_suggestion_points_at_what_the_build_does_define():
    """The whole value of the report. Told only "matched nothing", the run guessed spellings five
    times; the useful answer names a symbol that is there."""
    suggestions = nearest(r"^<vault::VaultError as core::fmt::Display>::fmt$", _SYMBOLS)
    assert "<anchor_lang::error::ErrorCode as core::fmt::Display>::fmt" in suggestions


def test_a_pattern_with_nothing_to_probe_suggests_nothing():
    """Rather than dumping the whole symbol table at the author."""
    assert nearest(r"^.*$", _SYMBOLS) == ()
