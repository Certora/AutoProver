"""Tests for FoundryManager compiler-settings parsing (foundry.toml).

Covers the declared EVM version (`evm_version`): it must be honored because
solc's default EVM target can be newer than the declared one (shanghai/PUSH0 vs
a pinned "paris"), changing codegen and even failing with stack-too-deep where
the declared target compiles.

`forge` is not available in CI, so `forge remappings` is monkeypatched away —
these tests only exercise the toml parsing.
"""

from pathlib import Path

import pytest

from certora_autosetup.build_systems.foundry import FoundryManager
from certora_autosetup.utils import remappings as remappings_mod


@pytest.fixture(autouse=True)
def _no_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("forge")

    monkeypatch.setattr(remappings_mod.subprocess, "run", fake_run)


def _parse(tmp_path: Path, toml: str, profile: str | None = None):
    foundry_toml = tmp_path / "foundry.toml"
    foundry_toml.write_text(toml)
    manager = FoundryManager(project_root=tmp_path, scope=None)
    return manager.parse_config(foundry_toml, profile)


def test_evm_version_read_from_default_profile(tmp_path: Path) -> None:
    config = _parse(tmp_path, '[profile.default]\nevm_version = "paris"\n')
    assert config.evm_version == "paris"
    assert config.to_certora_dict()["solc_evm_version"] == "paris"


def test_evm_version_absent(tmp_path: Path) -> None:
    config = _parse(tmp_path, '[profile.default]\nsrc = "src"\n')
    assert config.evm_version is None
    assert "solc_evm_version" not in config.to_certora_dict()


def test_evm_version_profile_override(tmp_path: Path) -> None:
    config = _parse(
        tmp_path,
        '[profile.default]\nevm_version = "paris"\n[profile.ci]\nevm_version = "shanghai"\n',
        profile="ci",
    )
    assert config.evm_version == "shanghai"


def test_evm_version_inherited_from_default_profile(tmp_path: Path) -> None:
    config = _parse(
        tmp_path,
        '[profile.default]\nevm_version = "paris"\n[profile.ci]\nsrc = "src"\n',
        profile="ci",
    )
    assert config.evm_version == "paris"
