"""The project build an external run mode gets in place of AutoSetup, and what the prover backend
does for such a run instead of harness creation.

Covers each layer with fakes: ``Autosetup.build`` over a fake compilation analysis, the
``--build-only`` flag, the composer wrapper over a stand-in subprocess, and the prover backend's
``prepare_system`` / ``prepare_external_mode``. No solc, certoraRun, LLM or prover runs.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import composer.spec.source.autosetup as autosetup_mod
import composer.spec.source.pipeline as pipeline_mod
from certora_autosetup.autosetup.autosetup import Autosetup
from certora_autosetup.autosetup.cli_args import create_parser
from certora_autosetup.autosetup.types import AutosetupConfig, solc_versions_of
from certora_autosetup.setup.setup_prover import CompilationAnalysisError
from certora_autosetup.utils.types import ContractHandle
from composer.pipeline.modes import ModeSupport, ProjectBuild
from composer.pipeline.run_mode import RunMode
from composer.prover.core import ProverOptions
from composer.spec.source.artifacts import ProverArtifactStore
from composer.spec.source.autosetup import BuildSuccess, SetupFailure, run_autosetup_build
from composer.spec.source.pipeline import ExternalModePrepared, ProverBackend
from composer.spec.system_model import SourceApplication, SourceExplicitContract


# ---------------------------------------------------------------------------
# certora_autosetup: Autosetup.build and --build-only
# ---------------------------------------------------------------------------


class _FakeSetupProver:
    """Records what ``build`` asks of the compilation analysis and answers with ``outcome``."""

    def __init__(self, outcome: tuple[bool, dict, bool, list[ContractHandle]]):
        self.outcome = outcome
        self.calls: list[tuple[list[ContractHandle], str]] = []
        self.full_setup_called = False

    def run_compilation_analysis(self, contracts, main_contract):
        self.calls.append((list(contracts), main_contract))
        return self.outcome

    def setup_prover(self, *_a, **_kw):
        self.full_setup_called = True
        raise AssertionError("build must stop after the compilation analysis")


def _autosetup(tmp_path: Path, setup_prover: _FakeSetupProver, handles: list[ContractHandle]) -> Autosetup:
    config = AutosetupConfig(
        project_root=tmp_path, certora_dir=tmp_path / "certora", script_dir=tmp_path,
        reports_dir=tmp_path / "reports", orchestration_timestamp="t",
    )
    a = Autosetup(
        config=config, setup_prover=setup_prover, prover_runner=None, config_manager=None,  # type: ignore[arg-type]
        scope=None, signature_manager=None, rule_generator=None, contract_handles=handles,  # type: ignore[arg-type]
    )
    # No build config on disk: the detected build system contributes no settings, so the
    # result holds exactly what the compilation analysis produced.
    a.setup_build_system_config = lambda: None  # type: ignore[method-assign]
    return a


def test_build_returns_the_compilation_settings_and_stops(tmp_path):
    vault = ContractHandle(source_file="src/Vault.sol", contract_name="Vault")
    token = ContractHandle(source_file="src/Token.sol", contract_name="Token")
    settings = {
        "compiler_map": {"Vault": "solc8.28", "Token": "solc7.6"},
        "packages": ["@oz=lib/oz"],
    }
    fake = _FakeSetupProver((True, settings, True, [vault, token]))
    result = _autosetup(tmp_path, fake, [vault, token]).build(vault)

    assert fake.calls == [([vault, token], "Vault")]
    assert not fake.full_setup_called
    assert result.compilation_config["compiler_map"] == {"Vault": "solc8.28", "Token": "solc7.6"}
    assert result.compilation_config["packages"] == ["@oz=lib/oz"]
    assert "files" not in result.compilation_config and "verify" not in result.compilation_config
    assert result.solc_versions == ["solc7.6", "solc8.28"]
    assert result.import_patcher_applied
    assert result.contracts == ["src/Vault.sol", "src/Token.sol"]
    assert json.loads(json.dumps(result.to_json()))["solc_versions"] == ["solc7.6", "solc8.28"]


def test_build_raises_when_the_project_does_not_compile(tmp_path):
    vault = ContractHandle(source_file="src/Vault.sol", contract_name="Vault")
    fake = _FakeSetupProver((False, {}, False, [vault]))
    with pytest.raises(CompilationAnalysisError):
        _autosetup(tmp_path, fake, [vault]).build(vault)


def test_solc_versions_of_reads_the_scalar_and_the_map():
    assert solc_versions_of({"solc": "solc8.20"}) == ["solc8.20"]
    assert solc_versions_of({"compiler_map": {"A": "solc8.20", "B": "solc8.20"}}) == ["solc8.20"]
    assert solc_versions_of({}) == []


def test_the_build_only_flag_takes_an_output_path():
    args = create_parser().parse_args(["--main-contract", "src/V.sol:V", "--build-only", "out.json"])
    assert args.build_only == "out.json"
    assert create_parser().parse_args(["--main-contract", "src/V.sol:V"]).build_only is None


# ---------------------------------------------------------------------------
# composer: run_autosetup_build
# ---------------------------------------------------------------------------


_FAKE_AUTOSETUP = """
import json, sys
mode, out = sys.argv[1], sys.argv[2]
print("detecting build system")
print("compiling", file=sys.stderr)
if mode == "ok":
    json.dump({"compilation_config": {"solc": "solc8.28"}, "solc_versions": ["solc8.28"],
               "import_patcher_applied": False, "contracts": ["src/V.sol"]}, open(out, "w"))
    sys.exit(0)
