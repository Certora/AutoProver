"""EVM versions taken from the project's own build.

The prover requires ``solc_evm_version_map`` to cover every file. A contract the build
says nothing about gets the declared ``solc_evm_version`` or, without one, the default
of its own compiler, which ``solc_default_evm_version`` reads from solc itself.
"""

import json
import subprocess
from pathlib import Path

import pytest

from certora_autosetup.build_systems.hardhat import HardhatConfig, HardhatManager
from certora_autosetup.parsers.foundry import ArtifactContract, FoundryContractExtractor
from certora_autosetup.utils import solc_version_resolver
from certora_autosetup.utils.scope import Scope
from certora_autosetup.utils.solc_version_resolver import (
    assign_evm_versions,
    solc_default_evm_version,
)
from certora_autosetup.utils.types import ContractHandle

FOO = ContractHandle(contract_name="Foo", source_file="src/Foo.sol")
BAR = ContractHandle(contract_name="Bar", source_file="src/Bar.sol")
OLD = ContractHandle(contract_name="Old", source_file="src/Old.sol")


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    solc_default_evm_version.cache_clear()
    yield
    solc_default_evm_version.cache_clear()


@pytest.fixture
def solc_defaults(monkeypatch):
    """Stand in for the solc probe; records which binaries were probed."""
    defaults = {"solc8.24": "shanghai", "solc7.6": "istanbul", "solc": "paris"}
    probed: list = []

    def fake(solc_binary):
        probed.append(solc_binary)
        return defaults.get(solc_binary)

    monkeypatch.setattr(solc_version_resolver, "solc_default_evm_version", fake)
    return probed


# --- solc_default_evm_version ----------------------------------------------------------


def test_default_evm_version_read_from_metadata(monkeypatch) -> None:
    calls: list = []
    metadata = json.dumps({"settings": {"evmVersion": "shanghai"}})
    stdout = json.dumps({"contracts": {"probe.sol": {"Probe": {"metadata": metadata}}}})

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        request = json.loads(kwargs["input"])
        assert "evmVersion" not in request["settings"]
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(solc_version_resolver.subprocess, "run", fake_run)
    assert solc_default_evm_version("solc8.24") == "shanghai"
    assert solc_default_evm_version("solc8.24") == "shanghai"
    assert calls == [["solc8.24", "--standard-json"]]  # probed once per binary


def test_default_evm_version_none_when_binary_missing(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(solc_version_resolver.subprocess, "run", fake_run)
    assert solc_default_evm_version("solc9.99") is None


# --- assign_evm_versions ---------------------------------------------------------------


def test_contracts_outside_the_build_get_the_declared_version(solc_defaults) -> None:
    config = {"solc": "solc8.24", "solc_evm_version": "paris"}
    assign_evm_versions(config, [FOO, BAR], {"Foo": "cancun"})
    assert config["solc_evm_version_map"] == {"Foo": "cancun", "Bar": "paris"}
    assert "solc_evm_version" not in config
    assert solc_defaults == []


def test_without_a_declared_version_each_compiler_default_is_used(solc_defaults) -> None:
    config = {"compiler_map": {"Foo": "solc8.24", "Bar": "solc8.24", "Old": "solc7.6"}}
    assign_evm_versions(config, [FOO, BAR, OLD], {"Foo": "cancun"})
    assert config["solc_evm_version_map"] == {"Foo": "cancun", "Bar": "shanghai", "Old": "istanbul"}


def test_without_any_compiler_named_plain_solc_is_probed(solc_defaults) -> None:
    config: dict = {}
    assign_evm_versions(config, [FOO, BAR], {"Foo": "cancun"})
    assert config["solc_evm_version_map"] == {"Foo": "cancun", "Bar": "paris"}
    assert solc_defaults == ["solc"]


def test_one_value_for_every_contract_is_the_scalar(solc_defaults) -> None:
    config = {"solc": "solc8.24", "solc_evm_version": "paris"}
    assign_evm_versions(config, [FOO, BAR], {"Foo": "cancun", "Bar": "cancun"})
    assert config["solc_evm_version"] == "cancun"
    assert "solc_evm_version_map" not in config


def test_a_compiler_that_cannot_run_leaves_its_contract_out(solc_defaults) -> None:
    config = {"compiler_map": {"Foo": "solc8.24", "Bar": "solc9.99"}}
    assign_evm_versions(config, [FOO, BAR], {"Foo": "cancun"})
    # Never collapsed to a scalar: the map does not cover Bar.
    assert config["solc_evm_version_map"] == {"Foo": "cancun"}


# --- Foundry build artifacts ---------------------------------------------------------


def _foundry_artifact(project: Path, contract: str, source: str, evm_version: str | None) -> None:
    artifact_dir = project / "out" / Path(source).name
    artifact_dir.mkdir(parents=True)
    settings: dict = {"compilationTarget": {source: contract}}
    if evm_version is not None:
        settings["evmVersion"] = evm_version
    (artifact_dir / f"{contract}.json").write_text(json.dumps({
        "bytecode": {"object": "0x6080604052"},
        "metadata": {"compiler": {"version": "0.8.24+commit.e11b9ed9"}, "settings": settings},
    }))


def test_artifact_map_carries_the_evm_version(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    (tmp_path / "src").mkdir()
    for name in ("Foo", "Bar"):
        (tmp_path / "src" / f"{name}.sol").write_text(f"contract {name} {{}}")
    _foundry_artifact(tmp_path, "Foo", "src/Foo.sol", "cancun")
    _foundry_artifact(tmp_path, "Bar", "src/Bar.sol", None)
    monkeypatch.chdir(tmp_path)

    source_map = FoundryContractExtractor(tmp_path).build_source_path_to_contracts_map()

    assert source_map == {
        "src/Foo.sol": [ArtifactContract("Foo", "0.8.24", "cancun")],
        "src/Bar.sol": [ArtifactContract("Bar", "0.8.24", None)],
    }


# --- Hardhat overrides -----------------------------------------------------------------


def test_hardhat_override_paths_are_rebased_to_the_run_root(tmp_path: Path) -> None:
    hardhat_root = tmp_path / "packages" / "core"
    hardhat_root.mkdir(parents=True)
    manager = HardhatManager(hardhat_root, Scope(project_root=tmp_path), run_root=tmp_path)

    overrides = manager._evm_version_overrides({
        "contracts/Foo.sol": {"version": "0.8.24", "settings": {"evmVersion": "cancun"}},
        "contracts/Bar.sol": {"version": "0.8.24", "settings": {"optimizer": {"enabled": True}}},
    })

    assert overrides == {"packages/core/contracts/Foo.sol": "cancun"}


def test_hardhat_overrides_set_the_named_contracts_only(solc_defaults) -> None:
    config = HardhatConfig(evm_version="paris", evm_version_overrides={"src/Foo.sol": "cancun"})
    conf = {"solc": "solc8.24", "solc_evm_version": "paris"}

    config.apply_per_contract_settings([FOO, BAR], conf)

    assert conf["solc_evm_version_map"] == {"Foo": "cancun", "Bar": "paris"}
    assert "solc_evm_version" not in conf


def test_hardhat_without_overrides_leaves_the_conf_alone() -> None:
    conf = {"solc": "solc8.24", "solc_evm_version": "paris"}
    HardhatConfig(evm_version="paris").apply_per_contract_settings([FOO, BAR], conf)
    assert conf == {"solc": "solc8.24", "solc_evm_version": "paris"}
