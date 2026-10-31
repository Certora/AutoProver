"""The Soroban arm of the CVLR backend: the wasm build contract, the conf, the scaffold, and the
backend/CLI reached through the chain value. No cargo, network or prover: workspace objects are
built directly, and the generated build script runs against a Python stand-in command.
"""

import asyncio
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.cargo import wasm
from composer.cargo.metadata import CratePackage, LibTarget, Workspace
from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, build_command_path
from composer.cargo.session import CargoSession, HostCheck
from composer.cargo.wasm import WASM_TARGET, WasmBuild, WasmIdentity, wasm_artifact, wasm_manifest
from composer.prover.conf import SelectRules
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr import entry
from composer.spec.cvlr.chains import SOLANA_CVLR, SOROBAN_CVLR
from composer.spec.cvlr.conf import (
    BASE_CONF,
    SOROBAN_BASE_CONF,
    RunOverlay,
    TunableConf,
    soroban_conf,
    soroban_tunable_conf,
)
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE, SOROBAN_CVLR_GUIDANCE
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.manifest import (
    ManifestCorrupt,
    ManifestMissing,
    rule_names_in,
    rule_names_in_wasm,
)
from composer.spec.cvlr.pipeline import CvlrBackend, CvlrFormalizer
from composer.spec.cvlr.preflight import session_settings
from composer.spec.cvlr.reference import SOROBAN, SOROBAN_V22, SOROBAN_V25, generations
from composer.spec.cvlr.scaffold import (
    ENVS_DIR,
    SOROBAN_SCAFFOLD,
    Write,
    apply,
    plan_scaffold,
    scaffold_for,
    select_generation,
)
from composer.spec.cvlr.verify import BuildPermit, HarnessTarget, gate_tools
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
soroban-sdk = "22.0.8"
"""


def _project(
    root: Path,
    *,
    manifest: str = MANIFEST,
    sdk_version: str | None = "22.0.8",
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
    links: dict[CratePackage, tuple[CratePackage, ...]] = {}
    if sdk_version is not None:
        sdk = CratePackage(
            name="soroban-sdk",
            version=sdk_version,
            manifest_path=root / "vendor" / "soroban-sdk" / "Cargo.toml",
            lib=None,
            features=(),
            source="registry+https://github.com/rust-lang/crates.io-index",
        )
        resolved.append(sdk)
        # The generation gate reads what the contract links, not every resolved package.
        links[package] = (sdk,)
    workspace = Workspace(
        root=root,
        target_directory=root / "target",
        members=(package,),
        packages=tuple(resolved),
        links=links,
    )
    return workspace, package


# ---------------------------------------------------------------------------------------------
# The wasm build


def _identity(root: Path, package_dir: str = ".") -> WasmIdentity:
    return WasmIdentity(
        workspace_root=root,
        package_dir=Path(package_dir),
        target_directory=root / "target",
        artifact_stem="token",
    )


def _build(root: Path, features: tuple[str, ...] = ()) -> WasmBuild:
    return WasmBuild(
        manifest_path=root / "Cargo.toml", identity=_identity(root), features=features
    )


def test_wasm_argv_names_the_release_wasm_target(tmp_path):
    argv = _build(tmp_path, ("certora", "unit_a")).argv()
    assert argv[:6] == ["cargo", "rustc", "--lib", "--target", WASM_TARGET, "--release"]
    assert argv[argv.index("--features") + 1] == "certora unit_a"


def test_the_target_is_the_one_cvlr_declares_its_env_imports_for():
    # CVLR gates `wasm_import_module = "env"` on `target_os = "none"`, which only this target has.
    assert WASM_TARGET == "wasm32v1-none"


def test_wasm_argv_hands_the_link_flag_to_rustc_after_the_features(tmp_path):
    """One symbol still needs ``--allow-undefined``. After ``--`` it is rustc's, and only the
    contract's own link sees it."""
    argv = _build(tmp_path, ("certora",)).argv()
    split = argv.index("--")
    assert argv[split + 1 :] == list(wasm.LINK_ARGS) == ["-C", "link-arg=--allow-undefined"]
    assert argv.index("--features") < split


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
    into = tmp_path / "submissions"
    into.mkdir()
    return await wasm.write_build_script(session, _build(tmp_path), into=into, name="unit_a")


