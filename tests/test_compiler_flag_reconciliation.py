"""Tests for scalar/map compiler-flag exclusivity in generated confs.

certoraRun hard-rejects a conf that carries both a per-contract map and its
scalar counterpart ("compiler map flags cannot be set with other compiler
flags" for solc/compiler_map, "You cannot use both ..." for the other pairs),
and the rejection happens before any solc invocation — no compilation
workaround can detect it. Every step that puts a map into a conf must
therefore drop the superseded scalar.
"""

from pathlib import Path

import pytest

from certora_autosetup.autosetup.autosetup import Autosetup
from certora_autosetup.cache.cache_fs import init_cache_fs
from certora_autosetup.parsers.build_system_detector import BuildSystem
from certora_autosetup.setup.setup_prover import SetupProver
from certora_autosetup.utils.enhanced_config_manager import ConfigManager
from certora_autosetup.utils.scope import Scope
from certora_autosetup.utils.types import ContractHandle


# =============================================================================
# ConfigManager.drop_scalars_superseded_by_maps
# =============================================================================


def test_drops_solc_when_compiler_map_present() -> None:
    conf = {"solc": "solc8.30", "compiler_map": {"Vault": "solc8.35"}}
    ConfigManager.drop_scalars_superseded_by_maps(conf)
    assert conf == {"compiler_map": {"Vault": "solc8.35"}}


def test_each_pair_is_dropped_independently() -> None:
    conf = {
        "solc": "solc8.30",
        "compiler_map": {"Vault": "solc8.35"},
        "solc_via_ir": True,
        "solc_via_ir_map": {"Vault": True},
        "solc_optimize": "200",
        "solc_optimize_map": {"Vault": "200"},
        "solc_evm_version": "paris",
        "solc_evm_version_map": {"Vault": "cancun"},
    }
    ConfigManager.drop_scalars_superseded_by_maps(conf)
    assert set(conf) == {
        "compiler_map",
        "solc_via_ir_map",
        "solc_optimize_map",
        "solc_evm_version_map",
    }


def test_scalars_survive_without_maps() -> None:
    conf = {"solc": "solc8.30", "solc_via_ir": True, "files": ["A.sol"]}
    ConfigManager.drop_scalars_superseded_by_maps(conf)
    assert conf == {"solc": "solc8.30", "solc_via_ir": True, "files": ["A.sol"]}


def test_unrelated_pair_not_affected() -> None:
    # A via-ir map must not drop the solc scalar and vice versa.
    conf = {"solc": "solc8.30", "solc_via_ir_map": {"Vault": True}}
    ConfigManager.drop_scalars_superseded_by_maps(conf)
    assert conf == {"solc": "solc8.30", "solc_via_ir_map": {"Vault": True}}


# =============================================================================
# SetupProver._precompute_compiler_settings
# =============================================================================
#
# The shape observed in the wild: foundry.toml pins solc 0.8.30 (the conf's
# scalar "solc"), but the build's artifacts were produced with 0.8.35, so a
# compiler_map is precomputed for every contract. The scalar must not survive
# next to the map.


class _StubExtractor:
    def __init__(self, source_map):
        self._source_map = source_map

    def build_source_path_to_contracts_map(self):
        return self._source_map


@pytest.fixture
def setup_prover(tmp_path: Path, monkeypatch) -> SetupProver:
    monkeypatch.chdir(tmp_path)
    init_cache_fs(str(tmp_path), force=True)
    certora_dir = tmp_path / "certora"
    certora_dir.mkdir()
    sp = SetupProver(
        log=lambda *args, **kwargs: None,
        certora_dir=certora_dir,
        script_dir=tmp_path,
        additional_contracts=[],
        extra_args=[],
        skip_llm=True,
        force_llm_regenerate=False,
        stop_after_summaries=True,
        scope=Scope(project_root=tmp_path),
    )
    sp.build_system = BuildSystem.FOUNDRY
    return sp


