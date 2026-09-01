"""Spelling the canonical tuning files for the platform generation a target is on.

``docs/cvlr-backend-plan.md`` §7.5.6. The defect these guard against does not raise, log, or fail a
build: a directive whose path was renamed out from under it simply matches nothing, and the prover
proceeds with a different configuration than the file appears to describe. So the tests here are
mostly about *silence* — that a rewrite happens where it must, that it does not happen where it must
not, and that the vendored files stay byte-identical to upstream so the refresh script remains a
copy.

The riskiest of them is :func:`test_every_declared_alias_still_names_something_in_the_vendored_files`.
An alias is written against a file that upstream owns; when that file is refreshed, an alias whose
canonical path no longer appears is dead weight that looks like coverage.
"""

from pathlib import Path

import pytest

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr.env_paths import PathDialect, dialect_for
from composer.spec.cvlr.scaffold import (
    CANONICAL_ENVS,
    ENV_DIR,
    INLINING,
    canonical_env,
    compose_env,
)
from composer.spec.cvlr_reference import SOLANA, SOROBAN, NamespacePattern, PathAlias

#: Every crate the post-split Solana platform layer is spread across, as a real target resolves them.
#: Verified against ``test_scenarios/solana_vault_idl``'s ``Cargo.lock``.
SPLIT_CRATES = (
    "solana-account-info",
    "solana-pubkey",
    "solana-program-error",
    "solana-program-pack",
    "solana-rent",
    "solana-clock",
    "solana-sysvar",
    "solana-hash",
    "solana-sdk-ids",
    "solana-cpi",
    "solana-instruction",
)


def _workspace(*resolved: str) -> Workspace:
    """A ``Workspace`` that resolves exactly ``resolved``, and nothing else.

    Only the resolved-package list matters here: the dialect reads which crates exist and nothing
    about the files on disk."""
    packages = tuple(
        CratePackage(
            name=name,
            version="2.3.0",
            manifest_path=Path("/nonexistent") / name / "Cargo.toml",
            lib=None,
            features=(),
            source="registry+https://github.com/rust-lang/crates.io-index",
        )
        for name in resolved
    )
    return Workspace(
        root=Path("/nonexistent"),
        target_directory=Path("/nonexistent/target"),
        members=(),
        packages=packages,
    )


@pytest.fixture
def split() -> PathDialect:
    """The dialect a post-split target gets — the case every real Solana target is now in."""
    return dialect_for(_workspace("solana-program", *SPLIT_CRATES), SOLANA)


@pytest.fixture
def monolithic() -> PathDialect:
    """The dialect a 1.18 target gets, where the canonical paths are already the right ones."""
    return dialect_for(_workspace("solana-program"), SOLANA)


# ---------------------------------------------------------------------------------------------
# rewriting


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        (
            "^solana_program::account_info::AccountInfo::lamports$",
            "^solana_account_info::AccountInfo::lamports$",
        ),
        (
            "^solana_program::pubkey::Pubkey::find_program_address$",
            "^solana_pubkey::Pubkey::find_program_address$",
        ),
        (
            "^<solana_program::program_error::ProgramError as core::convert::From<u64>>::from$",
            "^<solana_program_error::ProgramError as core::convert::From<u64>>::from$",
        ),
        ("^solana_program::system_program::id$", "^solana_sdk_ids::system_program::id$"),
        ("^solana_program::incinerator::check_id$", "^solana_sdk_ids::incinerator::check_id$"),
        ("^solana_program::rent::Rent::minimum_balance$", "^solana_rent::Rent::minimum_balance$"),
        # Three concepts in one pattern, which is why aliases are substrings rather than prefixes.
        (
            "^solana_program::sysvar::rent::<impl solana_program::sysvar::Sysvar for "
            "solana_program::rent::Rent>::get$",
            "^solana_sysvar::rent::<impl solana_sysvar::Sysvar for solana_rent::Rent>::get$",
        ),
    ],
)
def test_a_renamed_concept_is_spelled_as_the_defining_crate(
    split: PathDialect, canonical: str, expected: str
) -> None:
    assert split.spellings(canonical) == (expected,)


