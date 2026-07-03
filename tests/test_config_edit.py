"""
Tests for ConfigEditTool wired through a ReAct graph.

Verifies that edits produce correct state transitions via the state reducers,
not just correct Command objects.
"""
import pytest
from pydantic import ValidationError

from langgraph.graph import MessagesState

from composer.spec.source.author import (
    ConfigEditTool, AddFile, RemoveFile, AddLink, RemoveLink, SetProverFlag,
)
from composer.spec.source.prover import ProverStateExtra

from graphcore.testing import Scenario, tool_call_raw, ToolCallDict

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# State + constants
# ---------------------------------------------------------------------------


class ConfigTestState(MessagesState, ProverStateExtra):
    pass


_EDIT = "config_edit"
TOOL = ConfigEditTool.as_tool(_EDIT)


# ---------------------------------------------------------------------------
# Tool call constructors
# ---------------------------------------------------------------------------


def _add_file(path: str, contract_name: str | None = None) -> dict:
    return AddFile(type="add_file", file_path=path, contract_name=contract_name).model_dump()


def _remove_file(path: str) -> dict:
    return RemoveFile(type="remove_file", path_to_remove=path).model_dump()


def _add_link(src: str, field: str, tgt: str) -> dict:
    return AddLink(type="add_link", source_contract_name=src, link_field_name=field, target_contract_name=tgt).model_dump()


def _remove_link(src: str, field: str) -> dict:
    return RemoveLink(type="remove_link", source_contract_name=src, link_field_name=field).model_dump()


def _set_flag(flag: str, value: bool | int) -> dict:
    # Raw dict (not SetProverFlag.model_dump()) so tests can exercise values the
    # schema itself would reject before reaching the per-flag validation.
    return {"type": "set_flag", "flag": flag, "value": value}


def _edit(*edits: dict) -> ToolCallDict:
    return tool_call_raw(_EDIT, edits=list(edits))


# ---------------------------------------------------------------------------
# Scenario builder + extractors
# ---------------------------------------------------------------------------


def _scenario(
    files: list[str] | None = None,
    links: list[str] | None = None,
):
    config: dict = {}
    if files is not None:
        config["files"] = files
    if links is not None:
        config["link"] = links
    return Scenario(ConfigTestState, TOOL).init(
        config=config, rule_skips={},
    )


def _config(st: ConfigTestState) -> dict:
    return st["config"]


def _files(st: ConfigTestState) -> list[str]:
    return st["config"]["files"]


def _links(st: ConfigTestState) -> list[str]:
    return st["config"]["link"]


def _edit_response(st: ConfigTestState) -> str:
    return Scenario.last_single_tool(_EDIT, st)


# =========================================================================
# AddFile
# =========================================================================


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
# AddLink
# =========================================================================


class TestAddLink:
    async def test_sol_extension_rejected(self):
        links = await _scenario(files=[], links=[]).turn(
            _edit(_add_link("Foo.sol", "token", "Bar")),
        ).map_run(_links)
        assert links == []

    async def test_duplicate_link_rejected(self):
        links = await _scenario(files=[], links=["Foo:token=Bar"]).turn(
            _edit(_add_link("Foo", "token", "Baz")),
        ).map_run(_links)
        assert links == ["Foo:token=Bar"]

    async def test_add_link_appends(self):
        links = await _scenario(files=[], links=[]).turn(
            _edit(_add_link("Foo", "token", "Bar")),
        ).map_run(_links)
        assert "Foo:token=Bar" in links

    async def test_add_link_preserves_existing(self):
        links = await _scenario(files=[], links=["A:b=C"]).turn(
            _edit(_add_link("Foo", "token", "Bar")),
        ).map_run(_links)
        assert "A:b=C" in links
        assert "Foo:token=Bar" in links


# =========================================================================
# RemoveLink
# =========================================================================


class TestRemoveLink:
    async def test_remove_link(self):
        links = await _scenario(files=[], links=["Foo:token=Bar", "A:b=C"]).turn(
            _edit(_remove_link("Foo", "token")),
        ).map_run(_links)
        assert links == ["A:b=C"]

    async def test_remove_no_links_configured(self):
        config = await _scenario(files=[]).turn(
            _edit(_remove_link("Foo", "token")),
        ).map_run(_config)
        assert "link" not in config

    async def test_remove_link_not_found(self):
        links = await _scenario(files=[], links=["A:b=C"]).turn(
            _edit(_remove_link("Foo", "token")),
        ).map_run(_links)
        assert links == ["A:b=C"]


# =========================================================================
# SetProverFlag
# =========================================================================


class TestSetProverFlag:
    async def test_set_optimistic_fallback(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("optimistic_fallback", True)),
        ).map_run(_config)
        assert config["optimistic_fallback"] is True

    async def test_optimistic_fallback_rejects_int(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("optimistic_fallback", 1)),
        ).map_run(_config)
        assert "optimistic_fallback" not in config

    async def test_set_loop_iter(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("loop_iter", 3)),
        ).map_run(_config)
        assert config["loop_iter"] == 3

    async def test_loop_iter_out_of_range(self):
        for bad in (0, 9):
            config = await _scenario(files=[]).turn(
                _edit(_set_flag("loop_iter", bad)),
            ).map_run(_config)
            assert "loop_iter" not in config

    async def test_loop_iter_rejects_bool(self):
        """bool is an int subclass; the validator must not accept True as a loop count."""
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("loop_iter", True)),
        ).map_run(_config)
        assert "loop_iter" not in config

    async def test_set_global_timeout(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("global_timeout", 3600)),
        ).map_run(_config)
        assert config["global_timeout"] == 3600

    async def test_global_timeout_too_large(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("global_timeout", 7201)),
        ).map_run(_config)
        assert "global_timeout" not in config

    async def test_set_contract_recursion_limit(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("contract_recursion_limit", 2)),
        ).map_run(_config)
        assert config["contract_recursion_limit"] == 2

    async def test_contract_recursion_limit_too_large(self):
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("contract_recursion_limit", 11)),
        ).map_run(_config)
        assert "contract_recursion_limit" not in config

    async def test_sanity_and_optimistic_loop_not_settable(self):
        """The flags vacuity detection depends on (forced by prover_config_overlay)
        are rejected at the schema level, not just by run-time validation."""
        for forbidden in ("rule_sanity", "optimistic_loop"):
            with pytest.raises(ValidationError):
                SetProverFlag(type="set_flag", flag=forbidden, value=True)  # type: ignore[arg-type]

    async def test_failed_flag_aborts_batch(self):
        """Atomicity: a rejected flag value rolls back the whole edit list."""
        config = await _scenario(files=[]).turn(
            _edit(_set_flag("loop_iter", 3), _set_flag("global_timeout", 99999)),
        ).map_run(_config)
        assert "loop_iter" not in config
        assert "global_timeout" not in config


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
