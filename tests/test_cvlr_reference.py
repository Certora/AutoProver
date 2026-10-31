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
    assert (ref.SOLANA_V2.chain_crate.name, ref.SOLANA_V2.chain_crate.version) == ("cvlr-solana", "0.5.1")
    assert "2.x" in ref.SOLANA_V2.platform.label


def test_the_spl_token_model_is_part_of_the_reference_set():
    # Released with the chain crate it was split from, at its version. Leaving it off the
    # reference set would keep the scaffold from pinning it.
    (token,) = [m for m in ref.SOLANA_V2.models if m.program == "the SPL token program"]
    assert (token.release.name, token.release.version) == ("cvlr-spl-token", "0.5.1")
    assert ref.SOLANA_V2.unpublished == ()


def test_a_fresh_project_is_pinned_the_whole_reference_set():
    # The scaffold is what writes dependencies, and it does not add one later. A crate left out
    # of this list is one the project cannot name.
    assert ref.SOLANA_V2.scaffold_crates() == ref.SOLANA_V2.crates()
    assert {c.name for c in ref.SOLANA_V2.scaffold_crates()} == {
        "cvlr", "cvlr-solana", "cvlr-solana-stake", "cvlr-spl-token",
    }


def test_withholding_narrows_what_the_project_pins_and_not_what_the_corpus_was_built_on():
    # The two accessors answer two questions, and this is the case that makes them differ: the
    # target *is* the program cvlr-solana-stake models, so the project must not be given it —
    # while the corpus was still written against it, and gaps() still has to say so.
    narrowed = ref.SOLANA_V2.withholding("cvlr-solana-stake")
    assert {c.name for c in narrowed.scaffold_crates()} == {
        "cvlr", "cvlr-solana", "cvlr-spl-token",
    }
    assert narrowed.crates() == ref.SOLANA_V2.crates()


def test_withholding_leaves_the_reference_set_it_was_called_on_alone():
    # Frozen, and the module-level set is shared by every run in the process.
    ref.SOLANA_V2.withholding("cvlr-solana-stake")
    assert ref.SOLANA_V2.withheld == frozenset()
    assert ref.SOLANA_V2.scaffold_crates() == ref.SOLANA_V2.crates()


def test_only_a_program_model_can_be_withheld():
    # Silently changing nothing is the one outcome a caller cannot tell apart from success, so a
    # typo would leave the model in the project it was meant to be kept out of.
    with pytest.raises(ValueError, match="not program models of cvlr-solana"):
        ref.SOLANA_V2.withholding("cvlr-solana-staek")
    # The chain crate models no program: a project without it has no CVLR at all, and "run
    # without part of CVLR" is not what this setting is for.
    with pytest.raises(ValueError, match="not program models"):
        ref.SOLANA_V2.withholding("cvlr-solana")


def test_a_companion_crate_cannot_be_withheld_though_it_is_pinned_like_a_model():
    # Soroban's derive crate is declared beside the chain crate and pinned the same way, and it
    # models no program — so there is no target it could be an answer key for. The bound is the
    # type, not a list someone has to remember to keep in step.
    assert ("cvlr-soroban-derive", "0.4.0") in {(c.name, c.version) for c in ref.SOROBAN.crates()}
    assert ref.SOROBAN.models == ()
    with pytest.raises(ValueError, match="this chain models: none"):
        ref.SOROBAN.withholding("cvlr-soroban-derive")


def test_the_refusal_names_what_each_model_stands_for():
    # The error has to say what withholding *means*, because the setting is a statement about the
    # target: an operator who reads "the stake program" knows whether it applies to their run.
    with pytest.raises(ValueError, match="cvlr-solana-stake \\(the stake program\\)"):
        ref.SOLANA_V2.withholding("nonsense")


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


def test_the_1_18_line_shares_the_core_and_has_no_program_models():
    """`cvlr-solana` 0.4.x requires `cvlr-*` `>=0.4, <1`, so it resolves onto the one 0.6.2 core.
    The 0.4 line has no stake model and no separate token crate, so there is nothing to withhold."""
    v1 = ref.SOLANA_V1_18
    assert v1.core == ref.SOLANA_V2.core
    assert (v1.chain_crate.name, v1.chain_crate.version) == ("cvlr-solana", "0.4.6")
    assert v1.models == ()
    assert {c.name for c in v1.scaffold_crates()} == {"cvlr", "cvlr-solana"}
    assert 'solana-program = "1.18"' in v1.cargo_dependencies()
    assert v1.platform.path_aliases == ()
    with pytest.raises(ValueError, match="models: none"):
        v1.withholding("cvlr-solana-stake")


def test_only_the_1_18_line_warns_the_author_off_the_manual():
    """The manual is written for the newest line. On 1.18 it names crates that do not exist."""
    assert ref.SOLANA_V3.caveats == ref.SOLANA_V2.caveats == ()
    caveats = " ".join(ref.SOLANA_V1_18.caveats)
    assert "cvlr_spl_token" in caveats and "cvlr_solana::token" in caveats


def test_the_sdk3_line_is_released_whole():
    """Chain crate and models together, at one version, and on the same core as 2.x."""
    v3 = ref.SOLANA_V3
    assert {(c.name, c.version) for c in (v3.chain_crate, *(m.release for m in v3.models))} == {
        ("cvlr-solana", "0.6.0"), ("cvlr-solana-stake", "0.6.0"), ("cvlr-spl-token", "0.6.0")
    }
    assert v3.core == ref.SOLANA_V2.core
    assert [w.name for w in v3.platform.witnesses] == ["solana-account-info"]
