"""The Soroban arm of the CVLR backend: the wasm build contract, the conf, the scaffold, and the
backend/CLI reached through the chain value. No cargo, network or prover: workspace objects are
built directly, and the generated build script runs against a Python stand-in command.
"""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.cargo import wasm
from composer.cargo.metadata import CratePackage, LibTarget, Workspace
from composer.cargo.sbf import PLATFORM_TOOLS_ROOT
from composer.cargo.session import CargoSession
from composer.cargo.wasm import (
    WASM_TARGET,
    WasmIdentity,
    wasm_argv,
    wasm_artifact,
    wasm_manifest,
)
from composer.rustapp.toolchain import PROJECT_TOOLCHAINS
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr import entry
from composer.spec.cvlr.chains import SOLANA_CVLR, SOROBAN_CVLR
from composer.spec.cvlr.conf import (
    RunOverlay,
    SOROBAN_TEMPLATE_BASE,
    SelectRules,
    load_soroban_base,
    soroban_conf,
)
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE, SOROBAN_CVLR_GUIDANCE
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.pipeline import CvlrBackend, _submission
from composer.spec.cvlr.scaffold import (
    ENVS_DIR,
    SOROBAN_SCAFFOLD,
    AppendSection,
    NewFile,
    apply,
    plan_scaffold,
    scaffold_for,
)
from composer.spec.cvlr.verify import HarnessTarget, gate_tools
from composer.spec.cvlr_reference import SOROBAN
from composer.spec.types import PropertyFormulation
from composer.templates.loader import load_jinja_template

CONTRACT = """\
use soroban_sdk::{contract, contractimpl};

#[contract]
pub struct Token;

#[contractimpl]
impl Token {
    pub fn init(_e: soroban_sdk::Env) {}
}
"""

MANIFEST = """\
[package]
name = "token"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
soroban-sdk = "22.0.7"
"""


def _project(
    root: Path,
    *,
    manifest: str = MANIFEST,
    sdk_version: str | None = "22.0.7",
) -> tuple[Workspace, CratePackage]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    for path, contents in (
        (root / "Cargo.toml", manifest),
        (root / "src" / "lib.rs", CONTRACT),
    ):
        if not path.exists():
            path.write_text(contents)
    package = CratePackage(
        name="token",
        version="0.1.0",
        manifest_path=root / "Cargo.toml",
        lib=LibTarget(name="token", src_path=root / "src" / "lib.rs", crate_types=("cdylib",)),
        features=(),
        source=None,
    )
    resolved = [package]
    if sdk_version is not None:
        resolved.append(
            CratePackage(
                name="soroban-sdk",
                version=sdk_version,
                manifest_path=root / "vendor" / "soroban-sdk" / "Cargo.toml",
                lib=None,
                features=(),
                source="registry+https://github.com/rust-lang/crates.io-index",
            )
        )
    workspace = Workspace(
        root=root,
        target_directory=root / "target",
        members=(package,),
        packages=tuple(resolved),
    )
    return workspace, package


# ---------------------------------------------------------------------------------------------
# The wasm build


def test_wasm_argv_names_the_release_wasm_target():
    argv = wasm_argv(manifest_path=Path("x/Cargo.toml"), features=("certora", "unit_a"))
    assert argv[:5] == ["rustc", "--lib", "--target", WASM_TARGET, "--release"]
    assert argv[argv.index("--features") + 1] == "certora unit_a"


def test_wasm_argv_hands_the_link_flag_to_rustc_after_the_features():
    """Current rustc no longer passes ``--allow-undefined`` to wasm-ld, so without it the
    ``CVT_*`` intrinsics fail the link instead of becoming the ``env`` imports the prover reads
    After ``--`` it is rustc's, and only the contract's own link sees it."""
    argv = wasm_argv(manifest_path=Path("x/Cargo.toml"), features=("certora",))
    split = argv.index("--")
    assert argv[split + 1 :] == list(wasm.LINK_ARGS) == ["-C", "link-arg=--allow-undefined"]
    assert argv.index("--features") < split


def _identity(root: Path, package_dir: str = ".") -> WasmIdentity:
    return WasmIdentity(
        workspace_root=root,
        package_dir=Path(package_dir),
        target_directory=root / "target",
        artifact_stem="token",
    )


