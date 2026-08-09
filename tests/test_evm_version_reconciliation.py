"""Tests for solc_evm_version scalar/map reconciliation outside the workaround loop:
the merged-conf helper in autosetup.py and the map fill in sync_compiler_maps_with_files.
"""

from pathlib import Path

from certora_autosetup.autosetup.autosetup import _reconcile_evm_version_keys
from certora_autosetup.utils.enhanced_config_manager import ConfigManager


# ---------------------------------------------------------------------------
# _reconcile_evm_version_keys (merged base conf + compilation updates)
# ---------------------------------------------------------------------------


def test_removal_propagates_over_base_scalar() -> None:
    # The invalid_evm_version workaround dropped the declared version from the
    # updates; the base build-system conf re-emits the scalar, which must not
    # resurrect the rejected setting.
    config = {"solc_evm_version": "paris"}
    _reconcile_evm_version_keys(config, updates={"solc": "solc8.22"}, contract_names=["Foo"])
    assert "solc_evm_version" not in config


def test_scalar_kept_when_updates_still_carry_it() -> None:
    config = {"solc_evm_version": "paris"}
    _reconcile_evm_version_keys(
        config, updates={"solc_evm_version": "paris"}, contract_names=["Foo"]
    )
    assert config["solc_evm_version"] == "paris"


def test_scalar_kept_when_no_updates_exist() -> None:
    # No compilation phase ran (e.g. cache hit): the declared version stands.
    config = {"solc_evm_version": "paris"}
    _reconcile_evm_version_keys(config, updates={}, contract_names=["Foo"])
    assert config["solc_evm_version"] == "paris"


def test_map_supersedes_scalar_and_is_completed() -> None:
    # Cancun override for one contract + declared version: the scalar expands
    # into the map for the contracts it misses (the prover forbids scalar+map
    # and requires the map to cover every contract).
    config = {
        "solc_evm_version": "paris",
        "solc_evm_version_map": {"Foo": "cancun"},
    }
    _reconcile_evm_version_keys(
        config,
        updates={"solc_evm_version_map": {"Foo": "cancun"}},
        contract_names=["Foo", "Bar"],
    )
    assert "solc_evm_version" not in config
    assert config["solc_evm_version_map"] == {"Foo": "cancun", "Bar": "paris"}


def test_map_without_scalar_left_alone() -> None:
    config = {"solc_evm_version_map": {"Foo": "cancun"}}
    _reconcile_evm_version_keys(
        config,
        updates={"solc_evm_version_map": {"Foo": "cancun"}},
        contract_names=["Foo", "Bar"],
    )
    assert config["solc_evm_version_map"] == {"Foo": "cancun"}


# ---------------------------------------------------------------------------
# sync_compiler_maps_with_files: evm map fill for newly injected files
# ---------------------------------------------------------------------------


def _touch_files(project_root: Path, files: list) -> None:
    # parse_contract_files(strict=False) silently drops paths that don't exist.
    for spec in files:
        path = project_root / spec.split(":", 1)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_sync_fills_evm_map_from_reference_scalar(tmp_path: Path) -> None:
    manager = ConfigManager(project_root=tmp_path)
    manager.reference_compiler_maps = {"solc_evm_version": "paris"}
    conf = {
        "files": ["contracts/Foo.sol:Foo", "certora/mocks/Mock.sol:Mock"],
        "solc_evm_version_map": {"Foo": "cancun"},
    }
    _touch_files(tmp_path, conf["files"])
    assert manager.sync_compiler_maps_with_files(conf) is True
    assert conf["solc_evm_version_map"] == {"Foo": "cancun", "Mock": "paris"}


def test_sync_fills_evm_map_from_majority_value(tmp_path: Path) -> None:
    manager = ConfigManager(project_root=tmp_path)
    conf = {
        "files": [
            "contracts/Foo.sol:Foo",
            "contracts/Bar.sol:Bar",
            "certora/mocks/Mock.sol:Mock",
        ],
        "solc_evm_version_map": {"Foo": "paris", "Bar": "paris"},
    }
    _touch_files(tmp_path, conf["files"])
    assert manager.sync_compiler_maps_with_files(conf) is True
    assert conf["solc_evm_version_map"]["Mock"] == "paris"


def test_sync_trims_and_fills_together(tmp_path: Path) -> None:
    manager = ConfigManager(project_root=tmp_path)
    manager.reference_compiler_maps = {"solc_evm_version": "paris"}
    conf = {
        "files": ["contracts/Foo.sol:Foo"],
        "solc_evm_version_map": {"Gone": "cancun", "Foo": "paris"},
    }
    _touch_files(tmp_path, conf["files"])
    assert manager.sync_compiler_maps_with_files(conf) is True
    assert conf["solc_evm_version_map"] == {"Foo": "paris"}