def test_a_symbol_that_survived_the_split_is_left_alone(split: PathDialect) -> None:
    """``solana-program`` is a *partial* facade, and reading the module rather than the symbol got
    this wrong twice: ``invoke``, ``invoke_signed``, ``set_return_data`` and ``get_stack_height`` are
    real functions there on the split generation, and appear in a real target's symbol table under
    the canonical spelling."""
    for pattern in (
        "^solana_program::program::invoke$",
        "^solana_program::program::invoke_signed$",
        "^solana_program::program::set_return_data$",
        "^solana_program::instruction::get_stack_height$",
    ):
        assert split.spellings(pattern) == (pattern,)


def test_a_concept_on_both_sides_of_the_split_is_emitted_under_both_spellings(
    split: PathDialect,
) -> None:
    """``solana-program`` kept a real ``invoke_signed_unchecked`` while the one that ends up on the
    call path is ``solana-cpi``'s. A summary that covered only one of them would leave the other
    fully analyzed, which is the state that made this whole investigation necessary."""
    assert split.spellings("^solana_program::program::invoke_signed_unchecked$") == (
        "^solana_program::program::invoke_signed_unchecked$",
        "^solana_cpi::invoke_signed_unchecked$",
    )


def test_a_symbol_level_alias_beats_the_module_it_sits_inside() -> None:
    """Longest canonical wins, which is what lets a partial facade be described at all: the module
    gets one answer and the one symbol that disagrees with it gets another. Shortest-first would
    apply the module's answer and leave the specific alias with nothing to match."""
    dialect = PathDialect(
        (
            PathAlias("solana_program::program", ("wholesale",)),
            PathAlias("solana_program::program::invoke_signed_unchecked", ("just_this_one",)),
        )
    )
    assert dialect.spellings("^solana_program::program::invoke_signed_unchecked$") == (
        "^just_this_one$",
    )
    assert dialect.spellings("^solana_program::program::invoke$") == ("^wholesale::invoke$",)


# ---------------------------------------------------------------------------------------------
# the blanket


def test_the_namespace_blanket_widens_to_the_whole_family(split: PathDialect) -> None:
    """The single most consequential directive in the files: ``^solana_program::.*$`` sets the
    never-inline *default* for the platform layer, and on a real post-split target it matched two
    symbols. The widened form has to cover both the split crates and the monolith that remains."""
    import re

    (widened,) = split.spellings("^solana_program::.*$")
    for symbol in (
        "solana_account_info::AccountInfo::lamports",
        "solana_pubkey::Pubkey::find_program_address",
        "solana_cpi::invoke_signed_unchecked",
        "solana_program::program::invoke_signed",
    ):
        assert re.search(widened, symbol), f"{widened} does not cover {symbol}"


def test_the_blanket_is_widened_even_on_a_target_that_predates_the_split(
    monolithic: PathDialect,
) -> None:
    """Unlike a :class:`PathAlias`, the widened blanket is a *superset* of what it replaces, so it
    is correct on either generation and needs no version condition. That is the whole reason it is
    expressed as a pattern over crate names rather than as a list of crates."""
    import re

    (widened,) = monolithic.spellings("^solana_program::.*$")
    assert re.search(widened, "solana_program::program::invoke_signed")


def test_a_path_that_merely_starts_with_the_split_crate_is_not_widened(
    split: PathDialect,
) -> None:
    """``^solana_program::instruction::get_stack_height$`` names one function; widening it would
    point a directive at symbols that do not exist. The literal ``.*`` is what tells the two
    apart."""
    pattern = "^solana_program::instruction::get_stack_height$"
    assert split.spellings(pattern) == (pattern,)


# ---------------------------------------------------------------------------------------------
# what a target that predates the split gets


def test_a_target_without_the_split_crates_keeps_the_canonical_spelling(
    monolithic: PathDialect,
) -> None:
    """An alias names a crate; if the target does not resolve it, the alias is not merely useless
    but wrong — on 1.18 the canonical path is the real one. Dropping unresolvable spellings is what
    lets the aliases be declared unconditionally against the newest generation."""
    for pattern in (
        "^solana_program::account_info::AccountInfo::lamports$",
        "^solana_program::pubkey::Pubkey::find_program_address$",
        "^solana_program::rent::Rent::minimum_balance$",
    ):
        assert monolithic.spellings(pattern) == (pattern,)