def test_wasm_manifest_covers_the_package_and_the_workspace_manifests(tmp_path):
    (tmp_path / "Cargo.lock").write_text("")
    manifest = wasm_manifest(_identity(tmp_path, "contracts/token"))
    assert manifest.project_directory == tmp_path
    assert manifest.executables == f"target/{WASM_TARGET}/release/token.wasm"
    assert "contracts/token/**/*.rs" in manifest.sources
    assert "contracts/token/Cargo.toml" in manifest.sources
    assert "Cargo.toml" in manifest.sources and "Cargo.lock" in manifest.sources
    assert manifest.solana_inlining == () and manifest.solana_summaries == ()


def test_wasm_manifest_for_a_root_package_globs_the_root(tmp_path):
    assert "**/*.rs" in wasm_manifest(_identity(tmp_path)).sources


async def _script_for(tmp_path: Path) -> Path:
    session = CargoSession(workdir=tmp_path, sandbox=SandboxConfig())
    return await wasm.write_build_script(
        session, _identity(tmp_path), manifest_path=tmp_path / "Cargo.toml"
    )


def _rig_command(script: Path, py: str) -> None:
    """Swap the recorded build argv for a Python stand-in, keeping everything else real."""
    command_file = script.with_name(wasm.BUILD_COMMAND_NAME)
    command = json.loads(command_file.read_text())
    command["argv"] = [sys.executable, "-c", py]
    command_file.write_text(json.dumps(command))


