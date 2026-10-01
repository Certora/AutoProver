"""CVLR metadata and the prover conf, with no toolchain, network, or LLM.

Nothing here shells out to cargo or submits a job. What is checked is the parse of
``cargo metadata`` and ``Cargo.toml``, the conf a tunable conf renders to, and the keys one
submission adds.
"""

import json
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

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
    MalformedBuildManifest,
    Built,
    build_command_path,
    parse_manifest as parse_build_manifest,
    platform_tools_cargos,
    PLATFORM_TOOLS_ROOT,
    SbfBuild,
    write_build_script,
)
from composer.cargo.session import CargoSession, CompileFailed
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.prover import Submission, write_submission


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


def test_the_build_warms_with_the_cargo_it_will_actually_run(tmp_path):
    """A cache is only warm for the cargo that filled it.

    Cargo hashes a git source into a directory name and the hash is not stable across versions: the
    host's cargo 1.89 fetched ``Certora/anchor`` into ``git/db/anchor-8a7e45e4c93a95b5`` while the
    cargo 1.79 inside platform-tools v1.43 looked for ``git/db/anchor-1f3eb14fb7b4e8f1``, found
    nothing, and — confined and therefore offline — called it a network failure. Measured, not
    inferred: both directories exist side by side after fetching with each.
    """
    for flavour in ("platform-tools-certora", "platform-tools"):
        binary = tmp_path / "v1.43" / flavour / "rust" / "bin" / "cargo"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")

    found = platform_tools_cargos("v1.43", root=tmp_path)

    assert [p.parent.parent.parent.name for p in found] == [
        "platform-tools-certora", "platform-tools"
    ], "both flavours are warmed, since which one the build picks is the tool's business"


def test_a_version_with_no_toolchain_yields_nothing_to_warm(tmp_path):
    """Not an error: an unconfined build is not forced offline and fetches what it lacks, and a
    confined one already fails at :class:`PlatformToolsMissing` with an operator action named."""
    assert platform_tools_cargos("v1.43", root=tmp_path) == ()


def test_a_directory_without_the_binary_is_not_offered(tmp_path):
    """A half-extracted toolchain directory exists; warming with a path that is not there would
    fail the fetch and report it as the project's problem."""
    (tmp_path / "v1.43" / "platform-tools" / "rust" / "bin").mkdir(parents=True)

    assert platform_tools_cargos("v1.43", root=tmp_path) == ()


