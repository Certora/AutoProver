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
``test_scenarios/solana_vault_idl``, and the expected demanglings are what ``rustfilt`` produced for
the same input. No toolchain is needed to run these — :func:`defined_functions`, which shells out to
``llvm-readelf``, is exercised by the expensive gate.
"""

import pytest

from composer.cargo.symbols import demangle, nearest, unmatched

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


@pytest.mark.parametrize("mangled,expected", _REAL, ids=[e for _, e in _REAL])
def test_a_real_symbol_demangles_the_way_rustfilt_does(mangled, expected):
    """The names have to match the tuning files exactly or every directive is a near miss.

    ``llvm-readelf --demangle`` is not a substitute and that is why this exists: it strips the
    ``_ZN``/length framing but leaves ``$LT$``-style escapes and the trailing hash, so its output
    matches nothing the prover reads."""
    assert demangle(mangled) == expected


def test_an_unmangled_symbol_is_left_alone():
    """``#[rule]`` adds ``#[no_mangle]``, so a rule's symbol is its function name verbatim."""
    assert demangle("rule_authority_matches_signer") == "rule_authority_matches_signer"


def test_a_v0_symbol_is_returned_untouched_rather_than_guessed_at():
    """platform-tools emits legacy mangling today. A decoder that does not understand v0 must hand
    the name back rather than invent one, because a wrong name is a directive nobody can match."""
    assert demangle("_RNvCs1234_5vault10initialize") == "_RNvCs1234_5vault10initialize"


@pytest.mark.parametrize("junk", ["_ZNE", "_ZN99tooshortE", "_ZNxE", ""])
def test_a_name_that_does_not_parse_comes_back_as_it_went_in(junk):
    assert demangle(junk) == junk


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
