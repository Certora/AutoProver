"""CVLR metadata and the prover conf, with no toolchain, network, or LLM.

Nothing here shells out to cargo or submits a job. What is checked is the parse of
``cargo metadata``, the conf the prover settings render to, and the keys one submission adds.
"""

import json
from pathlib import Path

from composer.cargo.metadata import parse_metadata
from composer.prover import conf as prover_conf
from composer.spec.cvlr import conf as cvlr_conf
from composer.spec.cvlr.crates import resolve
from composer.spec.cvlr_reference import SOLANA


# --------------------------------------------------------------------------------------------
# cargo metadata
# --------------------------------------------------------------------------------------------

#: A two-member workspace with one published dependency, in cargo's own shape. Hand-written rather
#: than recorded so that every field this codebase reads is visible in the test that reads it.
_METADATA = {
    "workspace_root": "/w",
    "target_directory": "/w/target",
    "workspace_members": ["path+file:///w/programs/lend#0.1.0", "path+file:///w#0.1.0"],
    "packages": [
        {
            "id": "path+file:///w/programs/lend#0.1.0",
            "name": "example-lending",
            "version": "0.1.0",
            "manifest_path": "/w/programs/lend/Cargo.toml",
            "source": None,
            "features": {"certora": [], "no-entrypoint": []},
            "targets": [
                {
                    "name": "example_lending",
                    "kind": ["cdylib"],
                    "crate_types": ["cdylib"],
                    "src_path": "/w/programs/lending/src/lib.rs",
                },
                {
                    "name": "bench",
                    "kind": ["bench"],
                    "crate_types": ["bin"],
                    "src_path": "/w/programs/lending/benches/b.rs",
                },
            ],
        },
        {
            "id": "path+file:///w#0.1.0",
            "name": "workspace-root-crate",
            "version": "0.1.0",
            "manifest_path": "/w/Cargo.toml",
            "source": None,
            "features": {},
            "targets": [{"name": "root", "kind": ["lib"], "crate_types": ["lib"], "src_path": "/w/src/lib.rs"}],
        },
        {
            "id": "registry+https://github.com/rust-lang/crates.io-index#cvlr@0.6.1",
            "name": "cvlr",
            "version": "0.6.1",
            "manifest_path": "/home/u/.cargo/registry/src/idx/cvlr-0.6.1/Cargo.toml",
            "source": "registry+https://github.com/rust-lang/crates.io-index",
            "features": {},
            "targets": [{"name": "cvlr", "kind": ["lib"], "crate_types": ["lib"], "src_path": "/reg/cvlr/src/lib.rs"}],
        },
        {
            "id": "registry+https://github.com/rust-lang/crates.io-index#cvlr-log@0.6.1",
            "name": "cvlr-log",
            "version": "0.6.1",
            "manifest_path": "/home/u/.cargo/registry/src/idx/cvlr-log-0.6.1/Cargo.toml",
            "source": "registry+https://github.com/rust-lang/crates.io-index",
            "features": {},
            "targets": [{"name": "cvlr_log", "kind": ["lib"], "crate_types": ["lib"], "src_path": "/reg/cvlr-log/src/lib.rs"}],
        },
    ],
}


def test_the_lib_target_is_the_one_an_artifact_is_named_after():
    """A package's bench and bin targets are not what a verification build produces."""
    lend = parse_metadata(_METADATA).member("example-lending")
    assert lend is not None
    assert lend.lib is not None
    assert lend.lib.name == "example_lending"
    assert lend.lib.artifact_stem == "example_lending"


def test_a_dash_in_a_lib_name_becomes_an_underscore_in_the_artifact():
    """Cargo names the file after the target with ``-`` normalized, and the ``.so`` path in the
    build manifest follows that, not the package name."""
    payload = json.loads(json.dumps(_METADATA))
    payload["packages"][0]["targets"][0]["name"] = "example-lending"
    lend = parse_metadata(payload).member("example-lending")
    assert lend is not None and lend.lib is not None
    assert lend.lib.artifact_stem == "example_lending"