def test_precompute_drops_scalar_solc_when_map_is_built(
    setup_prover, tmp_path: Path, monkeypatch
) -> None:
    mock_src = tmp_path / "certora" / "mocks" / "DummyERC20Impl.sol"
    mock_src.parent.mkdir(parents=True)
    mock_src.write_text("pragma solidity ^0.8.0;\ncontract DummyERC20Impl {}\n")
    monkeypatch.setattr(
        "certora_autosetup.utils.enhanced_config_manager.resolve_pragma_to_version",
        lambda spec, **kwargs: "0.8.30",
    )
    monkeypatch.setattr(
        "certora_autosetup.setup.setup_prover.FoundryContractExtractor",
        lambda root: _StubExtractor({"src/Vault.sol": [("Vault", "0.8.35")]}),
    )

    contracts = [
        ContractHandle(contract_name="Vault", source_file="src/Vault.sol"),
        ContractHandle(
            contract_name="DummyERC20Impl", source_file="certora/mocks/DummyERC20Impl.sol"
        ),
    ]
    config = setup_prover._precompute_compiler_settings(
        contracts, {"solc": "solc8.30", "files": ["src/Vault.sol", str(mock_src)]}
    )

    assert "solc" not in config
    # Total over the scene: artifact version for Vault, pragma-resolved
    # (biased to the old default) for the injected mock.
    assert config["compiler_map"] == {
        "Vault": "solc8.35",
        "DummyERC20Impl": "solc8.30",
    }


def test_precompute_keeps_scalar_when_artifacts_agree(setup_prover, monkeypatch) -> None:
    monkeypatch.setattr(
        "certora_autosetup.setup.setup_prover.FoundryContractExtractor",
        lambda root: _StubExtractor({"src/Vault.sol": [("Vault", "0.8.30")]}),
    )
    contracts = [ContractHandle(contract_name="Vault", source_file="src/Vault.sol")]
    config = setup_prover._precompute_compiler_settings(
        contracts, {"solc": "solc8.30", "files": ["src/Vault.sol"]}
    )
    assert config["solc"] == "solc8.30"
    assert "compiler_map" not in config


# =============================================================================
# Autosetup.get_build_system_config_dict_with_updates
# =============================================================================
#
# The base build-system dict contributes scalars (e.g. "solc") while the
# compilation updates contribute the maps; dict.update never deletes, so the
# merge must drop the superseded scalars before the dict reaches a conf.


def test_merge_drops_base_scalar_when_updates_bring_map(monkeypatch) -> None:
    autosetup = Autosetup.__new__(Autosetup)
    autosetup.compilation_config_updates = {
        "compiler_map": {"Vault": "solc8.35", "Helper": "solc8.30"},
        "solc_via_ir_map": {"Vault": True, "Helper": False},
    }
    autosetup.contract_handles = [
        ContractHandle(contract_name="Vault", source_file="src/Vault.sol"),
        ContractHandle(contract_name="Helper", source_file="src/Helper.sol"),
    ]
    monkeypatch.setattr(
        autosetup,
        "get_build_system_config_dict",
        lambda: {"solc": "solc8.30", "solc_via_ir": True, "packages": []},
        raising=False,
    )

    config = autosetup.get_build_system_config_dict_with_updates()

    assert "solc" not in config
    assert "solc_via_ir" not in config
    assert config["compiler_map"] == {"Vault": "solc8.35", "Helper": "solc8.30"}
    assert config["solc_via_ir_map"] == {"Vault": True, "Helper": False}


# =============================================================================
# SCALAR_TO_MAP_KEYS vs certora-cli
# =============================================================================


def test_scalar_to_map_keys_match_certora_cli() -> None:
    # The pair names are literals because importing them at runtime is not
    # clean: certora_cli's internal modules use flat imports (`from Shared
    # import ...`), so reaching certoraContextAttributes requires putting the
    # certora_cli package dir on sys.path (shadow risk for generic names like
    # `Shared`) plus the set_attribute_class() global. This test pins the
    # literals against the installed CLI in a subprocess instead, so any
    # rename/removal upstream fails here rather than silently desyncing.
    import subprocess as sp
    import sys

    conf_keys = [key for pair in ConfigManager.SCALAR_TO_MAP_KEYS for key in pair]
    probe = (
        "import sys, json, certora_cli\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(certora_cli.__file__).parent))\n"
        "import CertoraProver.certoraContextAttributes as Attrs\n"
        "Attrs.set_attribute_class(Attrs.EvmProverAttributes)\n"
        f"names = {conf_keys!r}\n"
        "resolved = [getattr(Attrs.EvmProverAttributes, n.upper()).get_conf_key() for n in names]\n"
        "print(json.dumps(resolved))\n"
    )
    result = sp.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert __import__("json").loads(result.stdout) == conf_keys
