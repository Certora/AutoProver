"""Tests for Truffle build-system support.

Covers the shapes Truffle-era repos (0.4.x/0.5.x) actually ship: both config names,
`truffle-config.js` and Truffle v4's `truffle.js`; the pinned compiler settings read out of
them; and the `@scope/pkg/contracts/...` package roots those projects import through, which
in a monorepo hang off one config per app.
"""

import json
import shutil
from pathlib import Path

import pytest

from certora_autosetup.build_systems.truffle import TruffleManager
from certora_autosetup.parsers.build_system_detector import BuildSystem, BuildSystemDetector
from certora_autosetup.parsers.truffle import TruffleContractExtractor
from certora_autosetup.utils.project_dir import find_build_config_dir


class _AllInScope:
    def is_file_in_scope(self, file_path):
        return True


@pytest.fixture
def manager(tmp_path: Path) -> TruffleManager:
    return TruffleManager(tmp_path, _AllInScope())


# Extractor output for a Truffle v5 config pinning its compiler.
V5_CONFIG = {
    "compilers": {"solc": {"version": "0.5.17", "settings": {"optimizer": {"enabled": True, "runs": 10000}}}},
    "solc": {},
    "contracts_directory": "./contracts",
    "contracts_build_directory": None,
}

# Truffle v4: settings under a top-level `solc` key, with no version anywhere — the
# compiler shipped inside truffle itself.
V4_CONFIG = {
    "compilers": {},
    "solc": {"optimizer": {"enabled": True, "runs": 10000}},
    "contracts_directory": None,
    "contracts_build_directory": None,
}


def test_v5_config_pins_solc_and_paths(manager: TruffleManager, tmp_path: Path) -> None:
    config = manager._extract_config_from_json(V5_CONFIG, tmp_path)

    assert config.solc_version == "0.5.17"
    assert config.optimizer is True
    assert config.optimizer_runs == 10000
    assert config.src == "contracts"
    assert config.get_artifact_directory() == "build/contracts"
    assert config.evm_version is None


def test_v5_config_evm_version_is_honored(manager: TruffleManager, tmp_path: Path) -> None:
    config_data = {
        **V5_CONFIG,
        "compilers": {
            "solc": {"version": "0.5.17", "settings": {"evmVersion": "istanbul"}}
        },
    }
    config = manager._extract_config_from_json(config_data, tmp_path)

    assert config.evm_version == "istanbul"
    assert config.to_certora_dict()["solc_evm_version"] == "istanbul"


def test_v4_config_has_no_version_to_pin(manager: TruffleManager, tmp_path: Path) -> None:
    # solc_version None is the correct answer, not a failure: the compilation phase then
    # resolves the version per contract from the source pragmas.
    config = manager._extract_config_from_json(V4_CONFIG, tmp_path)

    assert config.solc_version is None
    assert config.optimizer is True
    assert config.optimizer_runs == 10000


def test_empty_config_falls_back_to_truffle_defaults(manager: TruffleManager, tmp_path: Path) -> None:
    config = manager._extract_config_from_json({}, tmp_path)

    assert config.solc_version is None
    assert config.src == "contracts"
    assert config.get_artifact_directory() == "build/contracts"


@pytest.mark.parametrize("version", ["pragma", "native", "0.8", "/usr/local/bin/solc"])
def test_non_pin_solc_versions_are_ignored(manager: TruffleManager, version: str) -> None:
    # Truffle accepts these in `compilers.solc.version`; none of them names a version a
    # conf could carry, so they must behave like an absent version.
    assert manager._parse_solc_version(version) is None


def test_exact_solc_version_is_kept(manager: TruffleManager) -> None:
    assert manager._parse_solc_version("0.4.24") == "0.4.24"


def test_absolute_contracts_directory_is_relativized(manager: TruffleManager, tmp_path: Path) -> None:
    config_data = dict(V5_CONFIG, contracts_directory=str(tmp_path / "src" / "contracts"))
    config = manager._extract_config_from_json(config_data, tmp_path)

    assert config.src == str(Path("src") / "contracts")


def test_packages_come_from_package_json(tmp_path: Path) -> None:
    # Truffle resolves `@scope/pkg/...` through node_modules with no
    # remapping file in the project, so the conf's packages list is the only resolver solc gets.
    (tmp_path / "truffle-config.js").write_text("module.exports = {};")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"@scope/pkg": "1.0.0"}}))

    config = TruffleManager(tmp_path, _AllInScope()).parse_config(tmp_path / "truffle-config.js")

    packages = config.to_certora_dict()["packages"]
    assert any(entry.startswith("@scope/pkg/=") and entry.endswith("node_modules/@scope/pkg/") for entry in packages)


# --- detection ---------------------------------------------------------------------


def test_detects_truffle_config(tmp_path: Path) -> None:
    (tmp_path / "truffle-config.js").write_text("module.exports = {};")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.TRUFFLE


