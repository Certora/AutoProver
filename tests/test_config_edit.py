"""
Tests for ConfigEditTool wired through a ReAct graph.

Verifies that edits produce correct state transitions via the state reducers,
not just correct Command objects.
"""
import pytest

from langgraph.graph import MessagesState

from composer.spec.source.author import (
    ConfigEditTool, AddFile, RemoveFile,
    SetStorageExtensionAnnotation, SetStorageExtensionHarnesses, SetContractExtensions,
    ExtensionSpec,
)
from composer.spec.source.conf_maps import CompilerSettings
from composer.spec.source.munge.tool_names import CONFIG_EDIT
from composer.spec.source.prover import (
    OVERLAY_OWNED_KEYS, ProverStateExtra, prover_config_overlay,
)

from graphcore.testing import Scenario, tool_call_raw, ToolCallDict

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# State + constants
# ---------------------------------------------------------------------------


class ConfigTestState(MessagesState, ProverStateExtra):
    pass


_EDIT = CONFIG_EDIT
TOOL = ConfigEditTool.as_tool(_EDIT)


# ---------------------------------------------------------------------------
# Tool call constructors
# ---------------------------------------------------------------------------


def _add_file(
    path: str,
    contract_name: str | None = None,
    compiler_settings: CompilerSettings | None = None,
) -> dict:
    return AddFile(
        type="add_file", file_path=path, contract_name=contract_name,
        compiler_settings=compiler_settings,
    ).model_dump()


def _remove_file(path: str) -> dict:
    return RemoveFile(type="remove_file", path_to_remove=path).model_dump()


def _set_annotation(value: bool) -> dict:
    return SetStorageExtensionAnnotation(type="storage_extension_annotation", value=value).model_dump()


def _set_harnesses(*pairs: tuple[str, str]) -> dict:
    return SetStorageExtensionHarnesses(type="storage_extension_harnesses", harnesses=list(pairs)).model_dump()


def _set_extensions(extensions: dict) -> dict:
    return SetContractExtensions(type="contract_extensions", extensions=extensions).model_dump()


def _edit(*edits: dict) -> ToolCallDict:
    return tool_call_raw(_EDIT, edits=list(edits))


# ---------------------------------------------------------------------------
# Scenario builder + extractors
# ---------------------------------------------------------------------------


def _scenario(
    files: list[str] | None = None,
    extra: dict | None = None,
):
    config: dict = {}
    if files is not None:
        config["files"] = files
    if extra is not None:
        config.update(extra)
    return Scenario(ConfigTestState, TOOL).init(
        config=config, rule_skips={},
    )


def _config(st: ConfigTestState) -> dict:
    return st["config"]


def _files(st: ConfigTestState) -> list[str]:
    return st["config"]["files"]


def _edit_response(st: ConfigTestState) -> str:
    return Scenario.last_single_tool(_EDIT, st)


# =========================================================================
# AddFile
# =========================================================================


class TestAddFileCompilerMaps:
    async def test_add_file_without_demanded_settings_rejected(self):
        # Present map + absent contract + no settings: the whole edit aborts —
        # neither the file registration nor any map entry lands.
        config = await _scenario(
            files=["src/Hub.sol"],
            extra={"solc_optimize_map": {"Hub": "22300"}},
        ).turn(
            _edit(_add_file("certora/mocks/Mock.sol")),
        ).map_run(_config)
        assert "certora/mocks/Mock.sol" not in config["files"]
        assert "Mock" not in config["solc_optimize_map"]

    async def test_add_file_spurious_settings_rejected(self):
        config = await _scenario(
            files=["src/Hub.sol"],
        ).turn(
            _edit(_add_file(
                "certora/mocks/Mock.sol",
                compiler_settings=CompilerSettings(optimize="200"),
            )),
        ).map_run(_config)
        assert "certora/mocks/Mock.sol" not in config["files"]
        assert "solc_optimize_map" not in config

    async def test_add_file_explicit_settings_written_under_contract(self):
        config = await _scenario(
            files=["src/Hub.sol"],
            extra={"solc_map": {"Hub": "solc8.29"}},
        ).turn(
            _edit(_add_file(
                "certora/mocks/OldMock.sol",
                compiler_settings=CompilerSettings(solc="solc4.24"),
            )),
        ).map_run(_config)
        assert config["solc_map"]["OldMock"] == "solc4.24"

    async def test_add_file_contract_already_mapped_needs_no_settings(self):
        config = await _scenario(
            files=["src/Hub.sol"],
            extra={"solc_optimize_map": {"Hub": "22300", "Mock": "200"}},
        ).turn(
            _edit(_add_file("certora/mocks/Mock.sol")),
        ).map_run(_config)
        assert "certora/mocks/Mock.sol" in config["files"]
        assert config["solc_optimize_map"] == {"Hub": "22300", "Mock": "200"}

    async def test_add_file_with_contract_suffix_maps_contract_name(self):
        config = await _scenario(
            files=["src/Hub.sol"],
            extra={"solc_optimize_map": {"Hub": "22300"}},
        ).turn(
            _edit(_add_file(
                "certora/mocks/Mocks.sol", "DummyERC20B",
                compiler_settings=CompilerSettings(optimize="200"),
            )),
        ).map_run(_config)
        assert config["solc_optimize_map"]["DummyERC20B"] == "200"
        assert "Mocks" not in config["solc_optimize_map"]
        assert "certora/mocks/Mocks.sol:DummyERC20B" in config["files"]


