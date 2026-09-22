"""CVLR metadata and the prover conf, with no toolchain, network, or LLM.

Nothing here shells out to cargo or submits a job. What is checked is the parse of
``cargo metadata``, the conf the prover settings render to and the keys one submission adds, the
argument vector a build is given, and the build script handed to the prover.
``tests/test_cvlr_end_to_end.py`` submits a real build and compares the verdicts against a
checked-in file. It is ``expensive``.
"""

import json
import stat
from pathlib import Path

import pytest

from langgraph.store.memory import InMemoryStore

from composer.cargo.metadata import parse_metadata
from composer.cargo.session import CargoSession
from composer.sandbox.config import SandboxConfig
from composer.cargo.sbf import (
    MalformedBuildManifest,
    Built,
    parse_manifest,
    platform_tools_cargos,
    PLATFORM_TOOLS_ROOT,
    sbf_argv,
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
from composer.prover import conf as prover_conf
from composer.spec.cvlr import conf as cvlr_conf
from composer.spec.cvlr.crates import resolve
from composer.spec.cvlr.prover import Submission, write_submission
from composer.spec.cvlr.verify import _CaptureCallbacks, _RunAccounting
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.cvlr_reference import SOLANA
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


def test_the_certora_feature_is_the_default_only_when_no_features_are_named():
    named = Submission(manifest_path=Path("/w/C.toml"), features=("verify",))
    bare = Submission(manifest_path=Path("/w/C.toml"))
    assert named.resolved_features() == ("verify",)
    assert bare.resolved_features() == ("certora",)


# --------------------------------------------------------------------------------------------
# the build command and its manifest
# --------------------------------------------------------------------------------------------


def test_the_build_never_touches_rustup():
    """``cargo certora-sbf`` registers a toolchain link around each build, which writes to
    ``RUSTUP_HOME`` — read-only under confinement. Dropping ``--no-rustup`` fails the build for a
    reason that names neither rustup nor the sandbox."""
    assert "--no-rustup" in sbf_argv(manifest_path=Path("/w/Cargo.toml"))


def test_the_build_is_told_where_the_platform_tools_are():
    """The tool reads ``$CERTORA_PLATFORM_TOOLS_ROOT`` too, and the confined child never sees it:
    the launcher scrubs the environment down to a chain-neutral passthrough list that does not carry
    it. A deployment whose toolchains are not in the tool's default location — the container, whose
    default location is under a world-writable ``$HOME`` — would otherwise grant one root read-only
    and build against another."""
    argv = sbf_argv(manifest_path=Path("/w/Cargo.toml"))
    assert argv[argv.index("--platform-tools-root") + 1] == str(PLATFORM_TOOLS_ROOT)


def test_features_reach_the_build_as_one_space_separated_value():
    argv = sbf_argv(manifest_path=Path("/w/Cargo.toml"), features=("certora", "mocks"))
    assert argv[argv.index("--features") + 1] == "certora mocks"


def test_a_manifest_missing_what_the_prover_requires_is_rejected_here():
    """The same key missing at submission time is a ``CertoraUserInputError`` from inside a run that
    has already started."""
    with pytest.raises(MalformedBuildManifest, match="executables"):
        parse_manifest(json.dumps({"success": True, "project_directory": "/w", "sources": []}))


def test_a_build_that_reports_failure_is_not_read_as_a_manifest():
    with pytest.raises(MalformedBuildManifest, match="success"):
        parse_manifest(
            json.dumps(
                {"success": False, "project_directory": "/w", "sources": [], "executables": "x.so"}
            )
        )


def test_a_build_that_printed_no_json_says_so():
    with pytest.raises(MalformedBuildManifest, match="did not print JSON"):
        parse_manifest("error: could not compile `first_example`")


def test_the_manifest_keeps_cargos_own_paths():
    manifest = parse_manifest(
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
    script = await write_build_script(
        session, manifest_path=tmp_path / "Cargo.toml", features=("certora",)
    )
    command = json.loads((script.parent / "confined_build.json").read_text())
    assert command["argv"][1:] == sbf_argv(
        manifest_path=tmp_path / "Cargo.toml", features=("certora",)
    )
    assert command["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_an_unconfined_session_produces_a_build_script_with_no_wrapper(tmp_path):
    """The macOS development carve-out: ``provider="none"`` is a passthrough, and the script runs
    the command directly rather than pretending to confine it."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="none"))
    script = await write_build_script(session, manifest_path=tmp_path / "Cargo.toml")
    assert json.loads((script.parent / "confined_build.json").read_text())["argv_prefix"] == []


@pytest.mark.asyncio
async def test_the_build_script_is_executable(tmp_path):
    """``certoraParseBuildScript`` execs it directly rather than through an interpreter, so without
    the execute bit the shebang means nothing and ``validate_exec_file`` rejects the conf."""
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="none"))
    script = await write_build_script(session, manifest_path=tmp_path / "Cargo.toml")
    assert script.stat().st_mode & stat.S_IXUSR


# --------------------------------------------------------------------------------------------
# the conf on disk
#
# Everything above pins what the conf functions *compute*. These pin what `write_submission` *writes*,
# which is the only artifact the prover ever sees. The two came apart once already: the solver
# portfolio was merged the ordinary way, four `-solvers` lines became one, nine of twelve solver
# instances were dropped, and nothing failed — because every test on that path stopped at a dict.
# --------------------------------------------------------------------------------------------


def _unconfined(tmp_path: Path) -> CargoSession:
    """No cargo and no network: `provider="none"` makes the build script a passthrough, so the
    conf-writing half is reachable without a toolchain."""
    return CargoSession(workdir=tmp_path, sandbox=SandboxConfig(provider="none"))


@pytest.mark.asyncio
async def test_an_authors_conf_edits_reach_the_file_the_prover_is_handed(tmp_path):
    """What `adjust_prover_config` is for, checked at the far end of the wire."""
    edited = cvlr_conf.ProverSettings(loop_iter=4, solver_portfolio=True)
    conf_path = await write_submission(
        _unconfined(tmp_path),
        Submission(manifest_path=tmp_path / "Cargo.toml", settings=edited, stem="unit"),
    )

    written = json.loads(conf_path.read_text())
    assert written["loop_iter"] == "4"
    assert sum(a.startswith("-solvers ") for a in written["prover_args"]) == 4


@pytest.mark.asyncio
async def test_the_written_conf_names_the_build_script_written_beside_it(tmp_path):
    """`write_submission` writes two files and the conf points at the other one. Nothing else
    checks that they agree, and a conf naming a script that is not there is rejected inside
    `certoraRun`'s own validation — after the upload, in its vocabulary rather than ours."""
    conf_path = await write_submission(
        _unconfined(tmp_path), Submission(manifest_path=tmp_path / "Cargo.toml")
    )

    script = Path(json.loads(conf_path.read_text())["build_script"])
    assert script.is_file()
    assert script.stat().st_mode & stat.S_IXUSR, "certoraRun execs it directly"


@pytest.mark.asyncio
async def test_two_units_sharing_a_tree_write_separate_confs(tmp_path):
    """One working tree per run, one conf per unit (`docs/single-working-tree.md` §2.1). Units are
    prepared under a shared build permit and submitted concurrently, so a shared stem would have the
    second unit's conf replace the first's while the first is still in flight — and the loop bound
    one author raised would arrive on another's rules."""
    session = _unconfined(tmp_path)
    manifest = tmp_path / "Cargo.toml"
    solvency = await write_submission(
        session,
        Submission(
            manifest_path=manifest, settings=cvlr_conf.ProverSettings(loop_iter=3), stem="solvency"
        ),
    )
    access = await write_submission(
        session,
        Submission(
            manifest_path=manifest, settings=cvlr_conf.ProverSettings(loop_iter=7), stem="access"
        ),
    )

    assert solvency != access
    assert json.loads(solvency.read_text())["loop_iter"] == "3"
    assert json.loads(access.read_text())["loop_iter"] == "7"


# --------------------------------------------------------------------------------------------
# the toolchain registration
# --------------------------------------------------------------------------------------------


def _source(root: Path) -> SourceFields:
    return SourceFields(
        project_root=str(root),
        contract_name=SolidityIdentifier("lend"),
        relative_path="src/lib.rs",
        forbidden_read=None,
    )


def test_a_project_that_is_not_a_cargo_workspace_resolves_no_source_unit(tmp_path):
    """The empty answer the seam documents as "apply your own convention" — not an exception, because
    it is a state the wheel already handles."""
    assert SolanaToolchain().source_unit(_source(tmp_path)) == {}


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


# --------------------------------------------------------------------------------------------
# which Prover takes the run
# --------------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------------
# reporting a build that did not pass
# --------------------------------------------------------------------------------------------


def test_a_failed_compile_carries_the_compilers_own_words():
    """The consumer is an authoring agent, and rustc's human-format output — span, note, suggestion
    — is the most actionable form it can be given."""
    failed = CompileFailed(diagnostics="error[E0599]: no method named `cvlr_assert`", exit_code=101)
    assert not isinstance(failed, Built)
    assert "E0599" in failed.diagnostics


# --------------------------------------------------------------------------------------------
# what a prover run costs
# --------------------------------------------------------------------------------------------


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

