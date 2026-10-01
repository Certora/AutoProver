"""CVLR metadata and the prover conf, with no toolchain, network, or LLM.

Nothing here shells out to cargo or submits a job. What is checked is the parse of
``cargo metadata`` and ``Cargo.toml``, the conf a tunable conf renders to and the keys one
submission adds, the argument vector a build is given, and the build script handed to the prover.
``tests/test_cvlr_end_to_end.py`` submits a real build and compares the verdicts against a
checked-in file. It is ``expensive``.
"""

import json
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore

from composer.cargo.manifest import Dependency, MalformedManifest, parse_manifest
from composer.cargo import metadata
from composer.cargo.metadata import (
    CargoFailed,
    CargoMetadataJson,
    CargoTimedOut,
    CratePackage,
    GitBranch,
    GitRev,
    GitSource,
    GitTag,
    RegistrySource,
    UnreadableMetadata,
    Workspace,
    parse_metadata,
)
from composer.prover import conf as prover_conf
from composer.spec.cvlr import conf as cvlr_conf
from composer.spec.cvlr.crates import Absent, CvlrSources
from composer.spec.cvlr.reference import SOLANA
from composer.cargo.sbf import (
    BUILD_TIMEOUT_ENV,
    DEFAULT_BUILD_TIMEOUT_S,
    MalformedBuildManifest,
    Built,
    build_command_path,
    parse_manifest as parse_build_manifest,
    platform_tools_cargos,
    resolved_build_timeout_s,
    SbfBuild,
    write_build_script,
)
from composer.cargo.session import CargoSession, CompileFailed
from composer.cargo.toolchain import SolanaToolchain, ToolchainRequestUnsupported
from composer.diagnostics.timing import (
    RunSummary,
    install_run_summary,
    set_current_task_id,
)
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceFields
from composer.spec.cvlr.prover import Submission, write_submission
from composer.spec.cvlr.verify import CexAnalysis, _CaptureCallbacks, _RunAccounting
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.system_model import SolidityIdentifier


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


def test_the_certora_metadata_table_is_read_as_present_or_absent():
    assert parse_manifest('[package]\nname = "p"\n').certora_metadata is None
    assert parse_manifest("[workspace]\n").certora_metadata is None
    other_tool = '[package]\nname = "p"\n\n[package.metadata.docs.rs]\nall-features = true\n'
    assert parse_manifest(other_tool).certora_metadata is None
    declared = '[package]\nname = "p"\n\n[package.metadata.certora]\nsources = ["src/**/*.rs"]\n'
    assert parse_manifest(declared).certora_metadata is not None


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


def test_the_resolved_cvlr_crates_are_the_family_the_build_pulled_in():
    """``cvlr`` is a facade — ``cvlr_assert!`` expands in ``cvlr-asserts`` — so the family is what
    matters and it is recognized by name rather than declared anywhere."""
    sources = CvlrSources.of(_workspace(_METADATA))
    assert [(c.name, c.version) for c in sources.crates] == [("cvlr", "0.6.1"), ("cvlr-log", "0.6.1")]




def test_two_copies_of_one_version_are_ordered_the_same_whatever_order_cargo_lists_them():
    """A registry release and a git checkout of it both resolve as ``cvlr 0.6.1``."""
    registry = CratePackage(
        name="cvlr",
        version="0.6.1",
        manifest_path=Path("/home/u/.cargo/registry/src/idx/cvlr-0.6.1/Cargo.toml"),
        lib=None,
        features=(),
        source=RegistrySource("registry+https://github.com/rust-lang/crates.io-index"),
    )
    checkout = replace(
        registry,
        manifest_path=Path("/home/u/.cargo/git/checkouts/cvlr-1a2b/3c4d/cvlr/Cargo.toml"),
        source=GitSource.parse("git+https://github.com/Certora/cvlr.git?branch=main#3c4d5e6f"),
    )
    orders = [
        CvlrSources.of(
            Workspace(root=Path("/p"), target_directory=Path("/p/target"), members=(), packages=p)
        ).crates
        for p in ((registry, checkout), (checkout, registry))
    ]
    assert orders[0] == orders[1]


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
        cvlr_conf.RunOverlay(build_script=Path("/submission/confined_build.py"), **kwargs),
    )


