"""The CVLR reference set (``composer.spec.cvlr_reference``).

The data itself is verified by *compiling* it — a probe crate per chain, which is the acceptance
gate in ``docs/cvlr-capture-plan.md`` §9 and needs cargo and a network, so it is not run here.
What these tests hold is everything a wrong edit could break without cargo noticing: the chain
vocabulary matching the pipeline's, exact pinning, and the platform generation travelling with the
chain crate that requires it.
"""

import pytest

from composer.spec import cvlr_reference as ref


def test_the_chains_are_exactly_the_pipelines_rust_chains():
    """The module repeats the chain vocabulary as plain strings to stay import-cheap (its docstring
    says why), so this is the guard that the repetition stays true. EVM is excluded on purpose:
    CVLR is the Rust-side language."""
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
    # The platform generation is a compatibility statement about the *target*; claiming an exact
    # release there would assert a patch level we never compiled and do not care about.
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
    # AccountInfo type — so this pairing is the decision, not an incidental detail. Changing the
    # chain crate without changing this label is the mistake worth catching.
    assert ref.SOLANA.chain == ref.CrateRelease("cvlr-solana", "0.5.0")
    assert "2.x" in ref.SOLANA.platform.label


def test_the_unpublished_spl_token_crate_is_recorded_under_both_names():
    # It was renamed, so a reader searching either name must find the note; silence would read as
    # "no such thing" rather than "deliberately out of scope".
    (spl,) = ref.SOLANA.unpublished
    assert set(spl.names) == {"cvlr-spl-token", "cvlr-solana-token"}
    assert "SPL Token" in spl.missing


def test_an_unknown_chain_raises_and_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="no CVLR reference set for chain 'evm'") as e:
        ref.reference_for("evm")
    assert "'solana'" in str(e.value) and "'soroban'" in str(e.value)


def test_both_chains_share_one_core_release():
    # The core line is chain-independent; two chains drifting apart on it would mean one of them
    # is being compiled against a cvlr nobody chose.
    assert ref.SOLANA.core == ref.SOROBAN.core