def test_warming_is_tracked_per_binary_not_per_session(tmp_path):
    """Two cargos do not share a git cache, so one having warmed says nothing about the other."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig())

    assert not session.already_warmed("/tools/v1.43/rust/bin/cargo")
    session._warmed.add("/tools/v1.43/rust/bin/cargo")

    assert session.already_warmed("/tools/v1.43/rust/bin/cargo")
    assert not session.already_warmed("cargo"), "the host cargo is a separate cache"


def test_the_certora_feature_is_the_default():
    assert Submission(manifest_path=Path("/w/C.toml"), target_directory=Path("/w/target")).features == ("certora",)


def _build(**overrides) -> SbfBuild:
    return replace(
        SbfBuild(manifest_path=Path("/w/Cargo.toml"), target_dir=Path("/w/target/certora/unit")),
        **overrides,
    )


def test_the_build_never_touches_rustup():
    """``cargo certora-sbf`` registers a toolchain link around each build, which writes to
    ``RUSTUP_HOME`` — read-only under confinement. Dropping ``--no-rustup`` fails the build for a
    reason that names neither rustup nor the sandbox."""
    assert "--no-rustup" in _build().argv()


def test_the_build_is_told_where_the_platform_tools_are():
    """The tool reads ``$CERTORA_PLATFORM_TOOLS_ROOT`` too, and the confined child never sees it:
    the launcher scrubs the environment down to a chain-neutral passthrough list that does not carry
    it. A deployment whose toolchains are not in the tool's default location — the container, whose
    default location is under a world-writable ``$HOME`` — would otherwise grant one root read-only
    and build against another."""
    argv = _build().argv()
    assert argv[argv.index("--platform-tools-root") + 1] == str(PLATFORM_TOOLS_ROOT)


def test_features_reach_the_build_as_one_space_separated_value():
    argv = _build(features=("certora", "mocks")).argv()
    assert argv[argv.index("--features") + 1] == "certora mocks"


def test_a_manifest_missing_what_the_prover_requires_is_rejected_here():
    """The same key missing at submission time is a ``CertoraUserInputError`` from inside a run that
    has already started."""
    with pytest.raises(MalformedBuildManifest, match="executables"):
        parse_build_manifest(json.dumps({"success": True, "project_directory": "/w", "sources": []}))


def test_a_build_that_reports_failure_is_not_read_as_a_manifest():
    with pytest.raises(MalformedBuildManifest, match="success"):
        parse_build_manifest(
            json.dumps(
                {"success": False, "project_directory": "/w", "sources": [], "executables": "x.so"}
            )
        )


def test_a_build_that_printed_no_json_says_so():
    with pytest.raises(MalformedBuildManifest, match="Invalid JSON"):
        parse_build_manifest("error: could not compile `first_example`")


_VALID = {"success": True, "project_directory": "/w", "sources": [], "executables": "x.so"}


@pytest.mark.parametrize(
    "field, value",
    [("executables", ["x.so"]), ("sources", "p/src/lib.rs"), ("solana_inlining", 3)],
)
def test_a_manifest_field_of_the_wrong_type_is_rejected_here(field, value):
    with pytest.raises(MalformedBuildManifest, match=field):
        parse_build_manifest(json.dumps({**_VALID, field: value}))


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


@pytest.mark.asyncio
async def test_the_generated_build_script_reruns_the_gates_command(tmp_path):
    """The prover's build has to be the build that already passed, or the artifact it reports is not
    the artifact the gate approved."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="none"))
    build = _build(features=("certora",))
    script = await write_build_script(session, build, name="unit")
    command = json.loads(build_command_path(script).read_text())
    assert command["argv"] == build.argv()
    assert command["cwd"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_the_build_script_grants_the_platform_tools_root_its_argv_names(tmp_path, monkeypatch):
    """A root outside ``rust_build_policy``'s defaults, as ``$CERTORA_PLATFORM_TOOLS_ROOT`` names."""
    from composer.cargo import sbf
    from composer.sandbox import config as config_mod
    from composer.sandbox.launcher import LauncherProvider

    async def _available(provider):
        return None

    root = tmp_path / "tools"
    root.mkdir()
    monkeypatch.setattr(sbf, "PLATFORM_TOOLS_ROOT", root)
    monkeypatch.setattr(SandboxConfig, "resolve_provider", lambda self: LauncherProvider(binary="rc"))
    monkeypatch.setattr(config_mod, "ensure_available", _available)
    workdir = tmp_path / "w"
    workdir.mkdir()
    session = CargoSession(workdir=workdir, sandbox=SandboxConfig(provider="launcher"))

    script = await write_build_script(session, _build(), name="unit")

    command = json.loads(build_command_path(script).read_text())
    prefix, argv = command["argv_prefix"], command["argv"]
    assert argv[argv.index("--platform-tools-root") + 1] == str(root)
    assert any(a == "--ro" and b == str(root.resolve()) for a, b in zip(prefix, prefix[1:]))


@pytest.mark.asyncio
async def test_an_unconfined_session_produces_a_build_script_with_no_wrapper(tmp_path):
    """The macOS development carve-out: ``provider="none"`` is a passthrough, and the script runs
    the command directly rather than pretending to confine it."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="none"))
    script = await write_build_script(session, _build(), name="unit")
    assert json.loads(build_command_path(script).read_text())["argv_prefix"] == []


@pytest.mark.asyncio
async def test_the_build_script_is_executable(tmp_path):
    """``certoraParseBuildScript`` execs it directly rather than through an interpreter, so without
    the execute bit the shebang means nothing and ``validate_exec_file`` rejects the conf."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="none"))
    script = await write_build_script(session, _build(), name="unit")
    assert script.stat().st_mode & stat.S_IXUSR


def _unconfined(tmp_path: Path) -> CargoSession:
    """No cargo and no network: `provider="none"` makes the build script a passthrough, so the
    conf-writing half is reachable without a toolchain."""
    return CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="none"))


@pytest.mark.asyncio
async def test_a_tuned_conf_reaches_the_file_the_prover_is_handed(tmp_path):
    """The settings an author may change, checked at the far end of the wire."""
    edited = cvlr_conf.TunableConf(loop_iter=4, optimistic_loop=True)
    conf_path = await write_submission(
        _unconfined(tmp_path),
        Submission(
            manifest_path=tmp_path / "Cargo.toml",
            target_directory=tmp_path / "target",
            settings=edited,
            stem="unit",
        ),
    )

    written = json.loads(conf_path.read_text())
    assert written["loop_iter"] == "4"
    assert written["optimistic_loop"] is True