def test_the_run_names_the_build_script():
    assert _submission()["build_script"] == "/submission/confined_build.py"


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


def test_rewording_the_reason_for_optimistic_loop_keeps_the_stamp():
    """The justification is for the reviewer and is not in the conf, so a verdict earned under
    the setting stays a verdict under it whatever the account of it says."""
    first = cvlr_conf.TunableConf(optimistic_loop=cvlr_conf.OptimisticLoop(why="first reason"))
    second = cvlr_conf.TunableConf(optimistic_loop=cvlr_conf.OptimisticLoop(why="second"))
    assert cvlr_conf.conf_history(first) == cvlr_conf.conf_history(second)


def test_the_build_warms_with_the_cargo_it_will_actually_run(tmp_path):
    """Both platform-tools flavours, when the ``cargo`` binary is present.

    The tool picks which one runs.
    """
    for flavour in ("platform-tools-certora", "platform-tools"):
        binary = tmp_path / "v1.43" / flavour / "rust" / "bin" / "cargo"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")

    found = platform_tools_cargos("v1.43", root=tmp_path)

    assert [p.parent.parent.parent.name for p in found] == [
        "platform-tools-certora", "platform-tools"
    ], "both flavours, when the cargo binary is present"


def test_a_version_with_no_toolchain_yields_nothing_to_warm(tmp_path):
    """No binaries means nothing to fetch.

    An unconfined build can still fetch. A confined build already fails in
    :class:`composer.cargo.sbf.PlatformToolsMissing` when the toolchain
    directory is missing.
    """
    assert platform_tools_cargos("v1.43", root=tmp_path) == ()


def test_a_directory_without_the_binary_is_not_offered(tmp_path):
    """A toolchain directory with no ``cargo`` binary is skipped.

    Fetching that path would fail, and the failure would look like a problem
    in the project.
    """
    (tmp_path / "v1.43" / "platform-tools" / "rust" / "bin").mkdir(parents=True)

    assert platform_tools_cargos("v1.43", root=tmp_path) == ()


