"""The CVLR reference set (``composer.spec.cvlr.reference``).

Compiling a probe crate per chain is what checks that the versions resolve. That needs cargo and
a network, so it is not run here. These tests cover what a wrong edit can break without cargo
noticing: the chain names matching the pipeline's, exact pinning, and the platform generation
that travels with the chain crate.
"""

import pytest

from composer.spec.cvlr import reference as ref


def test_the_chains_are_exactly_the_pipelines_rust_chains():
    """The module repeats the chain names as plain strings so it does not import the pipeline.
    This checks that the repetition still matches. EVM is excluded: CVLR is the Rust-side language."""
    from typing import get_args

    from composer.pipeline.ecosystem import ChainTag

    assert set(ref.REFERENCE_SET) == set(get_args(ChainTag)) - {"evm"}


def test_every_cvlr_crate_is_pinned_to_an_exact_release():
    # A range would let a resolver move the corpus's ground truth without an edit here, and the
    # compile gate would then be testing something nobody chose.
    for chain, generations in ref.REFERENCE_SET.items():
        for crate in (c for r in generations for c in r.crates()):
            assert crate.dependency_line().startswith(f'{crate.name} = "='), (chain, crate)
            assert crate.version[0].isdigit(), (chain, crate)


def test_the_platform_is_a_line_not_a_release():
    # The platform generation is about the target. An exact pin would claim a patch level that
    # was never compiled.
    for chain, generations in ref.REFERENCE_SET.items():
        for r in generations:
            assert r.platform.sdk_crates, chain
            for crate in r.platform.sdk_crates:
                assert crate.dependency_line() == f'{crate.name} = "{crate.line}"'
                assert "=" not in crate.line


def test_the_dependency_block_carries_the_platform_too():
    # Without it a probe cannot name AccountInfo, so an entry that mentions one would fail the
    # gate for a missing import rather than for anything about CVLR.
    block = ref.SOLANA_V2.cargo_dependencies()
    assert 'cvlr = "=0.6.2"' in block
    assert 'cvlr-solana = "=0.5.1"' in block
    assert 'solana-program = "2.2"' in block


def test_the_solana_choice_records_the_platform_it_implies():
    # cvlr-solana 0.5.x requires solana-program 2.2, and each Solana generation has its own
    # AccountInfo. Changing the chain crate without changing this label pairs the wrong types.
    assert ref.SOLANA_V2.chain_crate == ref.CrateRelease("cvlr-solana", "0.5.1")
    assert "2.x" in ref.SOLANA_V2.platform.label


def test_the_spl_token_model_is_part_of_the_reference_set():
    # Released with the chain crate it was split from, at its version. Leaving it off the
    # reference set would keep the scaffold from pinning it.
    assert ref.CrateRelease("cvlr-spl-token", "0.5.1") in ref.SOLANA_V2.specializations
    assert ref.SOLANA_V2.unpublished == ()


def test_a_fresh_project_is_pinned_the_whole_reference_set():
    # The scaffold is what writes dependencies, and it does not add one later. A specialization
    # left out of this list is a crate the project cannot name.
    assert ref.SOLANA_V2.scaffold_crates() == ref.SOLANA_V2.crates()
    assert {c.name for c in ref.SOLANA_V2.scaffold_crates()} == {
        "cvlr", "cvlr-solana", "cvlr-solana-stake", "cvlr-spl-token",
    }


def test_an_unknown_chain_raises_and_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="no CVLR reference set for chain 'evm'") as e:
        ref.generations("evm")
    assert "'solana'" in str(e.value) and "'soroban'" in str(e.value)


def test_both_chains_share_one_core_release():
    # The core line is chain-independent; two chains drifting apart on it would mean one of them
    # is being compiled against a cvlr nobody chose.
    assert ref.SOLANA_V2.core == ref.SOROBAN.core


def _from_commits(reference: ref.ChainReference, repo: str) -> list[ref.CommitPatch]:
    return [p for p in reference.from_commits() if p.commit.repo == repo]


_CVLR_SOLANA_REPO = "https://github.com/Certora/cvlr-solana.git"


def test_the_crates_of_one_repository_are_read_from_one_commit():
    """``cvlr-solana`` releases its three crates together. Patching them from two commits would
    build a chain crate and a model that were never released together."""
    for reference in ref.generations("solana"):
        assert len({p.commit for p in _from_commits(reference, _CVLR_SOLANA_REPO)}) <= 1


def test_a_commit_is_pinned_by_hash_and_never_by_a_name_that_can_move():
    for patch in (p for r in ref.generations("solana") for p in r.from_commits()):
        commit = patch.commit
        assert len(commit.rev) == 40 and all(ch in "0123456789abcdef" for ch in commit.rev)


def test_solana_has_one_reference_set_per_generation_newest_first():
    assert ref.generations("solana") == (ref.SOLANA_V3, ref.SOLANA_V2, ref.SOLANA_V1_18)


def test_the_1_18_line_shares_the_core_and_has_no_specializations():
    """`cvlr-solana` 0.4.x requires `cvlr-*` `>=0.4, <1`, so it resolves onto the one 0.6.2 core.
    The 0.4 line has no stake model and no separate token crate."""
    v1 = ref.SOLANA_V1_18
    assert v1.core == ref.SOLANA_V2.core
    assert (v1.chain_crate.name, v1.chain_crate.version) == ("cvlr-solana", "0.4.6")
    assert v1.specializations == ()
    assert {c.name for c in v1.scaffold_crates()} == {"cvlr", "cvlr-solana"}
    assert 'solana-program = "1.18"' in v1.cargo_dependencies()
    assert v1.platform.path_aliases == ()


def test_the_sdk3_line_is_released_whole():
    """Chain crate and specializations together, at one version, and on the same core as 2.x."""
    v3 = ref.SOLANA_V3
    assert {(c.name, c.version) for c in (v3.chain_crate, *v3.specializations)} == {
        ("cvlr-solana", "0.6.0"), ("cvlr-solana-stake", "0.6.0"), ("cvlr-spl-token", "0.6.0")
    }
    assert v3.core == ref.SOLANA_V2.core
    assert [w.name for w in v3.platform.witnesses] == ["solana-account-info"]
