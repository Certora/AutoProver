"""The CVLR reference set (``composer.spec.cvlr.reference``).

Compiling a probe crate per chain is what checks that the versions resolve. That needs cargo and
a network, so it is not run here. These tests cover what a wrong edit can break without cargo
noticing: the chain names matching the pipeline's, exact pinning, and the platform generation
that travels with the chain crate.
"""

from pathlib import Path

import pytest

from composer.spec.cvlr import reference as ref

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    # was never compiled. A generation bound by a harness module declares none: its helpers are
    # the module, compiled against the program's own copy of the framework, so there is no chain
    # crate a probe could build against, whatever the framework publishes.
    for chain, generations in ref.REFERENCE_SET.items():
        for r in generations:
            assert r.platform.sdk_crates or r.shim is not None, chain
            for crate in r.platform.sdk_crates:
                assert crate.dependency_line() == f'{crate.name} = "{crate.line}"'
                assert "=" not in crate.line


def test_the_dependency_block_carries_the_platform_too():
    # Without it a probe cannot name AccountInfo, so an entry that mentions one would fail the
    # gate for a missing import rather than for anything about CVLR.
    block = ref.SOLANA_V2.cargo_dependencies()
    assert 'cvlr = "=0.6.2"' in block
    assert 'cvlr-solana = "=0.5.2"' in block
    assert 'solana-program = "2.2"' in block


def test_the_solana_choice_records_the_platform_it_implies():
    # cvlr-solana 0.5.x requires solana-program 2.2, and each Solana generation has its own
    # AccountInfo. Changing the chain crate without changing this label pairs the wrong types.
    assert ref.SOLANA_V2.binding == ref.CrateRelease("cvlr-solana", "0.5.2")
    assert "2.x" in ref.SOLANA_V2.platform.label


def test_the_spl_token_model_is_part_of_the_reference_set():
    # Pinned with the chain crate, at the same version. Left off this list, the scaffold does not
    # pin it.
    assert ref.CrateRelease("cvlr-spl-token", "0.5.2") in ref.SOLANA_V2.specializations
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


def test_every_generation_shares_one_core_release():
    # Every chain and every Solana generation is compiled against this same cvlr.
    assert ref.SOLANA_V1_18.core == ref.SOLANA_V2.core == ref.SOLANA_V3.core == ref.SOROBAN.core


def test_solana_has_one_reference_set_per_generation_framework_first_then_newest_first():
    assert ref.generations("solana") == (
        ref.SOLANA_JIMINY,
        ref.SOLANA_PINOCCHIO_0_11,
        ref.SOLANA_PINOCCHIO_0_10,
        ref.SOLANA_PINOCCHIO_0_9,
        ref.SOLANA_PINOCCHIO_0_8,
        ref.SOLANA_V3,
        ref.SOLANA_V2,
        ref.SOLANA_V1_18,
    )


def test_the_jiminy_line_is_the_core_bound_by_a_harness_module():
    """jiminy is published as git dependencies only, so no CVLR crate can name its types. The
    project is given the core, and the scaffold writes the module that builds jiminy's accounts."""
    jiminy = ref.SOLANA_JIMINY
    assert jiminy.crates() == (jiminy.core,)
    assert jiminy.shim is not None
    assert jiminy.shim.requires == "jiminy-entrypoint"
    assert [w.name for w in jiminy.platform.witnesses] == ["jiminy-entrypoint"]
    assert jiminy.platform.heap_opt_out is None
    assert "harness module" in jiminy.line()


def test_every_harness_module_has_its_template():
    for r in (r for generations in ref.REFERENCE_SET.values() for r in generations):
        if r.shim is None:
            continue
        template = (
            REPO_ROOT / "composer" / "spec" / "cvlr" / "harness_files"
            / f"{r.shim.template}.template.rs"
        )
        assert template.is_file(), template


def test_the_pinocchio_lines_share_a_module_and_differ_in_its_template():
    """A rule names `crate::certora::pinocchio` whatever the release. What changes between lines is
    the account header the module describes."""
    lines = (
        ref.SOLANA_PINOCCHIO_0_11,
        ref.SOLANA_PINOCCHIO_0_10,
        ref.SOLANA_PINOCCHIO_0_9,
        ref.SOLANA_PINOCCHIO_0_8,
    )
    shims = [r.shim for r in lines]
    assert all(s is not None and s.module == "pinocchio" and s.requires == "pinocchio" for s in shims)
    assert len({s.template for s in shims if s is not None}) == 4
    for r in lines:
        assert r.crates() == (r.core,)
        assert {w.name for w in r.platform.witnesses} == {"pinocchio"}
        assert r.platform.heap_opt_out is None


def test_no_pinocchio_release_is_claimed_by_two_lines():
    """The scaffold takes the first line that claims a target's release, so a release listed twice
    would silently never reach the second."""
    claimed = [
        w.line
        for r in ref.generations("solana")
        for w in r.platform.witnesses
        if w.name == "pinocchio"
    ]
    assert sorted(claimed) == ["0.10", "0.11", "0.12", "0.8", "0.9"]


def test_the_sdk_generations_switch_off_the_sdk_allocator():
    for r in (ref.SOLANA_V3, ref.SOLANA_V2, ref.SOLANA_V1_18):
        assert r.platform.heap_opt_out == ref.CUSTOM_HEAP_FEATURE


def test_the_sdk3_line_is_released_whole():
    """The chain crate and its specializations are one release. A stake model from another
    version was never published with this chain crate."""
    v3 = ref.SOLANA_V3
    assert {(c.name, c.version) for c in (v3.binding, *v3.specializations)} == {
        ("cvlr-solana", "0.6.1"), ("cvlr-solana-stake", "0.6.1"), ("cvlr-spl-token", "0.6.1")
    }