def test_the_owning_crate_is_the_deepest_one_containing_the_file():
    """A workspace whose root is itself a package contains every nested crate's files too, so the
    shallow match is always available and always wrong."""
    workspace = parse_metadata(_METADATA)
    owner = workspace.owning(Path("/w/programs/lend/src/lib.rs"))
    assert owner is not None and owner.name == "example-lending"


def test_a_file_in_no_member_has_no_owning_crate():
    workspace = parse_metadata(_METADATA)
    assert workspace.owning(Path("/elsewhere/src/lib.rs")) is None


def test_a_crate_family_is_recognized_by_name():
    """``cvlr`` does not declare its family anywhere; the helper crates come in as ordinary
    dependencies, and an agent asking what a macro expands to needs all of them."""
    workspace = parse_metadata(_METADATA)
    assert [c.name for c in workspace.family("cvlr")] == ["cvlr", "cvlr-log"]


def test_a_published_dependency_is_distinguished_from_a_workspace_member():
    workspace = parse_metadata(_METADATA)
    cvlr = workspace.resolved("cvlr")
    lend = workspace.resolved("example-lending")
    assert cvlr is not None and not cvlr.is_local
    assert lend is not None and lend.is_local


# --------------------------------------------------------------------------------------------
# CVLR source resolution
# --------------------------------------------------------------------------------------------


def test_the_cvlr_source_roots_are_the_crate_directories_the_build_resolved():
    sources = resolve(parse_metadata(_METADATA))
    assert sources.core is not None and sources.core.version == "0.6.1"
    assert sources.roots() == (
        Path("/home/u/.cargo/registry/src/idx/cvlr-0.6.1"),
        Path("/home/u/.cargo/registry/src/idx/cvlr-log-0.6.1"),
    )


def test_a_project_on_the_reference_core_but_without_the_chain_crate_reports_that_gap():
    """Two different statements, and the corpus's advice depends on which one holds: an old
    ``cvlr-solana`` and no ``cvlr-solana`` at all."""
    gaps = {g.crate: g for g in resolve(parse_metadata(_METADATA)).gaps(SOLANA)}
    assert "cvlr" not in gaps, "the fixture pins the reference core, so it is not a gap"
    assert gaps["cvlr-solana"].resolved is None
    assert "is not a dependency of this project" in gaps["cvlr-solana"].describe()


def test_an_older_cvlr_than_the_corpus_was_written_against_is_reported():
    payload = json.loads(json.dumps(_METADATA))
    payload["packages"][2]["version"] = "0.4.1"
    gaps = {g.crate: g for g in resolve(parse_metadata(payload)).gaps(SOLANA)}
    assert gaps["cvlr"].resolved == "0.4.1"
    assert gaps["cvlr"].reference == "0.6.1"


# --------------------------------------------------------------------------------------------
# the conf
# --------------------------------------------------------------------------------------------


def test_loops_are_bounded_soundly_and_the_bound_is_raised_instead():
    """``optimistic_loop`` assumes loops halt instead of proving it, so a violation that needs
    more iterations is not found. Bound the inputs that set the trip count, or edit the loop,
    before raising ``loop_iter``."""
    conf = cvlr_conf.settings_conf(cvlr_conf.ProverSettings())
    assert conf["optimistic_loop"] is False
    assert conf["loop_iter"] == "2"


def test_the_base_enables_no_optimistic_solana_flags():
    """None of the ``-solanaOptimistic*`` flags. They are unsound, and they do not fix the [3308]
    they were meant to."""
    assert not [f for f in cvlr_conf.BASE_PROVER_ARGS if f.startswith("-solanaOptimistic")]


def test_every_conf_checks_vacuity():
    """With ``rule_sanity`` off, a [3308] inside the generated vacuity rule is reported as
    verified. No setting turns it off."""
    for settings in (
        cvlr_conf.ProverSettings(),
        cvlr_conf.ProverSettings(loop_iter=5, optimistic_loop=True, solver_portfolio=True),
    ):
        assert cvlr_conf.settings_conf(settings)["rule_sanity"] == "basic"