def test_warming_is_tracked_per_binary_not_per_session(tmp_path):
    """Two cargos do not share a git cache. One of them being warm says nothing about the other."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig())

    assert not session.already_warmed("/tools/v1.43/rust/bin/cargo")
    session._warmed.add("/tools/v1.43/rust/bin/cargo")

    assert session.already_warmed("/tools/v1.43/rust/bin/cargo")
    assert not session.already_warmed("cargo"), "the host cargo is a separate cache"


def test_the_certora_feature_is_the_default():
    assert Submission(manifest_path=Path("/w/C.toml"), stem="unit", msg="unit").features == ("certora",)


def _build(**overrides) -> SbfBuild:
    return replace(
        SbfBuild(manifest_path=Path("/w/Cargo.toml")),
        **overrides,
    )


def test_the_build_never_touches_rustup():
    """``cargo certora-sbf`` links a rustup toolchain on each build and writes
    ``RUSTUP_HOME``. That directory is read-only under confinement, and the
    failure names neither rustup nor the sandbox."""
    assert "--no-rustup" in _build().argv()


class TestResolvedBuildTimeout:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(BUILD_TIMEOUT_ENV, raising=False)
        assert resolved_build_timeout_s() == DEFAULT_BUILD_TIMEOUT_S

    def test_integer_env_value_used(self, monkeypatch):
        monkeypatch.setenv(BUILD_TIMEOUT_ENV, "600")
        assert resolved_build_timeout_s() == 600

    def test_non_integer_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(BUILD_TIMEOUT_ENV, "not-a-number")
        assert resolved_build_timeout_s() == DEFAULT_BUILD_TIMEOUT_S


def test_features_reach_the_build_as_one_space_separated_value():
    argv = _build(features=("certora", "mocks")).argv()
    assert argv[argv.index("--features") + 1] == "certora mocks"


def test_a_manifest_missing_what_the_prover_requires_is_rejected_here():
    """A missing key fails here. At submission time the same gap is a
    ``CertoraUserInputError`` from a run that has already started."""
    with pytest.raises(MalformedBuildManifest) as rejected:
        parse_build_manifest(json.dumps({"success": True, "project_directory": "/w", "sources": []}))
    assert "executables" in str(rejected.value.__cause__)


def test_a_build_that_reports_failure_is_not_read_as_a_manifest():
    with pytest.raises(MalformedBuildManifest) as rejected:
        parse_build_manifest(
            json.dumps(
                {"success": False, "project_directory": "/w", "sources": [], "executables": "x.so"}
            )
        )
    assert "success" in str(rejected.value.__cause__)


def test_a_build_that_printed_no_json_is_rejected():
    with pytest.raises(MalformedBuildManifest) as rejected:
        parse_build_manifest("error: could not compile `first_example`")
    assert "Invalid JSON" in str(rejected.value.__cause__)


_VALID = {"success": True, "project_directory": "/w", "sources": [], "executables": "x.so"}


@pytest.mark.parametrize(
    "field, value",
    [("executables", ["x.so"]), ("sources", "p/src/lib.rs"), ("solana_inlining", 3)],
)
def test_a_manifest_field_of_the_wrong_type_is_rejected_here(field, value):
    with pytest.raises(MalformedBuildManifest) as rejected:
        parse_build_manifest(json.dumps({**_VALID, field: value}))
    assert field in str(rejected.value.__cause__)


def test_a_single_env_file_is_read_as_one_path_the_way_the_prover_reads_it():
    manifest = parse_build_manifest(
        json.dumps({**_VALID, "solana_inlining": "envs/inlining.txt", "solana_summaries": ""})
    )
    assert manifest.solana_inlining == ("envs/inlining.txt",)
    assert manifest.solana_summaries == ()


def test_the_manifest_keeps_cargos_own_paths():
    manifest = parse_build_manifest(
        json.dumps(
            {
                "success": True,
                "project_directory": "/w",
                "sources": ["p/Cargo.toml", "p/src/**/*.rs"],
                "executables": "target/sbf-solana-solana/release/p.so",
                "solana_inlining": ["p/../envs/cvlr_inlining_core.txt"],
            }
        )
    )
    assert manifest.artifact == Path("/w/target/sbf-solana-solana/release/p.so")
    assert manifest.solana_inlining == ("p/../envs/cvlr_inlining_core.txt",)


def _unconfined(tmp_path: Path) -> CargoSession:
    """A session in ``tmp_path/work``. ``provider="none"`` makes the build
    script a passthrough, so writing the conf needs no toolchain."""
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return CargoSession(workdir=workdir, sandbox=SandboxConfig(provider="none"))


def _outside(tmp_path: Path) -> Path:
    """Where a submission's build script and conf go: beside the workdir, not in it."""
    into = tmp_path / "submission"
    into.mkdir(exist_ok=True)
    return into


@pytest.mark.asyncio
async def test_the_generated_build_script_reruns_the_gates_command(tmp_path):
    """The prover reruns this command. It has to be the build the gate already
    ran, or the reported artifact is not the one the gate approved."""
    session = _unconfined(tmp_path)
    build = _build(features=("certora",))
    script = await write_build_script(session, build, into=_outside(tmp_path), name="unit")
    command = json.loads(build_command_path(script).read_text())
    assert command["argv"] == build.argv()
    assert command["cwd"] == str(session.workdir.resolve())


@pytest.mark.asyncio
async def test_an_unconfined_session_produces_a_build_script_with_no_wrapper(tmp_path):
    """``provider="none"`` is a passthrough. The script runs the command with no wrapper."""
    script = await write_build_script(
        _unconfined(tmp_path), _build(), into=_outside(tmp_path), name="unit"
    )
    assert json.loads(build_command_path(script).read_text())["argv_prefix"] == []