def _rig_command(script: Path, py: str) -> None:
    """Swap the recorded build argv for a Python stand-in, keeping everything else real."""
    command_file = build_command_path(script)
    command = json.loads(command_file.read_text())
    command["argv"] = [sys.executable, "-c", py]
    command_file.write_text(json.dumps(command))


@pytest.mark.asyncio
async def test_the_script_and_its_command_file_are_named_for_the_submission(tmp_path):
    script = await _script_for(tmp_path)
    assert script == (tmp_path / "submissions" / "unit_a.py").resolve()
    assert build_command_path(script).is_file()


@pytest.mark.asyncio
async def test_build_script_prints_what_the_prover_requires(tmp_path):
    script = await _script_for(tmp_path)
    _rig_command(script, "pass")
    result = subprocess.run(
        [sys.executable, str(script), "--json", "-l"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
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


def test_a_soroban_session_checks_on_the_host_and_follows_the_projects_toolchain():
    assert session_settings("soroban") == (None, HostCheck())


# ---------------------------------------------------------------------------------------------
# The rule list a wasm build carries


def _leb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _custom(name: str, payload: bytes) -> bytes:
    body = _leb(len(name)) + name.encode() + payload
    return b"\x00" + _leb(len(body)) + body


def _module(*sections: bytes) -> bytes:
    # A type section (id 1, empty vector) in front, so the reader has to skip a non-custom one.
    return b"\x00asm\x01\x00\x00\x00" + b"\x01\x01\x00" + b"".join(sections)


#: What ``cvlr-log`` links into every program: the ``.cvlr_rules`` anchor record and nothing else.
_ANCHOR_ONLY = b"\x00" + (3).to_bytes(4, "little") + b"CV1"


def test_the_soroban_rule_list_is_read_from_certora_rules(tmp_path):
    """``cvlr_soroban_derive``'s ``#[rule]`` writes NUL-terminated names into ``certora_rules``,
    the section certoraSorobanProver reads. ``.cvlr_rules`` holds only the anchor on a Soroban
    build, so reading it would list no rules and every submission would be refused."""
    wasm_file = tmp_path / "token.wasm"
    wasm_file.write_bytes(
        _module(
            _custom("certora_rules", b"mint_conserves\0burn_auth\0"),
            _custom(".cvlr_rules", _ANCHOR_ONLY),
        )
    )
    assert rule_names_in_wasm(wasm_file) == {"mint_conserves", "burn_auth"}
    # By suffix, so the gate needs no chain of its own.
    assert rule_names_in(wasm_file) == {"mint_conserves", "burn_auth"}


def test_a_wasm_without_certora_rules_did_not_list_its_rules(tmp_path):
    wasm_file = tmp_path / "token.wasm"
    wasm_file.write_bytes(_module(_custom(".cvlr_rules", _ANCHOR_ONLY)))
    with pytest.raises(ManifestMissing):
        rule_names_in_wasm(wasm_file)


def test_a_truncated_certora_rules_section_is_not_trusted(tmp_path):
    wasm_file = tmp_path / "token.wasm"
    wasm_file.write_bytes(_module(_custom("certora_rules", b"mint_conserves\0burn")))
    with pytest.raises(ManifestCorrupt):
        rule_names_in_wasm(wasm_file)


# ---------------------------------------------------------------------------------------------
# The conf


def test_soroban_conf_owns_the_run_keys():
    conf = soroban_conf(TunableConf(), RunOverlay(build_script=Path("/subs/unit_a.py")))
    assert conf["build_script"] == "/subs/unit_a.py"
    assert conf["precise_bitwise_ops"] is True
    assert conf["rule_sanity"] == "basic"
    assert conf["loop_iter"] == "2"


def test_soroban_conf_selects_rules_and_sanitizes_msg():
    conf = soroban_conf(
        TunableConf(),
        RunOverlay(
            build_script=Path("b.py"), rules=SelectRules(("init_*",)), msg="Deposit & Balance"
        ),
    )
    assert conf["rule"] == ["init_*"]
    assert "&" not in conf["msg"]


def test_soroban_conf_refuses_summaries():
    with pytest.raises(ValueError, match="solana_summaries"):
        soroban_conf(
            TunableConf(), RunOverlay(build_script=Path("b.py"), summaries=(Path("s.txt"),))
        )


def test_the_soroban_base_carries_no_solana_keys_or_flags():
    conf = soroban_tunable_conf(TunableConf())
    assert not any(k.startswith("solana") or k.startswith("cargo_tools") for k in conf)
    assert "prover_args" not in conf
    # The shared settings are the same on both chains.
    assert SOROBAN_BASE_CONF["smt_timeout"] == BASE_CONF["smt_timeout"]
    assert SOROBAN_BASE_CONF["rule_sanity"] == BASE_CONF["rule_sanity"]


# ---------------------------------------------------------------------------------------------
# The scaffold


def _planned_files(plan) -> set[str]:
    return {c.path.as_posix() for c in plan.changes if isinstance(c, Write)}


def _scaffolded(tmp_path: Path, reference=SOROBAN_V22, **kw) -> dict:
    workspace, package = _project(tmp_path, **kw)
    plan = plan_scaffold(workspace, package, reference, SOROBAN_SCAFFOLD)
    assert not plan.blocked, plan.blocked
    assert apply(plan, workspace.root)
    return tomllib.loads((tmp_path / "Cargo.toml").read_text())


def test_soroban_scaffold_plans_no_solana_furniture(tmp_path):
    workspace, package = _project(tmp_path)
    plan = plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD)
    assert not plan.blocked
    assert not any(ENVS_DIR.as_posix() in f for f in _planned_files(plan)), "no tuning files"
    parsed = _scaffolded(tmp_path)
    assert "metadata" not in parsed.get("package", {})
    assert "no-entrypoint" not in parsed["features"]["certora"]
    # No forks: the only redirects are the chain crate's own commit pins.
    assert set(parsed["patch"]["crates-io"]) == {"cvlr-soroban", "cvlr-soroban-derive"}


def test_soroban_scaffold_declares_the_releases_and_reads_them_from_the_branch_commit(tmp_path):
    parsed = _scaffolded(tmp_path)
    deps = parsed["dependencies"]
    for crate in SOROBAN_V22.scaffold_crates():
        assert deps[crate.name]["version"] == f"={crate.version}"
        assert deps[crate.name]["optional"] is True
    # The core is a crates.io release; the chain crate and its derive crate are its branch's commit.
    assert "cvlr" not in parsed["patch"]["crates-io"]
    for name in ("cvlr-soroban", "cvlr-soroban-derive"):
        patch = parsed["patch"]["crates-io"][name]
        assert patch["git"] == "https://github.com/Certora/cvlr-soroban.git"
        assert patch["rev"] == SOROBAN_V22.chain_crate.unpublished.rev  # type: ignore[union-attr]


def test_both_soroban_lines_share_solanas_core_release():
    # cvlr 0.6.2 is no_std and declares its env imports, so one core serves every chain.
    assert SOROBAN_V22.core == SOROBAN_V25.core
    assert SOROBAN_V22.core.unpublished is None


def test_soroban_scaffold_is_idempotent(tmp_path):
    _scaffolded(tmp_path)
    workspace, package = _project(tmp_path)
    again = plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD)
    assert again.changes == ()