print("ParserError: Source not found", file=sys.stderr)
sys.exit(1)
"""


def _fake_args(mode: str, seen: list[tuple]):
    def fake(relative_path, main_contract, prover_opts, extra_files, *mode_args):
        seen.append((relative_path, main_contract, list(extra_files), mode_args))
        assert mode_args[0] == "--build-only"
        return [sys.executable, "-c", _FAKE_AUTOSETUP, mode, mode_args[1]]
    return fake


@pytest.mark.asyncio
async def test_run_autosetup_build_reads_the_settings_and_logs_the_output(tmp_path, monkeypatch):
    seen: list[tuple] = []
    events: list[dict] = []
    monkeypatch.setattr(autosetup_mod, "_autosetup_args", _fake_args("ok", seen))
    monkeypatch.setattr(autosetup_mod, "emit_custom_event", events.append)
    log = tmp_path / "logs" / "build.log"

    result = await run_autosetup_build(
        tmp_path, "src/V.sol", "V", ProverOptions(app="evm"), ["src/T.sol:T"], log,
    )
    assert isinstance(result, BuildSuccess)
    assert result.compilation_config == {"solc": "solc8.28"}
    assert seen[0][:3] == ("src/V.sol", "V", ["src/T.sol:T"])
    # Both streams land in the log.
    text = log.read_text()
    assert "detecting build system" in text and "compiling" in text
    # The output still streams to the UI as AutoSetup output does.
    assert {"type": "auto_setup_output", "line": "detecting build system"} in events
    assert events[-1] == {"type": "auto_setup_complete", "return_code": 0}


@pytest.mark.asyncio
async def test_run_autosetup_build_reports_a_failed_build(tmp_path, monkeypatch):
    monkeypatch.setattr(autosetup_mod, "_autosetup_args", _fake_args("fail", []))
    monkeypatch.setattr(autosetup_mod, "emit_custom_event", lambda _e: None)
    result = await run_autosetup_build(
        tmp_path, "src/V.sol", "V", ProverOptions(app="evm"), [], tmp_path / "build.log",
    )
    assert isinstance(result, SetupFailure)
    assert result.stderr is not None and "Source not found" in result.stderr


# ---------------------------------------------------------------------------
# The prover backend in an external mode
# ---------------------------------------------------------------------------


def _contract(ident: str, path: str) -> SourceExplicitContract:
    return SourceExplicitContract(
        sort="singleton", name=ident, solidity_identifier=ident, description="d",
        components=[], path=path,
    )


_APP = SourceApplication(
    application_type="vault", description="d",
    components=[_contract("Vault", "src/Vault.sol"), _contract("Token", "src/Token.sol")],
)


class _Source:
    contract_name = "Vault"
    relative_path = "src/Vault.sol"

    def __init__(self, root: Path) -> None:
        self.project_root = str(root)


class _Run:
    def __init__(self, root: Path, run_mode: RunMode) -> None:
        self.source = _Source(root)
        self.run_mode = run_mode
        self.tasks: list[Any] = []

    async def runner(self, task_info, job):
        self.tasks.append(task_info)
        return await job()


def _backend(root: Path, opts: ProverOptions) -> ProverBackend:
    return ProverBackend(
        artifact_store=ProverArtifactStore(str(root), "Vault"), _prover_opts=opts,
        editing=None, analysis_store=None,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_an_external_mode_skips_harness_creation_and_builds_the_project(tmp_path, monkeypatch):
    async def no_harness(*_a, **_kw):
        raise AssertionError("an external mode must not run the harness agent")

    builds: list[tuple] = []

    async def fake_build(project_root, relative_path, main_contract, prover_opts, extra_files, log_path):
        builds.append((relative_path, main_contract, list(extra_files), log_path))
        return BuildSuccess(
            compilation_config={"solc": "solc8.28"}, solc_versions=["solc8.28"],
            import_patcher_applied=True, contracts=["src/Vault.sol", "src/Token.sol"],
        )

    monkeypatch.setattr(pipeline_mod, "run_harness_creation", no_harness)
    monkeypatch.setattr(pipeline_mod, "run_autosetup_build", fake_build)
    opts = ProverOptions(app="evm", server="staging")
    run = _Run(tmp_path, RunMode.EVALUATE)

    prepared = await _backend(tmp_path, opts).prepare_system(_APP, run, None)  # type: ignore[arg-type]
    assert isinstance(prepared, ExternalModePrepared)
    assert prepared.main.contract.solidity_identifier == "Vault"
    assert prepared.main.contract.harnesses == []
    assert run.tasks == []

    support = await prepared.prepare_external_mode(run)  # type: ignore[arg-type]
    assert isinstance(support, ModeSupport)
    assert support.prover_options is opts
    assert support.build == ProjectBuild(
        succeeded=True, compilation_config={"solc": "solc8.28"}, solc_versions=["solc8.28"],
        sources_patched=True, log_path=tmp_path / pipeline_mod.PROJECT_BUILD_LOG,
    )
    # Every other contract the analysis found is in the build's scene, named explicitly.
    [(rel, main, extra, _log)] = builds
    assert (rel, main, extra) == ("src/Vault.sol", "Vault", ["src/Token.sol:Token"])
    [task] = run.tasks
    assert task.task_id == "project-build"

    with pytest.raises(RuntimeError, match="does not formalize"):
        await prepared.prepare_formalization(run)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failed_build_becomes_a_failed_project_build(tmp_path, monkeypatch):
    async def failing_build(*_a, **_kw):
        return SetupFailure(error="AutoSetup build failed (exit 1)", stderr="Source not found")

    monkeypatch.setattr(pipeline_mod, "run_autosetup_build", failing_build)
    run = _Run(tmp_path, RunMode.EVALUATE)
    prepared = await _backend(tmp_path, ProverOptions(app="evm")).prepare_system(_APP, run, None)  # type: ignore[arg-type]
    support = await prepared.prepare_external_mode(run)  # type: ignore[arg-type]

    assert support.build is not None and not support.build.succeeded
    assert support.build.error == "AutoSetup build failed (exit 1)\nSource not found"
    assert support.build.compilation_config == {}


# ---------------------------------------------------------------------------
# ArtifactStore.write_deliverable
# ---------------------------------------------------------------------------


def test_write_deliverable_writes_under_the_deliverable_dir(tmp_path):
    store = ProverArtifactStore(str(tmp_path), "Vault")
    rel = store.write_deliverable("evaluate/confs/a.conf", '{"verify": "Vault:a.spec"}')
    assert rel == Path("certora/evaluate/confs/a.conf")
    assert (tmp_path / rel).read_text() == '{"verify": "Vault:a.spec"}'

    rel_bin = store.write_deliverable(Path("evaluate/report.pdf"), b"%PDF-1.7")
    assert (tmp_path / rel_bin).read_bytes() == b"%PDF-1.7"

    # Replaces a leftover.
    store.write_deliverable("evaluate/confs/a.conf", "{}")
    assert (tmp_path / rel).read_text() == "{}"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.txt", "a/../../b", ""])
def test_write_deliverable_refuses_paths_outside_the_deliverable_dir(tmp_path, bad):
    store = ProverArtifactStore(str(tmp_path), "Vault")
    with pytest.raises(ValueError, match="must be relative"):
        store.write_deliverable(bad, "x")