class TestAddFile:
    async def test_add_file(self):
        files = await _scenario(files=["src/Foo.sol"]).turn(
            _edit(_add_file("src/Bar.sol")),
        ).map_run(_files)
        assert "src/Foo.sol" in files
        assert "src/Bar.sol" in files

    async def test_add_file_with_explicit_contract(self):
        files = await _scenario(files=[]).turn(
            _edit(_add_file("src/Bar.sol", "BarImpl")),
        ).map_run(_files)
        assert "src/Bar.sol:BarImpl" in files

    async def test_add_duplicate_rejected(self):
        files = await _scenario(files=["src/Foo.sol"]).turn(
            _edit(_add_file("src/Foo.sol")),
        ).map_run(_files)
        assert files == ["src/Foo.sol"]


# =========================================================================
# RemoveFile
# =========================================================================


class TestRemoveFile:
    async def test_remove_file(self):
        files = await _scenario(files=["src/Foo.sol", "src/Bar.sol"]).turn(
            _edit(_remove_file("src/Foo.sol")),
        ).map_run(_files)
        assert files == ["src/Bar.sol"]

    async def test_remove_nonexistent_rejected(self):
        files = await _scenario(files=["src/Foo.sol"]).turn(
            _edit(_remove_file("src/Missing.sol")),
        ).map_run(_files)
        assert files == ["src/Foo.sol"]

    async def test_remove_matches_contract_suffix(self):
        """RemoveFile uses startswith, so 'src/Foo.sol' matches 'src/Foo.sol:FooImpl'."""
        files = await _scenario(files=["src/Foo.sol:FooImpl", "src/Bar.sol"]).turn(
            _edit(_remove_file("src/Foo.sol")),
        ).map_run(_files)
        assert files == ["src/Bar.sol"]


# =========================================================================
# Flag edits
# =========================================================================


class TestFlagEdits:
    async def test_annotation_set(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_annotation(True)),
        ).map_run(_config)
        assert config["storage_extension_annotation"] is True

    async def test_annotation_false_deletes(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_annotation(True), _set_annotation(False)),
        ).map_run(_config)
        assert "storage_extension_annotation" not in config

    async def test_harnesses_set_encodes_pairs(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_harnesses(("Vault", "VaultHarness"), ("Pool", "PoolHarness"))),
        ).map_run(_config)
        assert config["storage_extension_harnesses"] == ["Vault=VaultHarness", "Pool=PoolHarness"]

    async def test_harnesses_empty_deletes(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_harnesses(("Vault", "VaultHarness")), _set_harnesses()),
        ).map_run(_config)
        assert "storage_extension_harnesses" not in config

    async def test_harnesses_sol_extension_rejected(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_harnesses(("Vault.sol", "VaultHarness"))),
        ).map_run(_config)
        assert "storage_extension_harnesses" not in config

    async def test_extensions_set(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_extensions({"Vault": [ExtensionSpec(extension="Ext")]})),
        ).map_run(_config)
        assert config["contract_extensions"] == {"Vault": [{"extension": "Ext", "exclude": []}]}

    async def test_extensions_exclude_carried(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_extensions({"Vault": [ExtensionSpec(extension="Ext", exclude=["burn"])]})),
        ).map_run(_config)
        assert config["contract_extensions"] == {"Vault": [{"extension": "Ext", "exclude": ["burn"]}]}

    async def test_extensions_empty_deletes(self):
        config = await _scenario(files=[]).turn(
            _edit(
                _set_extensions({"Vault": [ExtensionSpec(extension="Ext")]}),
                _set_extensions({}),
            ),
        ).map_run(_config)
        assert "contract_extensions" not in config

    async def test_extensions_sol_extension_rejected(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_extensions({"Vault": [ExtensionSpec(extension="Ext.sol")]})),
        ).map_run(_config)
        assert "contract_extensions" not in config


# =========================================================================
# Overlay ownership
# =========================================================================


class TestOverlayOwnership:
    async def test_overlay_keys_are_all_owned(self):
        """Every key the run overlay forces must appear in OVERLAY_OWNED_KEYS, or the
        flag-edit disjointness guard is checking against an understated set."""
        overlaid = prover_config_overlay({}, main_contract="C", verify_target="C:x.spec")
        assert set(overlaid) <= OVERLAY_OWNED_KEYS


# =========================================================================
# Multiple edits
# =========================================================================


class TestMultipleEdits:
    async def test_sequential_add_then_remove(self):
        files = await _scenario(files=["src/Foo.sol"]).turn(
            _edit(_add_file("src/Bar.sol"), _remove_file("src/Foo.sol")),
        ).map_run(_files)
        assert files == ["src/Bar.sol"]

    async def test_early_failure_aborts_remaining(self):
        files = await _scenario(files=["src/Foo.sol"]).turn(
            _edit(_remove_file("src/Missing.sol"), _add_file("src/Bar.sol")),
        ).map_run(_files)
        assert files == ["src/Foo.sol"]

    async def test_partial_mutation_not_applied(self):
        """First edit succeeds, second fails — original state preserved."""
        files = await _scenario(files=["src/Foo.sol"]).turn(
            _edit(_add_file("src/New.sol"), _remove_file("src/Missing.sol")),
        ).map_run(_files)
        assert files == ["src/Foo.sol"]
