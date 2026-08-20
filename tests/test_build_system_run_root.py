"""The build-config dir and the run root must reach the remapping builder as two distinct paths.

They differ exactly for a monorepo sub-project — the case remapping-context rebasing and the
hoisted-package walk were written for — so a manager constructed with only the build-config dir
makes both of them no-ops on the projects that need them.
"""

from pathlib import Path

import pytest

from certora_autosetup.build_systems.foundry import FoundryManager
from certora_autosetup.build_systems.truffle import TruffleManager
import certora_autosetup.build_systems.foundry as foundry_mod
import certora_autosetup.build_systems.truffle as truffle_mod


@pytest.fixture
def captured_builder_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the kwargs the managers pass to build_packages_from_remapping_sources."""
    captured: dict = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(foundry_mod, "build_packages_from_remapping_sources", fake_builder)
    monkeypatch.setattr(truffle_mod, "build_packages_from_remapping_sources", fake_builder)
    return captured


def test_foundry_manager_forwards_both_directories(tmp_path: Path, captured_builder_kwargs) -> None:
    project = tmp_path / "smart-contracts"
    project.mkdir()
    foundry_toml = project / "foundry.toml"
    foundry_toml.write_text("[profile.default]\n")

    FoundryManager(project_root=project, scope=None, run_root=tmp_path).parse_config(foundry_toml)

    assert captured_builder_kwargs["base_dir"] == project
    assert captured_builder_kwargs["run_root"] == tmp_path


def test_truffle_manager_forwards_both_directories(tmp_path: Path, captured_builder_kwargs) -> None:
    project = tmp_path / "smart-contracts"
    project.mkdir()
    config_file = project / "truffle-config.js"
    config_file.write_text("module.exports = {};\n")

    TruffleManager(project_root=project, scope=None, run_root=tmp_path).parse_config(config_file)

    assert captured_builder_kwargs["base_dir"] == project
    assert captured_builder_kwargs["run_root"] == tmp_path


def test_run_root_defaults_to_the_project_root(tmp_path: Path, captured_builder_kwargs) -> None:
    # A project whose build config sits at the run root needs no caller change.
    foundry_toml = tmp_path / "foundry.toml"
    foundry_toml.write_text("[profile.default]\n")

    FoundryManager(project_root=tmp_path, scope=None).parse_config(foundry_toml)

    assert captured_builder_kwargs["run_root"] == tmp_path


def test_every_manager_class_accepts_a_run_root(tmp_path: Path) -> None:
    # autosetup constructs whichever class detection picked through one polymorphic call, so a
    # manager that does not accept run_root would raise TypeError at setup time.
    from certora_autosetup.build_systems.hardhat import HardhatManager

    for manager_class in (FoundryManager, HardhatManager, TruffleManager):
        manager = manager_class(tmp_path / "sub", None, run_root=tmp_path)
        assert manager.run_root == tmp_path
        assert manager.project_root == tmp_path / "sub"
