"""Unit tests for HardhatManager config extraction."""

import json
from pathlib import Path

import pytest

from certora_autosetup.build_systems.hardhat import HardhatManager


class _AllInScope:
    def is_file_in_scope(self, file_path):
        return True


@pytest.fixture
def manager(tmp_path: Path) -> HardhatManager:
    return HardhatManager(tmp_path, _AllInScope())


# Resolved-config JSON as emitted by hardhat_config_extractor.js for a config
# with no `solidity` entry (e.g. a brownie-generated network-only stub): hardhat
# fills in its own built-in default compiler, and the extractor flags it.
IMPLICIT_DEFAULT_CONFIG = {
    "solidity": {"compilers": [{"version": "0.7.3", "settings": {"optimizer": {"enabled": False, "runs": 200}}}]},
    "paths": {},
    "solidityImplicitDefault": True,
}

EXPLICIT_CONFIG = {
    "solidity": {"compilers": [{"version": "0.8.20", "settings": {"optimizer": {"enabled": True, "runs": 800}}}]},
    "paths": {},
    "solidityImplicitDefault": False,
}


def test_implicit_default_solc_is_ignored(manager: HardhatManager) -> None:
    # Regression (real brownie-stub project): hardhat's built-in default 0.7.3 was
    # taken as the project's compiler and the whole ^0.8.0 scene was pinned to it,
    # failing compilation analysis before any workaround could help.
    config = manager._extract_config_from_json(IMPLICIT_DEFAULT_CONFIG, "javascript")
    assert config.solc_version is None


def test_explicit_solc_is_kept(manager: HardhatManager) -> None:
    config = manager._extract_config_from_json(EXPLICIT_CONFIG, "javascript")
    assert config.solc_version == "0.8.20"
    assert config.optimizer is True
    assert config.optimizer_runs == 800


def test_missing_flag_keeps_reported_solc(manager: HardhatManager) -> None:
    # Extractor output without the flag (e.g. the `{}` error fallback merged with
    # defaults elsewhere) must behave as before: trust what is reported.
    config = manager._extract_config_from_json(
        {"solidity": "0.8.19", "paths": {}}, "javascript"
    )
    assert config.solc_version == "0.8.19"


# Declared EVM version (settings.evmVersion) must be honored: solc's default EVM
# target can be newer than the declared one (shanghai/PUSH0 vs a pinned "paris"),
# changing codegen and even failing with stack-too-deep where the declared
# target compiles.

EVM_VERSION_CONFIG = {
    "solidity": {
        "compilers": [
            {
                "version": "0.8.22",
                "settings": {
                    "optimizer": {"enabled": True, "runs": 1},
                    "viaIR": True,
                    "evmVersion": "paris",
                },
            }
        ]
    },
    "paths": {},
    "solidityImplicitDefault": False,
}


def test_evm_version_extracted_from_compilers_array(manager: HardhatManager) -> None:
    config = manager._extract_config_from_json(EVM_VERSION_CONFIG, "javascript")
    assert config.evm_version == "paris"


def test_evm_version_extracted_from_simple_format(manager: HardhatManager) -> None:
    config = manager._extract_config_from_json(
        {"solidity": {"version": "0.8.22", "settings": {"evmVersion": "paris"}}, "paths": {}},
        "javascript",
    )
    assert config.evm_version == "paris"


def test_evm_version_absent(manager: HardhatManager) -> None:
    assert manager._extract_config_from_json(EXPLICIT_CONFIG, "javascript").evm_version is None
    assert (
        manager._extract_config_from_json({"solidity": "0.8.19", "paths": {}}, "javascript").evm_version
        is None
    )


def test_evm_version_emitted_in_certora_dict(manager: HardhatManager) -> None:
    config = manager._extract_config_from_json(EVM_VERSION_CONFIG, "javascript")
    assert config.to_certora_dict()["solc_evm_version"] == "paris"
    no_evm = manager._extract_config_from_json(EXPLICIT_CONFIG, "javascript")
    assert "solc_evm_version" not in no_evm.to_certora_dict()


# When the config cannot be resolved through Node.js, the compiler settings come
# from the build-info files of the project's own `hardhat compile`.


def _write_build_info(
    build_info_dir: Path, name: str, solc_version: str, settings: dict, n_sources: int = 1
) -> None:
    build_info_dir.mkdir(parents=True, exist_ok=True)
    (build_info_dir / f"{name}.json").write_text(json.dumps({
        "_format": "hh-sol-build-info-1",
        "solcVersion": solc_version,
        "input": {
            "language": "Solidity",
            "sources": {f"contracts/C{i}.sol": {"content": ""} for i in range(n_sources)},
            "settings": {**settings, "outputSelection": {"*": {"*": ["abi"]}}},
        },
        "output": {},
    }))


@pytest.fixture
def no_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HardhatManager, "_extract_config_via_node", lambda self, config_file: None)


def _hardhat_config(project: Path) -> Path:
    config_file = project / "hardhat.config.ts"
    config_file.write_text("export default {};\n")
    return config_file


def test_build_info_used_without_node(manager: HardhatManager, tmp_path: Path, no_node: None) -> None:
    # The pinned solc, not the newest one the pragmas allow.
    _write_build_info(
        tmp_path / "artifacts" / "build-info", "a", "0.8.19",
        {"optimizer": {"enabled": True, "runs": 100}, "viaIR": True, "evmVersion": "paris"},
    )
    config = manager.parse_config(_hardhat_config(tmp_path))
    assert config.solc_version == "0.8.19"
    assert config.optimizer is True
    assert config.optimizer_runs == 100
    assert config.via_ir is True
    assert config.evm_version == "paris"


def test_build_info_multiple_versions_leave_solc_unset(
    manager: HardhatManager, tmp_path: Path, no_node: None
) -> None:
    build_info_dir = tmp_path / "artifacts" / "build-info"
    _write_build_info(build_info_dir, "a", "0.8.19", {})
    _write_build_info(build_info_dir, "b", "0.7.6", {})
    assert manager.parse_config(_hardhat_config(tmp_path)).solc_version is None


def test_build_info_largest_job_sets_the_settings(
    manager: HardhatManager, tmp_path: Path, no_node: None
) -> None:
    build_info_dir = tmp_path / "artifacts" / "build-info"
    _write_build_info(build_info_dir, "a", "0.8.19", {"evmVersion": "london"}, n_sources=1)
    _write_build_info(build_info_dir, "b", "0.8.19", {"evmVersion": "paris"}, n_sources=5)
    config = manager.parse_config(_hardhat_config(tmp_path))
    assert config.solc_version == "0.8.19"
    assert config.evm_version == "paris"


def test_build_info_skips_files_without_solc_input(
    manager: HardhatManager, tmp_path: Path, no_node: None
) -> None:
    build_info_dir = tmp_path / "artifacts" / "build-info"
    _write_build_info(build_info_dir, "a", "0.8.28", {})
    (build_info_dir / "b.json").write_text(json.dumps({"output": {"contracts": {}}}))
    (build_info_dir / "broken.json").write_text("{not json")
    assert manager.parse_config(_hardhat_config(tmp_path)).solc_version == "0.8.28"


def test_defaults_without_node_or_build_info(manager: HardhatManager, tmp_path: Path, no_node: None) -> None:
    config = manager.parse_config(_hardhat_config(tmp_path))
    assert config.solc_version is None
    assert config.via_ir is False