def test_soroban_scaffold_turns_off_core_cvlrs_std_default(tmp_path):
    """Core ``cvlr``'s default ``cvlr-nondet/std`` collides with ``soroban-sdk``'s ``panic_impl``
    in the wasm build; a host ``cargo check`` never sees it."""
    deps = _scaffolded(tmp_path)["dependencies"]
    assert deps[SOROBAN_V22.core.name]["default-features"] is False
    # Only the core: cvlr-soroban's defaults are empty, and upstream leaves them on.
    assert "default-features" not in deps[SOROBAN_V22.chain_crate.name]


def test_soroban_workspace_pin_also_turns_off_core_cvlrs_std_default(tmp_path):
    """Cargo ignores a member's ``default-features = false`` unless the workspace entry agrees."""
    parsed = _scaffolded(tmp_path, manifest="[workspace]\n" + MANIFEST)
    pins = parsed["workspace"]["dependencies"]
    assert pins[SOROBAN_V22.core.name]["default-features"] is False
    assert "default-features" not in pins[SOROBAN_V22.chain_crate.name]
    assert parsed["dependencies"][SOROBAN_V22.core.name]["workspace"] is True


@pytest.mark.parametrize(
    ("sdk", "expected"),
    [("25.1.1", "25"), ("22.0.8", "22"), ("26.1.0", None), ("23.0.0", None), ("20.5.0", None)],
)
def test_the_line_is_chosen_by_the_sdk_the_contract_links(tmp_path, sdk, expected):
    workspace, package = _project(tmp_path, sdk_version=sdk)
    chosen = select_generation(workspace, package, generations("soroban"))
    lines = {"25": SOROBAN_V25, "22": SOROBAN_V22}
    assert chosen is (lines[expected] if expected else None)


