"""Tests for the foundry runner's build-failure handling.

When the LLM-authored draft fails to compile, ``forge test`` exits non-zero and
prints no JSON, so ``_parse_forge_json`` returns ``None`` and the runner surfaces
the raw solc output, led by a nudge to fix the compile error first (a whole
campaign dies on one compile error).
"""

import asyncio

import pytest

from composer.foundry import runner as runner_mod
from composer.foundry.runner import (
    ForgeTestDeps,
    ForgeTestTool,
    _parse_forge_json,
)


def test_parse_forge_json_returns_none_on_non_json() -> None:
    # Compile failures print human solc output, not JSON -> None triggers the branch.
    assert _parse_forge_json("") is None
    assert _parse_forge_json("Compiler run failed:\nError (8936): ...") is None


def test_parse_forge_json_parses_a_minimal_report() -> None:
    stdout = (
        '{"src/test/C.t.sol:C": {"test_results": '
        '{"test_Foo(uint256)": {"status": "Success"}}}}'
    )
    results = _parse_forge_json(stdout)
    assert results is not None
    assert [(r.name, r.status) for r in results] == [("test_Foo", "Success")]


def _min_state(curr_test: str) -> dict:
    """The minimal FoundryGenerationState the runner's build-failure branch reads."""
    return {
        "messages": [],
        "curr_test": curr_test,
        "skipped": [],
        "property_tests": [],
        "validations": {},
        "required_validations": [],
        "expected_failures": {},
        "last_test_names": ["stale_name"],
        "failed": None,
    }


@pytest.mark.asyncio
async def test_forge_test_build_failure_leads_with_hint(tmp_path, monkeypatch) -> None:
    """A compile failure returns the fix-the-build-first lead, keeps the raw solc
    output, and clears the recorded test names (no runnable buffer anymore)."""

    class _FakeProc:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            # Non-JSON stdout => _parse_forge_json returns None; solc error on stderr.
            return (b"", b"Error (8936): Identifier-start is not allowed at end of a number.\n")

    async def _fake_exec(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(runner_mod.asyncio, "create_subprocess_exec", _fake_exec)
    # get_stream_writer only works inside a langgraph runtime; stub it out.
    monkeypatch.setattr(runner_mod, "get_stream_writer", lambda: (lambda _event: None))

    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    deps = ForgeTestDeps(
        project_root=str(tmp_path), forge_binary="forge", timeout_s=600,
        sem=asyncio.Semaphore(1), test_root="test",
    )
    tool = ForgeTestTool(state=_min_state("contract C { }"), tool_call_id="t1", seed=None)

    # Mirror ToolBuilder.as_tool: set the dep context around run() (avoids
    # standing up a full langgraph runtime just to inject deps).
    token = ForgeTestTool._dep_ctx.set(deps)
    try:
        result = await tool.run()
    finally:
        ForgeTestTool._dep_ctx.reset(token)

    assert not isinstance(result, str)  # build-failure branch returns a Command
    content = result.update["messages"][-1].content
    assert content.startswith("The project failed to BUILD")
    assert "Error (8936)" in content        # raw solc output still surfaced
    assert result.update["last_test_names"] == []
