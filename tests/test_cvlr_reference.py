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
    assert 'cvlr = "=0.6.4"' in block
    assert 'cvlr-solana = "=0.5.2"' in block
    assert 'solana-program = "2.2"' in block


def test_a_target_on_fixed_is_given_the_narrow_fixed_point_type_and_another_is_not():
    # cvlr-fixed's `fixed` feature compiles the `fixed` crate, so a target that never uses it would
    # be made to build one only to be offered a type it has no use for.
    on_fixed = ref.SOLANA_V2.for_target(lambda name: name == "fixed")
    core = ref.SOLANA_V2.core
    narrow = ref.CrateRelease("cvlr-fixed", core.version, commit=core.commit, features=("fixed",))
    assert narrow in on_fixed.scaffold_crates()
    assert on_fixed.for_target(lambda name: name == "fixed") == on_fixed
    assert ref.SOLANA_V2.for_target(lambda _: False) is ref.SOLANA_V2
    assert narrow not in ref.SOLANA_V2.crates()


def test_a_crate_with_features_is_declared_with_them():
    narrow = ref.CrateRelease("cvlr-fixed", "0.6.4", features=("fixed",))
    assert narrow.dependency_line() == 'cvlr-fixed = { version = "=0.6.4", features = ["fixed"] }'
    # The API corpus documents what a target on `fixed` is offered, so its probe declares it.
    assert narrow.dependency_line() in ref.SOLANA_V2.cargo_dependencies()


def test_the_solana_choice_records_the_platform_it_implies():
    # cvlr-solana 0.5.x requires solana-program 2.2, and each Solana generation has its own
    # AccountInfo. Changing the chain crate without changing this label pairs the wrong types.
    assert ref.SOLANA_V2.binding == ref.CrateRelease("cvlr-solana", "0.5.2")
    assert "2.x" in ref.SOLANA_V2.platform.label


def test_the_spl_token_model_is_part_of_the_reference_set():
    # Pinned with the chain crate, at the same version. Left off this list, the scaffold does not
    # pin it.
    (token,) = [m for m in ref.SOLANA_V2.models if m.program == "the SPL token program"]
    assert token.release == ref.CrateRelease("cvlr-spl-token", "0.5.2")
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
    assert ref.CrateRelease("cvlr-soroban-derive", "0.4.0") in ref.SOROBAN.crates()
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
        assert (REPO_ROOT / "composer" / "templates" / r.shim.author_section).is_file()


def test_the_pinocchio_lines_share_a_module_and_differ_in_its_template():
    """A rule names `crate::certora::pinocchio` whatever the release. What changes between lines is
    the account header the module describes, and the account type the author is told about."""
    lines = (
        ref.SOLANA_PINOCCHIO_0_11,
        ref.SOLANA_PINOCCHIO_0_10,
        ref.SOLANA_PINOCCHIO_0_9,
        ref.SOLANA_PINOCCHIO_0_8,
    )
    shims = [r.shim for r in lines]
    assert all(s is not None and s.module == "pinocchio" and s.requires == "pinocchio" for s in shims)
    assert len({s.template for s in shims if s is not None}) == 4
    assert [s.author_section for s in shims if s is not None] == [
        ref.AuthorSection.PINOCCHIO_ACCOUNT_VIEW,
        ref.AuthorSection.PINOCCHIO_ACCOUNT_VIEW,
        ref.AuthorSection.PINOCCHIO_ACCOUNT_INFO,
        ref.AuthorSection.PINOCCHIO_ACCOUNT_INFO,
    ]
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


def test_the_1_18_line_shares_the_core_and_has_no_program_models():
    """`cvlr-solana` 0.4.x requires `cvlr-*` `>=0.4, <1`, so it resolves onto the one 0.6.4 core.
    The 0.4 line has no stake model and no separate token crate, so there is nothing to withhold."""
    v1 = ref.SOLANA_V1_18
    assert v1.core == ref.SOLANA_V2.core
    assert (v1.binding.name, v1.binding.version) == ("cvlr-solana", "0.4.7")
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
    """The chain crate and its program models are one release. A stake model from another
    version was never published with this chain crate."""
    v3 = ref.SOLANA_V3
    assert {(c.name, c.version) for c in (v3.binding, *(m.release for m in v3.models))} == {
        ("cvlr-solana", "0.6.1"), ("cvlr-solana-stake", "0.6.1"), ("cvlr-spl-token", "0.6.1")
    }


def test_a_commit_patches_the_crates_of_its_repository_the_graph_also_reaches():
    """A chain crate on crates.io depends on the core's own crates directly. Patching only the core
    to a commit leaves a second, crates.io copy of each of those in the graph, and two copies of a
    trait the harness implements and the chain crate calls."""
    commit = ref.GitCommit("https://x", "abc", family=("cvlr-mathint", "cvlr-nondet"))
    core = ref.CrateRelease("cvlr", "0.6.4", commit=commit)
    fixed = ref.CrateRelease("cvlr-fixed", "0.6.4", commit=commit, features=("fixed",))
    patches = ref.commit_patches([core, fixed, ref.CrateRelease("cvlr-solana", "0.5.2")])
    assert [p.name for p in patches] == ["cvlr", "cvlr-mathint", "cvlr-nondet", "cvlr-fixed"]
    assert all(p.commit == commit and p.version == "0.6.4" for p in patches)
