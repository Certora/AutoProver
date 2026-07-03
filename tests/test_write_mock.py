"""
Tests for the write_mock tool: real-filesystem writes confined to certora/mocks.
"""
import pytest

from composer.spec.source.author import WriteMockTool

pytestmark = pytest.mark.asyncio


_CONTENT = "// SPDX-License-Identifier: MIT\ncontract MockOracle { function price() external pure returns (uint256) { return 1; } }\n"


def _tool(project_root) -> object:
    return WriteMockTool.bind(str(project_root)).as_tool("write_mock")


async def test_writes_under_mocks_dir(tmp_path):
    res = await _tool(tmp_path).ainvoke(
        {"file_path": "certora/mocks/MockOracle.sol", "content": _CONTENT}
    )
    written = tmp_path / "certora" / "mocks" / "MockOracle.sol"
    assert written.read_text() == _CONTENT
    # The result must nudge the agent to register the mock in the prover config.
    assert "edit_config" in res


async def test_overwrite_allowed(tmp_path):
    tool = _tool(tmp_path)
    await tool.ainvoke({"file_path": "certora/mocks/M.sol", "content": "// v1"})
    await tool.ainvoke({"file_path": "certora/mocks/M.sol", "content": "// v2"})
    assert (tmp_path / "certora" / "mocks" / "M.sol").read_text() == "// v2"


async def test_rejects_write_outside_mocks(tmp_path):
    for bad in (
        "src/Evil.sol",
        "certora/harnesses/Evil.sol",
        "certora/mocks.sol",  # file named like the dir, still outside it
    ):
        res = await _tool(tmp_path).ainvoke({"file_path": bad, "content": _CONTENT})
        assert "only write into" in res
    assert not (tmp_path / "src").exists()


async def test_rejects_traversal_and_absolute(tmp_path):
    for bad in ("certora/mocks/../../etc/Evil.sol", "/certora/mocks/Evil.sol"):
        res = await _tool(tmp_path).ainvoke({"file_path": bad, "content": _CONTENT})
        assert "Invalid path" in res


async def test_rejects_non_solidity(tmp_path):
    res = await _tool(tmp_path).ainvoke(
        {"file_path": "certora/mocks/notes.txt", "content": "hi"}
    )
    assert ".sol" in res
    assert not (tmp_path / "certora" / "mocks" / "notes.txt").exists()
