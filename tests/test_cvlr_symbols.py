"""Reading a built program's symbols the way the prover's tuning files spell them.

A summary or an inlining directive is a regex over demangled names. One that
names a symbol the build does not define matches nothing, and the prover does
not say so. A symbol the compiler inlined away is absent, so no spelling of
it matches.

The mangled forms below are from ``llvm-readelf --syms`` on an SBF build of
``test_scenarios/solana_vault_idl``. Demangling is ``rustfilt``, as it is for
the prover. Tests that run it skip when it is not installed.
:func:`defined_functions` is covered by the expensive gate.
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
    """``rustfilt`` without ``-h`` strips the hash and undoes the escapes.

    An unmangled name, such as a ``#[rule]``'s ``#[no_mangle]`` symbol, comes
    back unchanged, in its place. The result is matched to the input by position.
    """
    mangled = [m for m, _ in _REAL] + ["rule_authority_matches_signer"]
    expected = [e for _, e in _REAL] + ["rule_authority_matches_signer"]
    assert demangle(mangled) == expected


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
    """A miss names a symbol the build does define, taken from the pattern's longest identifier."""
    suggestions = nearest(r"^<vault::VaultError as core::fmt::Display>::fmt$", _SYMBOLS)
    assert "<anchor_lang::error::ErrorCode as core::fmt::Display>::fmt" in suggestions