def test_detects_legacy_truffle_js(tmp_path: Path) -> None:
    # Truffle v4's config name on non-Windows.
    (tmp_path / "truffle.js").write_text("module.exports = {};")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.TRUFFLE


def test_foundry_outranks_a_leftover_truffle_config(tmp_path: Path) -> None:
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    (tmp_path / "truffle-config.js").write_text("module.exports = {};")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.FOUNDRY


def test_hardhat_outranks_a_leftover_truffle_config(tmp_path: Path) -> None:
    (tmp_path / "hardhat.config.js").write_text("module.exports = {};")
    (tmp_path / "truffle.js").write_text("module.exports = {};")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.HARDHAT


def test_detects_truffle_from_package_json_dependency(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"truffle": "^5.1.0"}}))

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.TRUFFLE


def test_detects_truffle_from_build_artifacts(tmp_path: Path) -> None:
    (tmp_path / "build" / "contracts").mkdir(parents=True)

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.TRUFFLE


def test_nested_truffle_config_anchors_detection(tmp_path: Path) -> None:
    # A Truffle monorepo keeps one truffle config per app; the app's config governs its
    # contracts even though the run root is the repo root.
    app = tmp_path / "apps" / "voting"
    (app / "contracts").mkdir(parents=True)
    (app / "build" / "contracts").mkdir(parents=True)
    (app / "truffle-config.js").write_text("module.exports = {};")
    contract = app / "contracts" / "Voting.sol"
    contract.write_text("contract Voting {}")

    assert find_build_config_dir(contract, tmp_path) == app.resolve()
    assert BuildSystemDetector.detect(app) == BuildSystem.TRUFFLE


# --- artifact extraction -----------------------------------------------------------


def _write_artifact(build_dir: Path, name: str, source_path: Path, bytecode: str) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / f"{name}.json").write_text(json.dumps({
        "contractName": name,
        "sourcePath": str(source_path),
        "bytecode": bytecode,
        "compiler": {"name": "solc", "version": "0.4.24+commit.e67f0147.Emscripten.clang"},
    }))


def test_extracts_logic_contracts_from_truffle_artifacts(tmp_path: Path, monkeypatch) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "ACL.sol").write_text("contract ACL {}")
    (contracts / "IACL.sol").write_text("interface IACL {}")

    build_dir = tmp_path / "build" / "contracts"
    _write_artifact(build_dir, "ACL", contracts / "ACL.sol", "0x6080604052")
    # Interfaces compile to no bytecode.
    _write_artifact(build_dir, "IACL", contracts / "IACL.sol", "0x")
    # A dependency compiled out of node_modules records a sourcePath outside the project.
    _write_artifact(build_dir, "ERC20", tmp_path / ".." / "elsewhere" / "ERC20.sol", "0x6080604052")

    monkeypatch.chdir(tmp_path)
    handles = TruffleContractExtractor(tmp_path).extract_logic_contracts()

    assert [(h.contract_name, h.source_file) for h in handles] == [("ACL", str(Path("contracts") / "ACL.sol"))]


def test_missing_artifacts_dir_names_the_build_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception, match="npx truffle compile"):
        TruffleContractExtractor(tmp_path).extract_logic_contracts()


# --- node-backed config evaluation --------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="requires node to evaluate truffle-config.js")
def test_config_is_evaluated_by_node(tmp_path: Path) -> None:
    # A truffle config is code, not data: this one computes its own optimizer runs, which
    # only a real evaluation resolves.
    (tmp_path / "truffle-config.js").write_text(
        "const runs = 10 * 1000;\n"
        "module.exports = {\n"
        "  compilers: { solc: { version: '0.4.24', settings: { optimizer: { enabled: true, runs } } } },\n"
        "  contracts_build_directory: './build/truffle',\n"
        "};\n"
    )

    config = TruffleManager(tmp_path, _AllInScope()).parse_config(tmp_path / "truffle-config.js")

    assert config.solc_version == "0.4.24"
    assert config.optimizer_runs == 10000
    assert config.get_artifact_directory() == str(Path("build") / "truffle")


@pytest.mark.skipif(shutil.which("node") is None, reason="requires node to evaluate truffle-config.js")
def test_config_requiring_a_missing_dependency_falls_back_to_defaults(tmp_path: Path) -> None:
    # A config that re-exports a shared package (`module.exports = require('<pkg>')`) is a
    # common Truffle-era layout; when the project's node_modules was never installed the
    # require throws. The run must continue with Truffle's defaults rather than abort.
    (tmp_path / "truffle-config.js").write_text("module.exports = require('shared-truffle-config');")

    config = TruffleManager(tmp_path, _AllInScope()).parse_config(tmp_path / "truffle-config.js")

    assert config.solc_version is None
    assert config.src == "contracts"
