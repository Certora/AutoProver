"""The CVLR reference set (``composer.spec.cvlr_reference``).

Compiling a probe crate per chain is what checks that the versions resolve. That needs cargo and
a network, so it is not run here. These tests cover what a wrong edit can break without cargo
noticing: the chain names matching the pipeline's, exact pinning, and the platform generation
that travels with the chain crate.
"""

import pytest

from composer.spec import cvlr_reference as ref


def test_the_chains_are_exactly_the_pipelines_rust_chains():
    """The module repeats the chain names as plain strings so it does not import the pipeline.
    This checks that the repetition still matches. EVM is excluded: CVLR is the Rust-side language."""
    from typing import get_args

    from composer.pipeline.ecosystem import ChainTag

    assert set(ref.REFERENCE_SET) == set(get_args(ChainTag)) - {"evm"}


def test_every_cvlr_crate_is_pinned_to_an_exact_release():
    # A range would let a resolver move the corpus's ground truth without an edit here, and the
    # compile gate would then be testing something nobody chose.
    for chain, r in ref.REFERENCE_SET.items():
        for crate in r.crates():
            assert crate.dependency_line().startswith(f'{crate.name} = "='), (chain, crate)
            assert crate.version[0].isdigit(), (chain, crate)


def test_the_platform_is_a_line_not_a_release():
    # The platform generation is about the target. An exact pin would claim a patch level that
    # was never compiled.
    for chain, r in ref.REFERENCE_SET.items():
        assert r.platform.crates, chain
        for crate in r.platform.crates:
            assert crate.dependency_line() == f'{crate.name} = "{crate.line}"'
            assert "=" not in crate.line


def test_the_dependency_block_carries_the_platform_too():
    # Without it a probe cannot name AccountInfo, so an entry that mentions one would fail the
    # gate for a missing import rather than for anything about CVLR.
    block = ref.SOLANA.cargo_dependencies()
    assert 'cvlr = "=0.6.1"' in block
    assert 'cvlr-solana = "=0.5.0"' in block
    assert 'solana-program = "2.2"' in block


def test_the_solana_choice_records_the_platform_it_implies():
    # cvlr-solana 0.5.0 requires solana-program 2.2, and each Solana generation has its own
    # AccountInfo. Changing the chain crate without changing this label pairs the wrong types.
    assert ref.SOLANA.chain == ref.CrateRelease("cvlr-solana", "0.5.0")
    assert "2.x" in ref.SOLANA.platform.label


def test_the_spl_token_model_is_part_of_the_reference_set():
    # On crates.io at 0.5.0, the same version as the chain crate it was split from. Leaving it
    # off the reference set would keep the scaffold from pinning it.
    assert ref.CrateRelease("cvlr-spl-token", "0.5.0") in ref.SOLANA.specializations
    assert ref.SOLANA.unpublished == ()


def test_a_fresh_project_is_pinned_the_whole_reference_set():
    # The scaffold is what writes dependencies, and it does not add one later. A specialization
    # left out of this list is a crate the project cannot name.
    assert ref.SOLANA.scaffold_crates() == ref.SOLANA.crates()
    assert {c.name for c in ref.SOLANA.scaffold_crates()} == {
        "cvlr", "cvlr-solana", "cvlr-solana-stake", "cvlr-spl-token",
    }


def test_an_unknown_chain_raises_and_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="no CVLR reference set for chain 'evm'") as e:
        ref.reference_for("evm")
    assert "'solana'" in str(e.value) and "'soroban'" in str(e.value)


def test_both_chains_share_one_core_release():
    # The core line is chain-independent; two chains drifting apart on it would mean one of them
    # is being compiled against a cvlr nobody chose.
    assert ref.SOLANA.core == ref.SOROBAN.core