def test_the_newest_line_is_the_one_named_for_callers_without_a_project():
    assert SOROBAN is SOROBAN_V25 is generations("soroban")[0]


def test_a_project_locked_below_the_branchs_patch_is_told_to_update_its_lockfile(tmp_path):
    """Phoenix's case: on SDK 22, locked to 22.0.7, and the 22 branch needs 22.0.8. Cargo would
    report a conflict naming neither; the scaffold names the fix."""
    workspace, package = _project(tmp_path, sdk_version="22.0.7")
    plan = plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD)
    (blocked,) = plan.blocked
    assert "22.0.7" in blocked.problem and "22.0.8" in blocked.problem
    assert "cargo update -p soroban-sdk" in blocked.resolution


def test_soroban_platform_gate_refuses_the_wrong_sdk_generation(tmp_path):
    workspace, package = _project(tmp_path, sdk_version="20.5.0")
    plan = plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD)
    assert plan.blocked
    assert any("soroban-sdk" in b.problem for b in plan.blocked)


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
        component, props, program="token", module="unit_a", cvlr_versions="cvlr 0.6.2",
        caveats=(), conf=soroban_tunable_conf(TunableConf()), package_root=Path("/nonexistent"),
    )
    return {
        name: " ".join(getattr(bound, name).render_to(load_jinja_template).split())
        for name in ("author_system", "author_task", "judge_system", "judge_task")
    }


def test_the_soroban_prompts_name_the_soroban_rule_attribute():
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


def test_the_soroban_author_is_steered_off_the_assert_false_rejection_form():
    # This backend's conf runs the vacuity check, which reports a rule that can only end inside a
    # failing call as vacuous — so the spelling Certora's own panics confs use comes back failed here.
    prompts = _prompts(SOROBAN_CVLR)
    assert "cvlr_assert!(authorized)" in prompts["author_system"]
    assert "cvlr_assert!(false)" in prompts["judge_system"]


def test_the_solana_prompts_are_still_solanas():
    prompts = _prompts(SOLANA_CVLR)
    assert "summarize_for_prover" in prompts["author_system"]
    assert "Solana program `token`" in prompts["judge_task"]


def test_only_solana_wraps_its_prompts_in_the_solana_knowledge_bundle():
    def bound(chain):
        return chain.prompts(
            None, [], program="token", module="unit_a", cvlr_versions="",
            caveats=(), conf={}, package_root=Path("/nonexistent"),
        )

    marker = object()
    assert bound(SOROBAN_CVLR).context(marker) is marker
    assert bound(SOLANA_CVLR).context(marker) is not marker