def test_the_loop_bound_is_written_the_way_a_conf_spells_an_integer():
    assert cvlr_conf.settings_conf(cvlr_conf.ProverSettings(loop_iter=4))["loop_iter"] == "4"


# ---------------------------------------------------------------------------------------------
# The solver portfolio
#
# It changes how long the prover spends, not what a verified result means. The portfolio is one
# recipe. ``-solanaTACSoundSignedMath`` next to ``-solanaTACMathInt`` turned an eighteen-rule run
# from 6.7 minutes, all verified, into a two-hour timeout with thirteen rules unverified.


def test_the_portfolio_is_the_recipe_the_corpus_uses_and_only_sound_flags():
    flags = cvlr_conf.NONLINEAR_SOLVER_PORTFOLIO
    assert any(f.startswith("-backendStrategy") for f in flags)
    assert "-smt_useNIA true" in flags and "-smt_useLIA true" in flags
    assert sum(f.startswith("-solvers ") for f in flags) >= 3
    # Nothing that changes what a verdict means may be smuggled in here.
    banned = ("optimistic_loop", "-solanaOptimistic", "rule_sanity", "-solanaTACSoundSignedMath")
    assert not [f for f in flags for b in banned if b in f]


def test_the_portfolio_adds_its_flags_to_the_base_and_turning_it_off_removes_them():
    """``-solvers`` repeats: each entry adds a solver configuration, so all four lines must reach
    the conf for all twelve instances to run."""
    on = cvlr_conf.settings_conf(cvlr_conf.ProverSettings(solver_portfolio=True))
    assert on["prover_args"] == [
        *cvlr_conf.BASE_PROVER_ARGS, *cvlr_conf.NONLINEAR_SOLVER_PORTFOLIO
    ]
    off = cvlr_conf.settings_conf(cvlr_conf.ProverSettings(solver_portfolio=False))
    assert off["prover_args"] == list(cvlr_conf.BASE_PROVER_ARGS)


def test_a_conf_change_invalidates_a_stamp_earned_before_it():
    """A verdict under one loop bound or one solver portfolio is not a verdict under another."""
    default = cvlr_conf.conf_history(cvlr_conf.ProverSettings())
    assert default != cvlr_conf.conf_history(cvlr_conf.ProverSettings(loop_iter=3))
    assert default != cvlr_conf.conf_history(cvlr_conf.ProverSettings(solver_portfolio=True))
    assert default == cvlr_conf.conf_history(cvlr_conf.ProverSettings())


# ---------------------------------------------------------------------------------------------
# One submission's keys


def _submission(**kwargs) -> dict:
    return cvlr_conf.solana_conf(
        cvlr_conf.ProverSettings(),
        cvlr_conf.RunOverlay(build_script=Path("/w/.certora_build/confined_build.py"), **kwargs),
    )


def test_the_run_names_the_build_script():
    assert _submission()["build_script"] == "/w/.certora_build/confined_build.py"


def test_the_message_is_reduced_to_what_the_prover_accepts():
    assert _submission(msg="Deposit & Balance")["msg"] == "Deposit Balance"


def test_selecting_rules_names_them():
    assert _submission(rules=prover_conf.SelectRules(("rule_vacuous",)))["rule"] == ["rule_vacuous"]


def test_inheriting_rules_checks_every_rule():
    assert "rule" not in _submission()


def test_the_env_files_are_left_for_the_build_manifest_to_supply():
    """``cargo certora-sbf`` reads them from ``[package.metadata.certora]``, and the prover
    applies that only when the conf has none."""
    conf = _submission()
    assert "solana_inlining" not in conf and "solana_summaries" not in conf


def test_a_units_summary_file_is_named_when_the_run_passes_one():
    conf = _submission(summaries=(Path("envs/cvlr_summaries_vault.txt"),))
    assert conf["solana_summaries"] == ["envs/cvlr_summaries_vault.txt"]
