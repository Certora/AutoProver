"""Unit tests for HardhatManager config extraction."""

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