@pytest.mark.asyncio
async def test_the_build_script_is_executable(tmp_path):
    """``certoraParseBuildScript`` execs the script. Without the execute bit,
    ``validate_exec_file`` rejects the conf."""
    script = await write_build_script(
        _unconfined(tmp_path), _build(), into=_outside(tmp_path), name="unit"
    )
    assert script.stat().st_mode & stat.S_IXUSR


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", [".", ".certora_build", ".sandbox_tmp"])
async def test_a_confined_session_refuses_to_write_the_build_script_where_its_build_can(
    tmp_path, inside
):
    """The prover runs the script unconfined, and the script takes its
    confinement from the command file beside it. A build that could rewrite
    either one could run the prover's rerun unconfined."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="launcher"))
    into = tmp_path / inside
    into.mkdir(exist_ok=True)

    with pytest.raises(ValueError, match="writable by the confined build"):
        await write_build_script(session, _build(), into=into, name="unit")

    assert list(into.glob("unit.*")) == []


@pytest.mark.asyncio
async def test_a_symlink_into_the_workdir_does_not_pass_for_outside_it(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (tmp_path / "link").symlink_to(workdir)
    session = CargoSession(workdir=workdir, sandbox=SandboxConfig(provider="launcher"))

    with pytest.raises(ValueError, match="writable by the confined build"):
        await write_build_script(session, _build(), into=tmp_path / "link", name="unit")


def test_a_directory_beside_the_workdir_is_out_of_the_confined_builds_reach(tmp_path):
    session = CargoSession(workdir=tmp_path / "work", sandbox=SandboxConfig(provider="launcher"))

    assert not session.build_can_write(tmp_path / "submission")
    assert session.build_can_write(tmp_path / "work" / "src")


@pytest.mark.asyncio
async def test_a_tuned_conf_reaches_the_file_the_prover_is_handed(tmp_path):
    """Author settings are written into the conf the prover is given."""
    edited = cvlr_conf.TunableConf(loop_iter=4, optimistic_loop=cvlr_conf.OptimisticLoop(why="w"))
    conf_path = await write_submission(
        _unconfined(tmp_path),
        Submission(
            manifest_path=tmp_path / "work" / "Cargo.toml",
            settings=edited,
            stem="unit",
            msg="unit",
        ),
        into=_outside(tmp_path),
    )

    written = json.loads(conf_path.read_text())
    assert written["loop_iter"] == "4"
    assert written["optimistic_loop"] is True


@pytest.mark.asyncio
async def test_the_written_conf_names_the_build_script_written_beside_it(tmp_path):
    """The conf names the build script written next to it.

    A conf that names a missing script is rejected by ``certoraRun`` after the upload.
    """
    into = _outside(tmp_path)
    conf_path = await write_submission(
        _unconfined(tmp_path),
        Submission(manifest_path=tmp_path / "work" / "Cargo.toml", stem="unit", msg="unit"),
        into=into,
    )

    assert conf_path.parent == into.resolve()
    script = Path(json.loads(conf_path.read_text())["build_script"])
    assert script.is_absolute(), "the prover runs in the workdir, which does not contain it"
    assert script.parent == into.resolve()
    assert script.is_file()
    assert script.stat().st_mode & stat.S_IXUSR, "certoraRun execs it directly"


@pytest.mark.asyncio
async def test_two_units_sharing_a_tree_write_separate_confs_and_build_scripts(tmp_path):
    """Two submissions in one working tree write separate confs and build scripts.

    They can be in flight together. A shared file would send one submission's
    loop bound or features with the other's job.
    """
    session = _unconfined(tmp_path)
    into = _outside(tmp_path)
    manifest = session.workdir / "Cargo.toml"
    solvency = await write_submission(
        session,
        Submission(
            manifest_path=manifest,
            settings=cvlr_conf.TunableConf(loop_iter=3),
            stem="solvency",
            msg="solvency",
            features=("certora", "solvency"),
        ),
        into=into,
    )
    access = await write_submission(
        session,
        Submission(
            manifest_path=manifest,
            settings=cvlr_conf.TunableConf(loop_iter=7),
            stem="access",
            msg="access",
            features=("certora", "access"),
        ),
        into=into,
    )

    confs = {unit: json.loads(path.read_text()) for unit, path in (("solvency", solvency), ("access", access))}
    assert confs["solvency"]["loop_iter"] == "3"
    assert confs["access"]["loop_iter"] == "7"

    def argv(conf: dict) -> list[str]:
        return json.loads(build_command_path(Path(conf["build_script"])).read_text())["argv"]

    def features(conf: dict) -> str:
        return argv(conf)[argv(conf).index("--features") + 1]

    assert features(confs["solvency"]) == "certora solvency"
    assert features(confs["access"]) == "certora access"


def _stub_command(script: Path) -> None:
    """Point the command file at a stand-in that prints its working directory and arguments."""
    command_file = build_command_path(script)
    command = json.loads(command_file.read_text())
    command["argv"] = [
        sys.executable, "-c", "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))",
    ]
    command_file.write_text(json.dumps(command))


@pytest.mark.asyncio
async def test_the_build_script_runs_its_command_in_the_workdir_with_the_provers_features(tmp_path):
    """Run the script the way the prover does, including ``--cargo_features``."""
    session = _unconfined(tmp_path)
    script = await write_build_script(session, _build(), into=_outside(tmp_path), name="unit")
    _stub_command(script)

    ran = subprocess.run(
        [str(script), "--json", "--cargo_features", "a", "b"],
        capture_output=True, text=True, check=True,
    )

    cwd, args = json.loads(ran.stdout)
    assert Path(cwd) == session.workdir.resolve()
    assert args == ["--features", "a b"]


@pytest.mark.asyncio
async def test_the_build_script_carries_the_build_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv(BUILD_TIMEOUT_ENV, "600")
    script = await write_build_script(
        _unconfined(tmp_path), _build(), into=_outside(tmp_path), name="unit"
    )
    assert json.loads(build_command_path(script).read_text())["timeout_s"] == 600


def _is_gone(pid: int) -> bool:
    """Exited: no process, or a zombie waiting for its new parent to reap it."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state == "Z"