@pytest.mark.asyncio
async def test_build_script_prints_what_the_prover_requires(tmp_path):
    script = await _script_for(tmp_path)
    _rig_command(script, "pass")
    result = subprocess.run(
        [sys.executable, str(script), "--json", "-l"], capture_output=True, text=True
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    # certoraParseBuildScript.run_rust_build's required keys, verbatim.
    assert all(k in payload for k in ("success", "project_directory", "sources", "executables"))
    assert payload["success"] is True
    assert payload["executables"].endswith("token.wasm")


@pytest.mark.asyncio
async def test_build_script_puts_the_provers_features_on_the_cargo_side(tmp_path):
    """``--cargo_features`` must land before ``--``: after it, rustc would get ``--features``."""
    script = await _script_for(tmp_path)
    seen = tmp_path / "argv.json"
    _rig_command(script, f"import json, sys; open({str(seen)!r}, 'w').write(json.dumps(sys.argv[1:]))")
    result = subprocess.run(
        [sys.executable, str(script), "--json", "-l", "--cargo_features", "certora"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(seen.read_text()) == ["--features", "certora", "--", *wasm.LINK_ARGS]


@pytest.mark.asyncio
async def test_build_script_reports_a_failed_build_as_failure(tmp_path):
    script = await _script_for(tmp_path)
    _rig_command(script, "raise SystemExit(1)")
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["success"] is False


def test_wasm_artifact_is_under_the_release_target(tmp_path):
    assert wasm_artifact(tmp_path / "target", "token") == (
        tmp_path / "target" / WASM_TARGET / "release" / "token.wasm"
    )


def test_the_soroban_toolchain_is_registered():
    assert "soroban" in PROJECT_TOOLCHAINS


# ---------------------------------------------------------------------------------------------
# The conf


def test_soroban_conf_owns_the_run_keys_and_drops_files():
    base = {
        "files": ["prebuilt.wasm"],
        "build_script": "./their_build.py",
        "precise_bitwise_ops": True,
        "rule": ["their_rule"],
    }
    conf = soroban_conf(base, RunOverlay(build_script=".certora_build/confined_build.py"))
    assert conf["build_script"] == ".certora_build/confined_build.py"
    assert "files" not in conf
    assert conf["precise_bitwise_ops"] is True
    assert conf["rule"] == ["their_rule"]  # InheritRules keeps the base's selection


def test_soroban_conf_selects_rules_and_sanitizes_msg():
    conf = soroban_conf(
        {},
        RunOverlay(
            build_script="b.py", rules=SelectRules(("init_*",)), msg="Deposit & Balance"
        ),
    )
    assert conf["rule"] == ["init_*"]
    assert "&" not in conf["msg"]


def test_soroban_conf_refuses_summaries():
    with pytest.raises(ValueError, match="solana_summaries"):
        soroban_conf({}, RunOverlay(build_script="b.py", summaries=("s.txt",)))


def test_soroban_template_base_carries_no_solana_keys():
    base = load_soroban_base(None)
    assert base == SOROBAN_TEMPLATE_BASE
    assert not any(k.startswith("solana") or k.startswith("cargo_tools") for k in base)
    assert base["precise_bitwise_ops"] is True


# ---------------------------------------------------------------------------------------------
# The scaffold


def _planned_files(plan) -> set[str]:
    return {c.path.as_posix() for c in plan.changes if isinstance(c, NewFile)}


def _appended(plan) -> str:
    return "\n".join(c.contents for c in plan.changes if isinstance(c, AppendSection))


def test_soroban_scaffold_plans_no_solana_furniture(tmp_path):
    workspace, package = _project(tmp_path)
    plan = plan_scaffold(workspace, package, SOROBAN, SOROBAN_SCAFFOLD)
    assert not plan.blocked
    files = _planned_files(plan)
    assert not any(ENVS_DIR.as_posix() in f for f in files), "no tuning-file tree on Soroban"
    appended = _appended(plan)
    assert "[package.metadata.certora]" not in appended
    assert "no-entrypoint" not in appended
    assert "[patch.crates-io" not in appended


def test_soroban_scaffold_pins_the_reference_set_from_crates_io(tmp_path):
    workspace, package = _project(tmp_path)
    plan = plan_scaffold(workspace, package, SOROBAN, SOROBAN_SCAFFOLD)
    appended = _appended(plan)
    assert f'[dependencies.{SOROBAN.chain.name}]\nversion = "={SOROBAN.chain.version}"' in appended
    assert "[dependencies.cvlr-soroban-derive]" in appended
    assert "git =" not in appended
    assert f'[dependencies.cvlr]\nversion = "={SOROBAN.core.version}"' in appended
    assert f'certora = ["dep:cvlr", "dep:{SOROBAN.chain.name}"' in appended


def test_soroban_scaffold_applies_and_is_idempotent(tmp_path):
    workspace, package = _project(tmp_path)
    plan = plan_scaffold(workspace, package, SOROBAN, SOROBAN_SCAFFOLD)
    assert apply(plan, workspace.root)
    # The manifest still parses, and now declares the feature and the pinned deps.
    parsed = tomllib.loads((tmp_path / "Cargo.toml").read_text())
    assert "certora" in parsed["features"]
    assert parsed["dependencies"]["cvlr-soroban"]["version"] == "=0.4.0"
    assert parsed["dependencies"]["cvlr-soroban"]["optional"] is True
    assert "metadata" not in parsed.get("package", {})
    # Second plan: nothing left to change.
    workspace2, package2 = _project(tmp_path)
    again = plan_scaffold(workspace2, package2, SOROBAN, SOROBAN_SCAFFOLD)
    assert not [c for c in again.changes if not isinstance(c, NewFile)]


def test_soroban_scaffold_turns_off_core_cvlrs_std_default(tmp_path):
    """Core ``cvlr``'s default ``cvlr-nondet/std`` collides with ``soroban-sdk``'s ``panic_impl``
    in the wasm build; a host ``cargo check`` never sees it."""
    workspace, package = _project(tmp_path)
    assert apply(plan_scaffold(workspace, package, SOROBAN, SOROBAN_SCAFFOLD), workspace.root)
    deps = tomllib.loads((tmp_path / "Cargo.toml").read_text())["dependencies"]
    assert deps[SOROBAN.core.name]["default-features"] is False
    # Only the core: cvlr-soroban's defaults are empty, and upstream leaves them on.
    assert "default-features" not in deps[SOROBAN.chain.name]


def test_soroban_workspace_pin_also_turns_off_core_cvlrs_std_default(tmp_path):
    """Cargo ignores a member's ``default-features = false`` unless the workspace entry agrees."""
    workspace, package = _project(tmp_path, manifest="[workspace]\n" + MANIFEST)
    assert apply(plan_scaffold(workspace, package, SOROBAN, SOROBAN_SCAFFOLD), workspace.root)
    parsed = tomllib.loads((tmp_path / "Cargo.toml").read_text())
    assert parsed["workspace"]["dependencies"][SOROBAN.core.name]["default-features"] is False
    assert "default-features" not in parsed["workspace"]["dependencies"][SOROBAN.chain.name]
    assert parsed["dependencies"][SOROBAN.core.name]["workspace"] is True


def test_soroban_platform_gate_refuses_the_wrong_sdk_generation(tmp_path):
    workspace, package = _project(tmp_path, sdk_version="26.1.0")
    plan = plan_scaffold(workspace, package, SOROBAN, SOROBAN_SCAFFOLD)
    assert plan.blocked
    assert "soroban-sdk" in plan.blocked[0].problem


def test_scaffold_for_knows_both_chains_and_refuses_others():
    assert scaffold_for("soroban") is SOROBAN_SCAFFOLD
    assert scaffold_for("solana").chain == "solana"
    with pytest.raises(KeyError):
        scaffold_for("sui")


# ---------------------------------------------------------------------------------------------
# The backend and CLI, reached through the chain value


def _prompts(chain, component=None):
    props = [PropertyFormulation(title="p", sort="invariant", description="supply is conserved")]
    bound = chain.prompts(
        component, props, program="token", module="unit_a", cvlr_versions="cvlr 0.4.2",
        package_root=Path("/nonexistent"),
    )
    return {
        name: " ".join(getattr(bound, name).render_to(load_jinja_template).split())
        for name in ("author_system", "author_task", "judge_system", "judge_task")
    }


def test_the_soroban_prompts_name_the_soroban_rule_attribute():
    # `cvlr`'s own `#[rule]` does not emit the `certora_rules` section certoraSorobanProver reads
    # rules from, so a harness using it compiles and then has nothing to check.
    prompts = _prompts(SOROBAN_CVLR)
    assert "use cvlr_soroban_derive::rule;" in prompts["author_system"]
    assert "Certora Soroban Prover" in prompts["author_system"]
    assert "Soroban contract `token`" in prompts["judge_task"]


def test_the_soroban_prompts_offer_no_tool_a_soroban_run_does_not_bind():
    # No tuning files (so no summaries) and no editor charter yet (so no program edits): a prompt
    # that names either sends the author after a tool that is not there.
    for name, text in _prompts(SOROBAN_CVLR).items():
        assert "summarize_for_prover" not in text, name
        assert "code_editor" not in text, name
        assert "revert_munge" not in text, name


def test_the_soroban_author_is_steered_off_the_assert_false_rejection_form():
    # This backend's conf runs the vacuity check, which reports a rule that can only end inside a
    # failing call as vacuous — so the spelling Certora's own panics confs use comes back failed here.
    prompts = _prompts(SOROBAN_CVLR)
    assert "cvlr_assert!(authorized)" in prompts["author_system"]
    assert "cvlr_assert!(false)" in prompts["judge_system"]


def test_the_solana_prompts_are_still_solanas():
    prompts = _prompts(SOLANA_CVLR)
    assert "Certora Solana Prover" in prompts["author_system"]
    assert "summarize_for_prover" in prompts["author_system"]
    assert "Solana program `token`" in prompts["judge_task"]


def test_the_explorer_is_not_told_the_program_is_solidity():
    # The shared source-tools partial defaults its language to Solidity; the CVLR author prompt
    # included it without saying otherwise, on both chains' account.
    for chain in (SOLANA_CVLR, SOROBAN_CVLR):
        text = _prompts(chain)["author_system"]
        assert "The Rust language itself" in text
        assert "Solidity" not in text


def test_the_backend_takes_its_guidance_and_cache_keys_from_the_chain():
    def backend(chain):
        return CvlrBackend(
            chain=chain, artifact_store=None, prover_opts=None, sandbox=None,  # type: ignore[arg-type]
            cex_analysis=None,  # type: ignore[arg-type]
        )

    soroban, solana = backend(SOROBAN_CVLR), backend(SOLANA_CVLR)
    assert soroban.backend_guidance is SOROBAN_CVLR_GUIDANCE
    assert solana.backend_guidance is SOLANA_CVLR_GUIDANCE
    # Separate keys: the two analyses are different models, and a shared key would replay one as
    # the other.
    assert soroban.analysis_spec.analysis_key != solana.analysis_spec.analysis_key


def _deps(chain, tmp_path, *, target_directory=None):
    project = tmp_path / "project"
    return SimpleNamespace(
        chain=chain,
        package_dir=Path("contracts/token"),
        preflight=SimpleNamespace(
            package="token",
            artifact_stem="token",
            workspace_root=project,
            target_directory=target_directory or project / "target",
        ),
    )


def test_a_soroban_submission_is_a_wasm_build_of_the_tree_with_no_summaries(tmp_path):
    tree = tmp_path / "project" / ".cvlr_work" / "build"
    sub = _submission(
        _deps(SOROBAN_CVLR, tmp_path),  # type: ignore[arg-type]
        SimpleNamespace(workdir=tree),  # type: ignore[arg-type]
        HarnessModule("mint"), "Mint",
    )
    assert sub.summaries == ()
    assert "solana_inlining" not in sub.base_conf
    ident = sub.wasm_identity
    assert ident is not None
    # The build runs in the tree, so the target directory moves with it.
    assert ident.workspace_root == tree
    assert ident.target_directory == tree / "target"
    assert ident.package_dir == Path("contracts/token")
    assert sub.manifest_path == tree / "contracts/token/Cargo.toml"


def test_a_target_directory_outside_the_project_stays_where_it_is(tmp_path):
    elsewhere = tmp_path / "shared-target"
    sub = _submission(
        _deps(SOROBAN_CVLR, tmp_path, target_directory=elsewhere),  # type: ignore[arg-type]
        SimpleNamespace(workdir=tmp_path / "tree"),  # type: ignore[arg-type]
        HarnessModule("mint"), "Mint",
    )
    assert sub.wasm_identity is not None
    assert sub.wasm_identity.target_directory == elsewhere


def test_a_solana_submission_still_names_its_units_summaries(tmp_path):
    sub = _submission(
        _deps(SOLANA_CVLR, tmp_path),  # type: ignore[arg-type]
        SimpleNamespace(workdir=tmp_path / "tree"),  # type: ignore[arg-type]
        HarnessModule("mint"), "Mint",
    )
    assert sub.wasm_identity is None
    assert len(sub.summaries) == 1


def test_a_target_without_tuning_files_binds_no_summary_tool():
    target = SimpleNamespace(tuning=None)
    names = [t.name for t in gate_tools(target, SimpleNamespace())]  # type: ignore[arg-type]
    assert names == ["cargo_check", "verify_rules"]


@pytest.mark.asyncio
async def test_staging_refuses_summaries_a_target_has_nowhere_to_put(tmp_path):
    target = HarnessTarget(
        session=None, module_path=tmp_path / "m.rs", package="token",  # type: ignore[arg-type]
        package_root=tmp_path, tuning=None, unit=None, tree=None,  # type: ignore[arg-type]
        build_sem=None,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="reads none"):
        await target.stage("", summaries=[object()])  # type: ignore[list-item]


def test_the_soroban_cli_asks_for_a_contract():
    parser = entry.build_parser("soroban")
    assert "Soroban Prover" in (parser.description or "")
    args = parser.parse_args(["/proj", "contracts/token/src/lib.rs:Token"])
    assert args.main_contract == "contracts/token/src/lib.rs:Token"


def test_a_package_name_passed_as_a_soroban_contract_gets_the_soroban_repair():
    # The Solana repair (underscore the package name) is wrong here: a contract is named by its
    # contract type, which no package name spells.
    with pytest.raises(ValueError, match="cannot name a contract") as e:
        entry.parse_main_program("src/lib.rs:soroban-token", "soroban")
    assert "#[contract]" in str(e.value)
    assert "src/lib.rs:soroban_token" not in str(e.value)


def test_a_soroban_build_is_not_granted_platform_tools(monkeypatch):
    monkeypatch.delenv("COMPOSER_SANDBOX_PROVIDER", raising=False)
    assert PLATFORM_TOOLS_ROOT not in entry.build_confinement("soroban").extra_ro
    assert PLATFORM_TOOLS_ROOT in entry.build_confinement("solana").extra_ro