def _backend(chain):
    return CvlrBackend(
        chain=chain, artifact_store=None, prover_opts=None, sandbox=None,  # type: ignore[arg-type]
        cex_analysis=None, reference=None, editing=None, work_dir=None,  # type: ignore[arg-type]
    )


def test_the_backend_takes_its_guidance_and_cache_keys_from_the_chain():
    soroban, solana = _backend(SOROBAN_CVLR), _backend(SOLANA_CVLR)
    assert soroban.backend_guidance is SOROBAN_CVLR_GUIDANCE
    assert solana.backend_guidance is SOLANA_CVLR_GUIDANCE
    # Separate keys: the two analyses are different models, and a shared key would replay one as
    # the other.
    assert soroban.analysis_spec.analysis_key != solana.analysis_spec.analysis_key


def test_only_solana_hoists_shared_properties_into_a_protocol_unit():
    assert _backend(SOROBAN_CVLR).shared_unit is None
    assert _backend(SOLANA_CVLR).shared_unit is not None


def _formalizer(chain, tree: Path, *, target_directory: Path | None = None):
    project = tree.parent / "project"
    deps = SimpleNamespace(
        chain=chain,
        package_dir=Path("contracts/token"),
        preflight=SimpleNamespace(
            package="token",
            artifact_stem="token",
            workspace_root=project,
            target_directory=target_directory or project / "target",
        ),
    )
    return SimpleNamespace(deps=deps, build=SimpleNamespace(session=SimpleNamespace(workdir=tree)))


def test_a_soroban_submission_is_a_wasm_build_of_the_tree_with_no_summaries(tmp_path):
    tree = tmp_path / "build"
    sub = CvlrFormalizer._submission(
        _formalizer(SOROBAN_CVLR, tree),  # type: ignore[arg-type]
        HarnessModule("mint"), "Mint", has_tuning=False,
    )
    assert sub.summaries == ()
    ident = sub.wasm
    assert ident is not None
    # The build runs in the tree, so a target directory inside the project moves with it.
    assert ident.workspace_root == tree
    assert ident.target_directory == tree / "target"
    assert ident.package_dir == Path("contracts/token")
    assert sub.manifest_path == tree / "contracts/token/Cargo.toml"


def test_a_target_directory_outside_the_project_stays_where_it_is(tmp_path):
    elsewhere = tmp_path / "shared-target"
    sub = CvlrFormalizer._submission(
        _formalizer(SOROBAN_CVLR, tmp_path / "build", target_directory=elsewhere),  # type: ignore[arg-type]
        HarnessModule("mint"), "Mint", has_tuning=False,
    )
    assert sub.wasm is not None
    assert sub.wasm.target_directory == elsewhere


def test_a_solana_submission_still_names_its_units_summaries(tmp_path):
    sub = CvlrFormalizer._submission(
        _formalizer(SOLANA_CVLR, tmp_path / "build"),  # type: ignore[arg-type]
        HarnessModule("mint"), "Mint", has_tuning=True,
    )
    assert sub.wasm is None
    assert len(sub.summaries) == 1


def test_a_target_without_tuning_files_binds_no_summary_tool():
    target = SimpleNamespace(tuning=None)
    names = [t.name for t in gate_tools(target, SimpleNamespace(), None)]  # type: ignore[arg-type]
    assert "summarize_for_prover" not in names
    assert {"cargo_check", "verify_rules", "adjust_prover_config"} <= set(names)


@pytest.mark.asyncio
async def test_staging_refuses_summaries_a_target_has_nowhere_to_put(tmp_path):
    target = HarnessTarget(
        session=None, package="token", package_root=tmp_path,  # type: ignore[arg-type]
        tuning=None, unit=None, tree=None, build_sem=asyncio.Semaphore(1),  # type: ignore[arg-type]
    )
    permit = BuildPermit(target)
    await permit.acquire()
    try:
        with pytest.raises(ValueError, match="reads none"):
            await permit.stage("", summaries=[object()])  # type: ignore[list-item]
    finally:
        permit.release()


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
