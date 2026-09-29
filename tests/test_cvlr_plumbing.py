"""CVLR metadata and the prover conf, with no toolchain, network, or LLM.

Nothing here shells out to cargo or submits a job. What is checked is the parse of
``cargo metadata`` and ``Cargo.toml``, the conf a tunable conf renders to, and the keys one
submission adds.
"""

import json
import subprocess
from pathlib import Path

import pytest

from composer.cargo.manifest import Dependency, MalformedManifest, parse_manifest
from composer.cargo import metadata
from composer.cargo.metadata import (
    CargoFailed,
    CargoMetadataJson,
    CargoTimedOut,
    GitBranch,
    GitRev,
    GitSource,
    GitTag,
    UnreadableMetadata,
    Workspace,
    parse_metadata,
)
from composer.prover import conf as prover_conf
from composer.spec.cvlr import conf as cvlr_conf
from composer.spec.cvlr.crates import Absent, CvlrSources
from composer.spec.cvlr.reference import SOLANA


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


def _workspace(payload: dict) -> Workspace:
    return parse_metadata(CargoMetadataJson.model_validate(payload))


def test_the_lib_target_is_the_one_an_artifact_is_named_after():
    """A package's bench and bin targets are not what a verification build produces."""
    lend = _workspace(_METADATA).member("example-lending")
    assert lend is not None
    assert lend.lib is not None
    assert lend.lib.name == "example_lending"
    assert lend.lib.artifact_stem == "example_lending"


def test_a_dash_in_a_lib_name_becomes_an_underscore_in_the_artifact():
    """Cargo names the file after the target with ``-`` normalized, and the ``.so`` path in the
    build manifest follows that, not the package name."""
    payload = json.loads(json.dumps(_METADATA))
    payload["packages"][0]["targets"][0]["name"] = "example-lending"
    lend = _workspace(payload).member("example-lending")
    assert lend is not None and lend.lib is not None
    assert lend.lib.artifact_stem == "example_lending"


def test_an_older_cargo_that_reports_only_kind_still_names_the_lib_target():
    payload = json.loads(json.dumps(_METADATA))
    del payload["packages"][0]["targets"][0]["crate_types"]
    payload["packages"][0]["targets"][0]["kind"] = ["cdylib"]
    lend = _workspace(payload).member("example-lending")
    assert lend is not None and lend.lib is not None
    assert lend.lib.builds_shared_object


def test_a_git_source_is_split_into_repository_reference_and_commit():
    source = GitSource.parse(
        "git+https://github.com/Certora/anchor.git?branch=certora-v0.31.1#3ebe7595"
    )
    assert source.repository == "https://github.com/Certora/anchor.git"
    assert source.reference == GitBranch("certora-v0.31.1")
    assert source.commit == "3ebe7595"


@pytest.mark.parametrize(
    ("query", "reference"),
    [("?tag=v1", GitTag("v1")), ("?rev=abc", GitRev("abc")), ("", None)],
    ids=["tag", "rev", "default-branch"],
)
def test_every_git_reference_cargo_spells_is_recognized(query, reference):
    source = GitSource.parse(f"git+https://github.com/o/r{query}#deadbeef")
    assert source.reference == reference
    assert source.repository == "https://github.com/o/r"


def test_one_repository_is_recognized_across_its_spellings():
    source = GitSource.parse("git+ssh://git@github.com/Certora/Anchor#3ebe7595")
    assert source.is_from("https://github.com/certora/anchor.git")
    assert source.is_from("https://github.com/Certora/anchor/")


def test_a_repository_whose_name_extends_another_is_a_different_repository():
    source = GitSource.parse("git+https://github.com/Certora/anchor-extras.git#3ebe7595")
    assert not source.is_from("https://github.com/Certora/anchor.git")


def test_the_owning_crate_is_the_deepest_one_containing_the_file():
    """A workspace whose root is itself a package contains every nested crate's files too, so the
    shallow match is always available and always wrong."""
    workspace = _workspace(_METADATA)
    owner = workspace.owning(Path("/w/programs/lend/src/lib.rs"))
    assert owner is not None and owner.name == "example-lending"


def test_a_file_in_no_member_has_no_owning_crate():
    workspace = _workspace(_METADATA)
    assert workspace.owning(Path("/elsewhere/src/lib.rs")) is None


