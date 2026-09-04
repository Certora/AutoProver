"""The deterministic half of the CVLR backend, tested without a toolchain, a network or an LLM.

``docs/cvlr-backend-plan.md`` §6 puts "deterministic first" ahead of everything else, and the claim
that phase 1b is unit-testable is only worth making if these tests need nothing installed. So
nothing here shells out to cargo and nothing submits: what is checked is the reading and the
layering — the parse of ``cargo metadata``, the parse of a conf, which conf keys a run owns, the
argument vector a build is given, and the shape of the build script handed to the prover.

The end-to-end counterpart — a real build submitted to the real Prover and compared against a
checked-in expected-verdict file — is ``tests/test_cvlr_end_to_end.py``, marked ``expensive``.
"""

import json
import stat
from pathlib import Path

import pytest

from langgraph.store.memory import InMemoryStore

from composer.cargo.metadata import parse_metadata
from composer.cargo.sbf import (
    MalformedBuildManifest,
    Built,
    parse_manifest,
    sbf_argv,
    write_build_script,
)
from composer.cargo.session import CargoSession, CompileFailed
from composer.cargo.toolchain import SolanaToolchain, ToolchainRequestUnsupported
from composer.certora_env import CertoraEnvironmentError, prover_app
from composer.diagnostics.timing import (
    RunSummary,
    install_run_summary,
    set_current_task_id,
)
from composer.prover.core import ProverOptions, make_prover_options
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceFields
from composer.spec.cvlr import conf as cvlr_conf
from composer.spec.cvlr.crates import resolve
from composer.spec.cvlr.prover import Submission
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
# CVLR source resolution (§5.5)
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

#: The public examples' ``Default.conf``, abridged. Both of its JSON5-isms are load-bearing here:
#: it would not survive ``json.loads``.
_REAL_CONF = """\
{
    // the rules this project means to check
    "rule": ["rule_correct_add", "rule_vacuous"],
    "prover_args": [
        "-solanaTACOptimize 0",
        "-unsatCoresForAllAsserts true",
    ],
    "loop_iter": 3,
    "rule_sanity": "basic",
}
"""


def test_a_real_conf_parses_despite_comments_and_trailing_commas():
    parsed = cvlr_conf.parse_conf(_REAL_CONF)
    assert parsed["rule"] == ["rule_correct_add", "rule_vacuous"]
    assert parsed["rule_sanity"] == "basic"


def test_an_integer_conf_value_stays_a_string():
    """``certoraUtils.read_conf_file`` reads ints as strings, which is why confs in the wild say
    ``"loop_iter": "1"``. Round-tripping a project's conf must not silently retype its values."""
    assert cvlr_conf.parse_conf(_REAL_CONF)["loop_iter"] == "3"


def test_a_conf_that_sets_a_key_twice_is_rejected():
    with pytest.raises(cvlr_conf.MalformedConf):
        cvlr_conf.parse_conf('{"loop_iter": "1", "loop_iter": "2"}')


def test_the_recommended_starting_point_is_the_base_when_a_project_has_no_conf():
    """Not an empty conf: an empty one has no loop bound, no SMT timeout and no prover flags, which
    verifies differently rather than neutrally."""
    base = cvlr_conf.load_base(None)
    assert base["loop_iter"] == "2"


def test_loops_are_bounded_soundly_and_the_bound_is_raised_instead():
    """A soundness-relevant pair of defaults, so it gets its own test rather than a line in another.

    ``optimistic_loop`` assumes the loop halt conditions hold rather than proving them, so it hides
    any violation reachable only after more iterations. It is a last resort: the intended remedies are
    to bound whatever determines the trip count, or to munge the loop, and only then to raise
    ``loop_iter``. The corpus agrees — of 354 confs across fifteen Solana projects, exactly one sets
    it true.

    The bound is 2, not the template's 1, because 1 is what made an earlier revision reach for
    ``optimistic_loop`` in the first place: with a bound of 1 *any* loop inside a handler fails before
    the rule's own property is reached, measured as VIOLATED on "Unwinding condition in a loop"
    against a loop in an Anchor handler's own borsh path (``docs/cvlr-backend-plan.md`` §7.6.2).
    Raising the bound answers that without assuming anything away.

    A future edit that flips this back should have to delete this test and say why.
    """
    base = cvlr_conf.load_base(None)
    assert base["optimistic_loop"] is False
    assert int(base["loop_iter"]) > 1


def test_the_recommended_starting_point_enables_no_optimistic_solana_flags():
    """The five ``-solanaOptimistic*`` flags are the most-attested block in the whole survey and the
    template sets none of them. A future edit that "restores" them should have to delete this."""
    flags = cvlr_conf.TEMPLATE_BASE["prover_args"]
    assert isinstance(flags, list)
    assert not [f for f in flags if f.startswith("-solanaOptimistic")]


def test_an_overlay_prover_arg_replaces_the_base_setting_of_the_same_flag():
    merged = cvlr_conf.merge_prover_args(
        ["-solanaTACOptimize 0", "-solanaStackSize 8192"], ["-solanaTACOptimize 2"]
    )
    assert merged == ["-solanaTACOptimize 2", "-solanaStackSize 8192"]