@pytest.mark.asyncio
async def test_the_written_conf_names_the_build_script_written_beside_it(tmp_path):
    """`write_submission` writes two files and the conf points at the other one. Nothing else
    checks that they agree, and a conf naming a script that is not there is rejected inside
    `certoraRun`'s own validation — after the upload, in its vocabulary rather than ours."""
    conf_path = await write_submission(
        _unconfined(tmp_path),
        Submission(manifest_path=tmp_path / "Cargo.toml", target_directory=tmp_path / "target"),
    )

    named = Path(json.loads(conf_path.read_text())["build_script"])
    assert not named.is_absolute(), "the prover resolves it against the workdir it runs in"
    script = tmp_path / named
    assert script.is_file()
    assert script.stat().st_mode & stat.S_IXUSR, "certoraRun execs it directly"


@pytest.mark.asyncio
async def test_two_units_sharing_a_tree_write_separate_confs_and_build_scripts(tmp_path):
    """Units sharing a working tree are submitted concurrently. A file they shared would carry the
    second unit's loop bound or features into the first unit's job."""
    session = _unconfined(tmp_path)
    manifest = tmp_path / "Cargo.toml"
    solvency = await write_submission(
        session,
        Submission(
            manifest_path=manifest,
            target_directory=tmp_path / "target",
            settings=cvlr_conf.TunableConf(loop_iter=3),
            stem="solvency",
            features=("certora", "solvency"),
        ),
    )
    access = await write_submission(
        session,
        Submission(
            manifest_path=manifest,
            target_directory=tmp_path / "target",
            settings=cvlr_conf.TunableConf(loop_iter=7),
            stem="access",
            features=("certora", "access"),
        ),
    )

    confs = {unit: json.loads(path.read_text()) for unit, path in (("solvency", solvency), ("access", access))}
    assert confs["solvency"]["loop_iter"] == "3"
    assert confs["access"]["loop_iter"] == "7"

    def argv(conf: dict) -> list[str]:
        return json.loads(build_command_path(tmp_path / conf["build_script"]).read_text())["argv"]

    def features(conf: dict) -> str:
        return argv(conf)[argv(conf).index("--features") + 1]

    assert features(confs["solvency"]) == "certora solvency"
    assert features(confs["access"]) == "certora access"

    # One crate, two feature sets: in one target directory, each unit's rerun would rebuild the
    # artifact over the other's, and a prover could upload the `.so` the other unit just built.
    target_dirs = {
        next(a for a in argv(conf) if a.startswith("CARGO_TARGET_DIR=")) for conf in confs.values()
    }
    assert target_dirs == {
        f"CARGO_TARGET_DIR={tmp_path / 'target' / 'certora' / 'solvency'}",
        f"CARGO_TARGET_DIR={tmp_path / 'target' / 'certora' / 'access'}",
    }


def _stub_command(script: Path) -> None:
    """Replace the build with a stand-in that reports where it ran and what it was given."""
    command_file = build_command_path(script)
    command = json.loads(command_file.read_text())
    command["argv"] = [
        sys.executable, "-c", "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))",
    ]
    command_file.write_text(json.dumps(command))


@pytest.mark.asyncio
async def test_the_build_script_runs_its_command_in_the_workdir_with_the_provers_features(tmp_path):
    """The script is what the prover execs, so it is run here rather than read."""
    script = await write_build_script(_unconfined(tmp_path), _build(), name="unit")
    _stub_command(script)

    ran = subprocess.run(
        [str(script), "--json", "--cargo_features", "a", "b"],
        capture_output=True, text=True, check=True,
    )

    cwd, args = json.loads(ran.stdout)
    assert Path(cwd) == tmp_path.resolve()
    assert args == ["--features", "a b"]


@pytest.mark.asyncio
async def test_a_build_script_moved_with_its_tree_refuses_to_build_the_original(tmp_path):
    """The command names the tree it was written in by absolute path. Run from a copy, it would
    build the original and hand the prover that tree's artifact."""
    original = tmp_path / "original"
    original.mkdir()
    script = await write_build_script(_unconfined(original), _build(), name="unit")
    _stub_command(script)
    moved = tmp_path / "moved"
    shutil.copytree(original, moved)

    ran = subprocess.run(
        [str(moved / script.relative_to(original)), "--json"], capture_output=True, text=True
    )

    assert ran.returncode != 0
    assert ran.stdout == ""
    assert "Regenerate it here" in ran.stderr


def test_a_relative_target_directory_is_refused():
    """``cargo certora-sbf`` resolves one against two different directories."""
    with pytest.raises(ValueError):
        SbfBuild(manifest_path=Path("/w/Cargo.toml"), target_dir=Path("target"))


def test_a_failed_compile_carries_the_compilers_own_words():
    """The consumer is an authoring agent, and rustc's human-format output — span, note, suggestion
    — is the most actionable form it can be given."""
    failed = CompileFailed(diagnostics="error[E0599]: no method named `cvlr_assert`", exit_code=101)
    assert not isinstance(failed, Built)
    assert "E0599" in failed.diagnostics