@pytest.mark.asyncio
async def test_a_build_past_its_timeout_is_stopped_with_everything_it_started(tmp_path):
    """Cargo runs the compilers as child processes, so stopping cargo alone would leave them running."""
    script = await write_build_script(
        _unconfined(tmp_path), _build(), into=_outside(tmp_path), name="unit"
    )
    pid_file = tmp_path / "child.pid"
    command_file = build_command_path(script)
    command = json.loads(command_file.read_text())
    command["argv"] = [
        sys.executable, "-c",
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen(['sleep', '60']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(60)",
        str(pid_file),
    ]
    command["timeout_s"] = 1
    command_file.write_text(json.dumps(command))

    ran = subprocess.run([str(script), "--json"], capture_output=True, text=True, timeout=30)

    assert ran.returncode == 124
    assert ran.stdout == ""
    assert "did not finish within 1s" in ran.stderr
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while not _is_gone(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _is_gone(child)


def _source(root: Path) -> SourceFields:
    return SourceFields(
        project_root=str(root),
        contract_name=SolidityIdentifier("lend"),
        relative_path="src/lib.rs",
        forbidden_read=None,
    )


@pytest.mark.asyncio
async def test_a_project_that_is_not_a_cargo_workspace_resolves_no_source_unit(tmp_path):
    """The empty answer the seam documents as "apply your own convention" — not an exception, because
    it is a state the wheel already handles."""
    assert await SolanaToolchain().source_unit(_source(tmp_path)) == {}


@pytest.mark.asyncio
async def test_a_prep_asking_for_an_idl_is_refused_before_any_work(tmp_path):
    """Refusing up front is the point: today an unregistered chain fails immediately, and a partial
    registration that failed after a multi-minute build would be a regression."""
    from composer.rustapp.wire import WorkspacePrep

    plan = WorkspacePrep(files={}, toolchain_request={"idl_dest": "fuzz/idls/lend.json"})
    with pytest.raises(ToolchainRequestUnsupported, match="IDL"):
        await SolanaToolchain().prepare(
            plan, None, source=_source(tmp_path), sandbox=None, timeout_s=60  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_a_prep_key_nothing_acts_on_is_refused_rather_than_ignored(tmp_path):
    """A request nothing acts on is a plan that believes work happened."""
    from composer.rustapp.wire import WorkspacePrep

    plan = WorkspacePrep(files={}, toolchain_request={"transmute_program": True})
    with pytest.raises(ToolchainRequestUnsupported, match="transmute_program"):
        await SolanaToolchain().prepare(
            plan, None, source=_source(tmp_path), sandbox=None, timeout_s=60  # type: ignore[arg-type]
        )


def test_a_failed_compile_carries_the_compilers_own_words():
    failed = CompileFailed(diagnostics="error[E0599]: no method named `cvlr_assert`", exit_code=101)
    assert not isinstance(failed, Built)
    assert "E0599" in failed.diagnostics


@pytest.mark.asyncio
async def test_a_prover_run_is_accounted_for():
    """``ProverCallbacks``' defaults are no-ops, so a backend that overrides only the events it
    cares about opts out of the run's prover accounting without saying so — which is what CVLR did
    until this class existed. The consequence was not cosmetic: a run whose wall clock is mostly
    cloud time reported none of it, in ``summary.format()`` and in ``job_info.json`` alike."""
    summary = RunSummary()
    install_run_summary(summary)
    callbacks = _RunAccounting()

    # A task has to be active for the per-task attribution to land anywhere: the link and the
    # runtime are folded into that task's phase record when the phase closes.
    with set_current_task_id("verify-deposits"):
        await callbacks.on_prover_run(["certoraSolanaProver", "run.conf"])
        await callbacks.on_prover_link("https://prover.certora.com/output/1/2")
        await callbacks.on_prover_runtime(4200)
        await callbacks.on_prover_result({})
    summary.record_phase(
        task_id="verify-deposits", label="deposits", phase="formalization",
        wall_s=90.0, queue_wait_s=0.0,
    )

    assert summary.prover_usage_summary()["total_ms"] == 4200
    assert summary.prover_total_calls == 1
    assert summary.phases[0].final_link == "https://prover.certora.com/output/1/2"
    assert summary.phases[0].prover_reported_ms == 4200


@pytest.mark.asyncio
async def test_capturing_evidence_does_not_replace_the_accounting():
    """The capture callbacks override ``on_prover_result`` for their own reasons; the wall-clock
    tally lives in the same event, so the override has to chain."""
    summary = RunSummary()
    install_run_summary(summary)
    callbacks = _CaptureCallbacks(CexAnalysisStore(store=InMemoryStore(), namespace=("t",)))

    await callbacks.on_prover_run([])
    await callbacks.on_prover_result({})

    assert summary.prover_total_calls == 1


def test_counterexamples_are_explained_on_the_authors_bound_model():
    """The analysis forks the author's conversation, and a model without the author's tools makes
    every fork miss the cache on the whole of it: on the vault benchmark, two thirds of the first
    44 minutes' spend. So there is no default model to fall back on, only the author's."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    analysis = CexAnalysis(store=CexAnalysisStore(store=InMemoryStore(), namespace=("t",)))
    state = {"messages": []}
    with pytest.raises(RuntimeError, match="before the author's graph was built"):
        analysis.handler(state)  # type: ignore[arg-type]

    bound = FakeListChatModel(responses=["x"])
    analysis.model.bind(bound)
    assert analysis.handler(state).llm is bound  # type: ignore[arg-type, attr-defined]