def test_a_chain_with_no_split_gets_a_dialect_that_changes_nothing() -> None:
    dialect = dialect_for(_workspace("soroban-sdk"), SOROBAN)
    assert dialect.aliases == ()
    assert dialect.spellings("^soroban_sdk::Env::storage$") == ("^soroban_sdk::Env::storage$",)


# ---------------------------------------------------------------------------------------------
# rendering a whole file


def test_rendering_preserves_comments_blank_lines_and_attributes(split: PathDialect) -> None:
    source = (
        "; a comment\n"
        "\n"
        "#[inline(never)] ^core::.*$\n"
        "#[inline] ^solana_program::account_info::AccountInfo::lamports$\n"
    )
    assert split.render(source) == (
        "; a comment\n"
        "\n"
        "#[inline(never)] ^core::.*$\n"
        "#[inline] ^solana_account_info::AccountInfo::lamports$\n"
    )


def test_a_fanned_out_summary_carries_its_whole_annotation_block(split: PathDialect) -> None:
    """A points-to summary's ``#[type(...)]`` lines *precede* its pattern, so a second spelling that
    did not repeat them would be a pattern with no summary attached — silently no longer a
    summary."""
    source = (
        ";; a summary\n"
        "#[type((*i32)(r1+0):num)]\n"
        "^solana_program::program::invoke_signed_unchecked$\n"
    )
    assert split.render(source) == (
        ";; a summary\n"
        "#[type((*i32)(r1+0):num)]\n"
        "^solana_program::program::invoke_signed_unchecked$\n"
        "\n"
        "#[type((*i32)(r1+0):num)]\n"
        "^solana_cpi::invoke_signed_unchecked$\n"
    )


def test_rendering_twice_is_not_claimed_and_is_not_reachable(split: PathDialect) -> None:
    """Rendering rendered output is *not* the identity, by construction: the alias that covers a
    symbol living on both sides of the split names the canonical spelling among its own
    replacements, so a second pass fans that copy out again.

    That is safe only because it cannot happen. Every caller renders from the vendored original —
    :func:`canonical_env` re-reads ``envs/`` on each call — so this pins the reachability rather than
    pretending the function is idempotent, and it fails loudly if a caller ever starts feeding
    rendered text back in.
    """
    summaries = canonical_env("cvlr_summaries_core.txt", split)
    assert summaries.count("^solana_cpi::invoke_signed_unchecked$") == 1
    assert split.render(summaries).count("^solana_cpi::invoke_signed_unchecked$") == 2


def test_recomposing_a_composite_is_stable(split: PathDialect) -> None:
    """The invariant that makes the above a non-issue: the composite is built from the vendored
    layers every time, so composing twice — which the authoring loop does whenever it adds a
    package-layer directive — gives the same file."""
    once = compose_env(INLINING, package_layer="; mine\n", dialect=split)
    assert compose_env(INLINING, package_layer="; mine\n", dialect=split) == once


def test_the_vendored_files_are_returned_verbatim_without_a_dialect() -> None:
    """The refresh script's contract: ``envs/`` is a *copy* of upstream, so a caller that asks for a
    canonical file with no dialect must get the bytes that were vendored. Anything else and the
    next refresh reports a diff that is ours, not upstream's."""
    for name in CANONICAL_ENVS:
        assert canonical_env(name) == (ENV_DIR / name).read_text()


def test_an_empty_dialect_is_the_identity() -> None:
    for name in CANONICAL_ENVS:
        assert PathDialect().render((ENV_DIR / name).read_text()) == (ENV_DIR / name).read_text()


# ---------------------------------------------------------------------------------------------
# keeping the aliases honest across an upstream refresh


def test_every_declared_alias_still_names_something_in_the_vendored_files() -> None:
    """An alias is written against a file upstream owns. After a refresh, one whose canonical path
    no longer appears anywhere is dead weight that reads like coverage — and the failure mode it is
    supposed to prevent is itself silent, so nothing else would notice."""
    vendored = "\n".join((ENV_DIR / name).read_text() for name in CANONICAL_ENVS)
    for alias in SOLANA.platform.path_aliases:
        canonical = alias.canonical
        assert canonical in vendored, (
            f"{canonical} is aliased but appears in none of the vendored tuning files; either "
            f"upstream removed the directive or the alias was written against a stale file"
        )