def test_a_crate_family_is_recognized_by_name():
    """``cvlr`` does not declare its family anywhere; the helper crates come in as ordinary
    dependencies, and an agent asking what a macro expands to needs all of them."""
    workspace = _workspace(_METADATA)
    assert [c.name for c in workspace.family("cvlr")] == ["cvlr", "cvlr-log"]


def test_a_published_dependency_is_distinguished_from_a_workspace_member():
    workspace = _workspace(_METADATA)
    (cvlr,) = workspace.resolved("cvlr")
    (lend,) = workspace.resolved("example-lending")
    assert not cvlr.is_local
    assert lend.is_local


def test_every_copy_of_a_crate_the_graph_resolves_twice_is_reported():
    """A graph holds two releases of one crate when dependents require incompatible ones. A lookup
    that returned the first would answer about whichever copy cargo happened to list first."""
    payload = json.loads(json.dumps(_METADATA))
    older = json.loads(json.dumps(payload["packages"][2]))
    older.update(id=f"{older['source']}#cvlr@0.4.1", version="0.4.1")
    payload["packages"].append(older)
    assert [c.version for c in _workspace(payload).resolved("cvlr")] == ["0.6.1", "0.4.1"]


def _cargo_answers(monkeypatch, answer):
    """``cargo metadata`` as a stub: ``answer`` is returned, or raised if it is an exception."""

    def run(args, **_kwargs):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(metadata.shutil, "which", lambda _name: "/usr/bin/cargo")
    monkeypatch.setattr(metadata.subprocess, "run", run)


@pytest.mark.asyncio
async def test_a_failed_cargo_metadata_carries_cargos_explanation(monkeypatch):
    stderr = (
        "error: failed to parse manifest at `/w/Cargo.toml`\n\nCaused by:\n  TOML parse error\n"
    )
    _cargo_answers(monkeypatch, subprocess.CompletedProcess([], 101, stdout="", stderr=stderr))
    failure = await Workspace.read(Path("/w"))
    assert failure == CargoFailed(stderr)
    assert failure.describe().startswith("error: failed to parse manifest")


@pytest.mark.asyncio
async def test_a_cargo_metadata_that_runs_out_of_time_says_so(monkeypatch):
    _cargo_answers(monkeypatch, subprocess.TimeoutExpired(["cargo"], 7))
    assert await Workspace.read(Path("/w"), timeout_s=7) == CargoTimedOut(7)


@pytest.mark.asyncio
async def test_cargo_output_that_does_not_validate_is_a_failure_not_an_exception(monkeypatch):
    _cargo_answers(monkeypatch, subprocess.CompletedProcess([], 0, stdout="{}", stderr=""))
    failure = await Workspace.read(Path("/w"))
    assert isinstance(failure, UnreadableMetadata)
    assert "packages" in failure.describe()


# --------------------------------------------------------------------------------------------
# Cargo.toml
# --------------------------------------------------------------------------------------------


def test_a_bare_version_string_is_a_version_requirement():
    """``foo = "1.0"`` is cargo's shorthand for ``foo = { version = "1.0" }``; a reader that kept
    the two apart would have to handle both at every use."""
    manifest = parse_manifest(
        '[dependencies]\ncvlr = "=0.6.1"\nlocal = { path = "../local", version = "0.1" }\n'
    )
    assert manifest.dependencies["cvlr"] == Dependency(version="=0.6.1")
    assert manifest.dependencies["local"] == Dependency(path="../local", version="0.1")


def test_an_empty_workspace_table_still_makes_a_workspace_root():
    assert parse_manifest("[workspace]\n").workspace is not None
    assert parse_manifest('[package]\nname = "p"\n').workspace is None


@pytest.mark.parametrize(
    "text",
    [
        "[dependencies\nnot toml",
        '[features]\ncertora = "dep:cvlr"\n',
        "[dependencies]\ncvlr = 6\n",
    ],
    ids=["unparseable", "feature-not-a-list", "dependency-not-a-spec"],
)
def test_a_manifest_cargo_would_refuse_is_malformed(text):
    with pytest.raises(MalformedManifest):
        parse_manifest(text)


# --------------------------------------------------------------------------------------------
# CVLR source resolution
# --------------------------------------------------------------------------------------------