def test_a_new_overlay_flag_is_appended_rather_than_replacing_anything():
    merged = cvlr_conf.merge_prover_args(["-solanaTACOptimize 0"], ["-solanaTACMathInt true"])
    assert merged == ["-solanaTACOptimize 0", "-solanaTACMathInt true"]


def _overlay(**kwargs) -> dict:
    return cvlr_conf.solana_conf(
        cvlr_conf.parse_conf(_REAL_CONF),
        cvlr_conf.RunOverlay(build_script="/w/.certora_build/confined_build.py", **kwargs),
    )


def test_the_run_owns_the_build_script_whatever_the_base_says():
    """Fifteen of sixteen surveyed projects name their own build script, and every one of them runs
    the build unconfined inside the prover's process."""
    base = {**cvlr_conf.parse_conf(_REAL_CONF), "build_script": "scripts/certora_build.py"}
    conf = cvlr_conf.solana_conf(base, cvlr_conf.RunOverlay(build_script="/w/ours.py"))
    assert conf["build_script"] == "/w/ours.py"


def test_a_prebuilt_artifact_in_the_base_is_dropped():
    """``run_rust_build`` asserts the context has no ``files`` before a build script may set them,
    so keeping both would fail inside the prover instead of here."""
    base = {**cvlr_conf.parse_conf(_REAL_CONF), "files": ["target/deploy/x.so"]}
    assert "files" not in cvlr_conf.solana_conf(base, cvlr_conf.RunOverlay(build_script="/w/o.py"))


def test_inheriting_rules_keeps_the_projects_own_selection():
    assert _overlay()["rule"] == ["rule_correct_add", "rule_vacuous"]


def test_selecting_rules_replaces_the_projects_selection():
    assert _overlay(rules=cvlr_conf.SelectRules(("rule_vacuous",)))["rule"] == ["rule_vacuous"]


def test_asking_for_all_rules_removes_the_selection_entirely():
    """Distinct from inheriting: against a base naming two of thirty rules, one runs two and the
    other runs thirty."""
    assert "rule" not in _overlay(rules=cvlr_conf.AllRules())


def test_the_env_files_are_left_for_the_build_manifest_to_supply():
    """``cargo certora-sbf`` reads them from ``[package.metadata.certora]`` and
    ``certoraParseBuildScript`` applies them only when the conf has none; setting them here would
    override the project's own declaration with a guess at it."""
    conf = _overlay()
    assert "solana_inlining" not in conf and "solana_summaries" not in conf


def test_the_conf_is_emitted_as_plain_json():
    """JSON5 is what a conf may be written in, not what this writes: a file we emit and re-read is
    the one place a trailing comma buys nothing."""
    assert json.loads(cvlr_conf.dump_conf(_overlay()))["msg"] == ""


def test_the_build_honors_the_tools_version_the_conf_declares():
    """``cargo_tools_version`` only reaches ``cargo certora-sbf`` on the CLI's own build path, and
    this backend owns the build — so unless it is read here the project's declaration is inert."""
    submission = Submission(
        manifest_path=Path("/w/programs/lend/Cargo.toml"),
        base_conf={"cargo_tools_version": "v1.43"},
    )
    assert cvlr_conf.tools_version(submission.base_conf) == "v1.43"


def test_the_certora_feature_is_the_default_only_when_nothing_else_says():
    named = Submission(manifest_path=Path("/w/C.toml"), base_conf={}, features=("verify",))
    from_conf = Submission(
        manifest_path=Path("/w/C.toml"), base_conf={"cargo_features": ["certora", "mocks"]}
    )
    bare = Submission(manifest_path=Path("/w/C.toml"), base_conf={})
    assert named.resolved_features() == ("verify",)
    assert from_conf.resolved_features() == ("certora", "mocks")
    assert bare.resolved_features() == ("certora",)


# --------------------------------------------------------------------------------------------
# the build command and its manifest
# --------------------------------------------------------------------------------------------


def test_the_build_never_touches_rustup():
    """``cargo certora-sbf`` registers a toolchain link around each build, which writes to
    ``RUSTUP_HOME`` — read-only under confinement. Dropping ``--no-rustup`` fails the build for a
    reason that names neither rustup nor the sandbox."""
    assert "--no-rustup" in sbf_argv(manifest_path=Path("/w/Cargo.toml"))


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


def test_the_solana_cli_is_reachable_under_the_name_the_run_selects_it_by():
    """The three CLIs share a ``list[str] -> CertoraRunResult | None`` signature, which is the only
    reason a Solana submission reuses the EVM backend's polling and result parsing unchanged."""
    from composer.certora_env import import_prover_entry

    assert import_prover_entry("solana").__name__ == "run_solana_prover"


def test_a_run_defaults_to_the_evm_prover():
    assert ProverOptions().app == "evm"
    assert make_prover_options(cloud=False, app="solana").app == "solana"


def test_an_unknown_prover_app_is_named_at_the_process_boundary():
    with pytest.raises(CertoraEnvironmentError, match="solanna"):
        prover_app("solanna")


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