def test_the_namespace_blanket_is_declared_for_a_directive_that_exists() -> None:
    """Specifically the blanket, because it is the one whose canonical spelling contains a regex
    fragment: an upstream reword from ``^solana_program::.*$`` to anything else leaves it matching
    nothing, and the platform layer loses its default with no other symptom."""
    blankets = [
        a for a in SOLANA.platform.path_aliases if isinstance(a, NamespacePattern)
    ]
    assert blankets, "the platform layer's never-inline default is no longer widened"
    core = (ENV_DIR / "cvlr_inlining_core.txt").read_text()
    for blanket in blankets:
        assert f"#[inline(never)] ^{blanket.canonical}$" in core


# ---------------------------------------------------------------------------------------------
# pinned against a real binary


#: Demangled symbols from a real Anchor program built for SBF — the evidence the alias table was
#: derived from. Checked in because it is the only thing that can catch an alias being *wrong*: every
#: other test here can only catch one being stale, and a wrong alias is just as silent.
SYMBOLS_FIXTURE = Path(__file__).parent / "data" / "vault_sbf_symbols.txt"


def _measured_symbols() -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in SYMBOLS_FIXTURE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


def test_every_alias_rewrites_to_a_path_the_binary_actually_defines(split: PathDialect) -> None:
    """The alias table was read off this symbol table, so this is the test that keeps the two
    honest. ``solana-program`` is a partial facade — some symbols moved and some did not — and
    deciding per *module* rather than per symbol got the answer wrong twice before this existed.

    Aliases are skipped only when the fixture has no symbol under either spelling, which means the
    program does not exercise that concept; those are pinned by the vendored-file test above.
    """
    symbols = _measured_symbols()
    unexercised: list[str] = []
    for alias in SOLANA.platform.path_aliases:
        if isinstance(alias, NamespacePattern):
            continue
        spellings = (alias.canonical, *alias.actual)
        if not any(sp in s for s in symbols for sp in spellings):
            unexercised.append(alias.canonical)
            continue
        assert any(a in s for s in symbols for a in alias.actual), (
            f"{alias.canonical} is exercised by the binary but none of its aliases "
            f"{alias.actual} names a path it defines — the alias names the wrong crate"
        )
    # Stated rather than asserted away: these are the concepts a lamports vault does not touch, and
    # the list changing is a signal about the fixture, not a failure.
    assert unexercised == [
        "solana_program::program_pack",
        "solana_program::clock",
        "solana_program::hash",
        "solana_program::system_program",
        "solana_program::incinerator",
    ]


def test_the_dialect_measurably_restores_coverage_and_costs_none(split: PathDialect) -> None:
    """The bug and the fix, as numbers against a real binary.

    Two counts, because each misses what the other catches. *Directives that match something*
    catches a revived directive whose effect is invisible in symbol coverage — the Anchor error
    conversion is already inside the blanket ``^.*anchor_lang.*$``, so reviving its ``#[inline]``
    changes that symbol's treatment without changing whether anything reaches it. *Symbols reached*
    catches the reverse: a rewrite that doubled a directive without addressing any more of the
    binary.

    The regression half is the important one. A rewrite that revived fifteen directives while
    quietly orphaning one symbol would still be a bad trade, and only the symbol set can see it.
    """
    import re

    symbols = _measured_symbols()

    def stats(text: str) -> tuple[int, set[str]]:
        live, reached = 0, set()
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith(";") or s.startswith("#[type("):
                continue
            pattern = re.sub(r"^#\[inline(?:\(never\))?\]\s*", "", s).strip()
            hits = {x for x in symbols if re.search(pattern, x)}
            if hits:
                live += 1
                reached |= hits
        return live, reached

    for name, directives, coverage in (
        ("cvlr_inlining_core.txt", (4, 15), (6, 35)),
        ("cvlr_inlining_anchor.txt", (6, 7), (26, 26)),
        ("cvlr_summaries_core.txt", (2, 6), (2, 4)),
    ):
        (lb, cb), (la, ca) = stats(canonical_env(name)), stats(canonical_env(name, split))
        assert (lb, la) == directives, f"{name} directives: {lb} -> {la}"
        assert (len(cb), len(ca)) == coverage, f"{name} symbols: {len(cb)} -> {len(ca)}"
        assert cb <= ca, f"{name} lost coverage of {sorted(cb - ca)}"