def test_the_cvlr_source_roots_are_the_crate_directories_the_build_resolved():
    sources = CvlrSources.of(_workspace(_METADATA))
    assert [(c.name, c.version) for c in sources.crates] == [
        ("cvlr", "0.6.1"),
        ("cvlr-log", "0.6.1"),
    ]
    assert sources.roots() == (
        Path("/home/u/.cargo/registry/src/idx/cvlr-0.6.1"),
        Path("/home/u/.cargo/registry/src/idx/cvlr-log-0.6.1"),
    )


def test_a_project_on_the_reference_core_but_without_the_chain_crate_reports_that_gap():
    """Two different statements, and only one of them stops a run: an old ``cvlr-solana`` and no
    ``cvlr-solana`` at all. They are separate types so a caller cannot conflate them."""
    sources = CvlrSources.of(_workspace(_METADATA))
    gaps = {g.crate: g for g in sources.gaps(SOLANA)}
    assert "cvlr" not in gaps, "the fixture pins the reference core, so it is not a gap"
    assert isinstance(gaps["cvlr-solana"], Absent)
    assert "is not a dependency of this project" in gaps["cvlr-solana"].describe()
    assert sources.mismatched(SOLANA) == (), "absence is not something to refuse over"


def test_an_older_cvlr_than_the_pin_is_a_mismatch_that_stops_the_run():
    payload = json.loads(json.dumps(_METADATA))
    payload["packages"][2]["version"] = "0.4.1"
    sources = CvlrSources.of(_workspace(payload))
    mismatched = {g.crate: g for g in sources.mismatched(SOLANA)}
    assert mismatched["cvlr"].resolved == "0.4.1"
    assert mismatched["cvlr"].reference == "0.6.1"


def test_an_older_cvlr_beside_the_pinned_one_is_still_a_mismatch():
    payload = json.loads(json.dumps(_METADATA))
    older = json.loads(json.dumps(payload["packages"][2]))
    older.update(id=f"{older['source']}#cvlr@0.4.1", version="0.4.1")
    payload["packages"].append(older)
    (mismatch,) = CvlrSources.of(_workspace(payload)).mismatched(SOLANA)
    assert (mismatch.crate, mismatch.resolved) == ("cvlr", "0.4.1")


# --------------------------------------------------------------------------------------------
# the conf
# --------------------------------------------------------------------------------------------


def test_loops_are_bounded_soundly_and_the_bound_is_raised_instead():
    """``optimistic_loop`` assumes loops halt instead of proving it, so a violation that needs
    more iterations is not found. Bound the inputs that set the trip count, or edit the loop,
    before raising ``loop_iter``."""
    conf = cvlr_conf.tunable_conf(cvlr_conf.TunableConf())
    assert conf["optimistic_loop"] is False
    assert conf["loop_iter"] == "2"


def test_the_base_enables_no_optimistic_solana_flags():
    """None of the ``-solanaOptimistic*`` flags. They are unsound, and they do not fix the [3308]
    they were meant to."""
    flags = cvlr_conf.tunable_conf(cvlr_conf.TunableConf())["prover_args"]
    assert not [f for f in flags if f.startswith("-solanaOptimistic")]


def test_every_conf_checks_vacuity():
    """With ``rule_sanity`` off, a [3308] inside the generated vacuity rule is reported as
    verified. No setting turns it off."""
    for tunable in (
        cvlr_conf.TunableConf(),
        cvlr_conf.TunableConf(loop_iter=5, optimistic_loop=True),
    ):
        assert cvlr_conf.tunable_conf(tunable)["rule_sanity"] == "basic"


def test_the_loop_bound_is_written_the_way_a_conf_spells_an_integer():
    assert cvlr_conf.tunable_conf(cvlr_conf.TunableConf(loop_iter=4))["loop_iter"] == "4"


def test_a_conf_change_invalidates_a_stamp_earned_before_it():
    """A verdict under one loop bound, or with loops assumed to finish, is not a verdict under
    another."""
    default = cvlr_conf.conf_history(cvlr_conf.TunableConf())
    assert default != cvlr_conf.conf_history(cvlr_conf.TunableConf(loop_iter=3))
    assert default != cvlr_conf.conf_history(cvlr_conf.TunableConf(optimistic_loop=True))
    assert default == cvlr_conf.conf_history(cvlr_conf.TunableConf())


# ---------------------------------------------------------------------------------------------
# One submission's keys


def _submission(**kwargs) -> dict:
    return cvlr_conf.solana_conf(
        cvlr_conf.TunableConf(),
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
