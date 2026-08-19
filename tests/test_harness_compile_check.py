"""Unit tests for the harness compile gate (``composer.spec.source.harness``)."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from composer.spec.source.harness import ForgeReport, _compile_check

ABSTRACT_ERROR = {
    "severity": "error",
    "errorCode": "3656",
    "message": 'Contract "TokenInstance1" should be marked as abstract.',
    "formattedMessage": (
        'TypeError: Contract "TokenInstance1" should be marked as abstract.\n'
        " --> certora/harnesses/TokenInstance1.sol:7:1:\n"
        "Note: Missing implementation: \n"
        "   --> src/interfaces/IToken.sol:543:5:\n"
    ),
}

TRANSIENT_WARNING = {
    "severity": "warning",
    "errorCode": "2394",
    "message": "Transient storage can break composability",
    "formattedMessage": "Warning: Transient storage can break composability",
}


def _report(*diagnostics: dict) -> str:
    return json.dumps({
        "errors": list(diagnostics),
        "sources": {},
        "contracts": {},
        "build_infos": [],
    })


def test_report_keeps_errors_and_drops_warnings():
    report = ForgeReport.model_validate_json(_report(TRANSIENT_WARNING, ABSTRACT_ERROR))
    assert report.compile_errors == [ABSTRACT_ERROR["formattedMessage"]]


def test_report_falls_back_to_the_bare_message():
    report = ForgeReport.model_validate_json(
        _report({"severity": "error", "message": "Source not found"})
    )
    assert report.compile_errors == ["Source not found"]


def test_report_empty_on_clean_build():
    assert ForgeReport.model_validate_json(_report(TRANSIENT_WARNING)).compile_errors == []
    assert ForgeReport.model_validate_json('{"sources": {}}').compile_errors == []


def test_report_rejects_output_that_is_not_a_report():
    # forge failed before compiling — nothing to hold the harnesses against.
    for output in ("Error: failed to resolve remappings", "", "[1, 2]"):
        with pytest.raises(ValidationError):
            ForgeReport.model_validate_json(output)


@pytest.mark.asyncio
async def test_compile_check_skipped_without_a_foundry_project(tmp_path):
    assert await _compile_check(str(tmp_path), ["certora/harnesses/TokenInstance1.sol"]) is None


@pytest.mark.asyncio
async def test_compile_check_builds_the_delivered_harnesses(tmp_path, monkeypatch):
    """forge is invoked on the delivered paths, inside the directory it was
    handed — the materialized project, whose layout the paths already match."""
    import composer.spec.source.harness as harness_mod

    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    monkeypatch.setattr(harness_mod.shutil, "which", lambda _: "/usr/local/bin/forge")
    invocations = []

    class _Proc:
        async def communicate(self):
            return _report(ABSTRACT_ERROR).encode(), b""

    async def _fake_exec(*cmd, **kwargs):
        invocations.append((cmd, kwargs["cwd"]))
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    result = await _compile_check(
        str(tmp_path),
        ["certora/harnesses/TokenInstance2.sol", "certora/harnesses/TokenInstance1.sol"],
    )
    assert result is not None
    assert "certora/harnesses/TokenInstance1.sol" in result
    (cmd, cwd) = invocations[0]
    assert cmd[1:] == (
        "build",
        "--json",
        "certora/harnesses/TokenInstance1.sol",
        "certora/harnesses/TokenInstance2.sol",
    )
    assert str(cwd) == str(tmp_path)


@pytest.mark.asyncio
async def test_compile_check_accepts_a_clean_build(tmp_path, monkeypatch):
    import composer.spec.source.harness as harness_mod

    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    monkeypatch.setattr(harness_mod.shutil, "which", lambda _: "/usr/local/bin/forge")

    class _Proc:
        async def communicate(self):
            return _report(TRANSIENT_WARNING).encode(), b""

    async def _fake_exec(*cmd, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    assert await _compile_check(str(tmp_path), ["certora/harnesses/TokenInstance1.sol"]) is None


@pytest.mark.asyncio
async def test_result_tool_sees_the_vfs_without_exposing_it(tmp_path):
    """Pins the contract the gate depends on: an ``AsyncResultTool`` that also
    carries injected state reads the VFS in its validator, while the model
    still sees only ``value``. A rejection is returned to the model and leaves
    the result unset; an acceptance stores the validated model.
    """
    from typing import NotRequired, override

    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode
    from pydantic import BaseModel

    from composer.spec.natspec.async_result import AsyncResultTool
    from graphcore.tools.schemas import WithInjectedState
    from graphcore.tools.vfs import VFSState

    class _Result(BaseModel):
        note: str

    class _State(MessagesState, VFSState):
        result: NotRequired[_Result]

    vfs_seen = []

    class _ResultTool(AsyncResultTool[_Result], WithInjectedState[_State]):
        """Signal the completion of your workflow."""

        @override
        async def validate_result(self, res: _Result) -> str | None:
            vfs_seen.append(sorted(self.state["vfs"]))
            return None if res.note == "ok" else f"rejected: {res.note}"

    tool = _ResultTool.as_tool("result")
    assert list(tool.args) == ["value"]

    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()

    def _call(note: str, call_id: str) -> dict:
        return {
            "messages": [AIMessage(content="", tool_calls=[{
                "name": "result",
                "args": {"value": {"note": note}},
                "id": call_id,
                "type": "tool_call",
            }])],
            "vfs": {"certora/harnesses/TokenInstance1.sol": "contract TokenInstance1 is Token { }"},
        }

    rejected = await app.ainvoke(_call("bad", "c1"))
    assert rejected["messages"][-1].content == "rejected: bad"
    assert "result" not in rejected
    assert vfs_seen == [["certora/harnesses/TokenInstance1.sol"]]

    accepted = await app.ainvoke(_call("ok", "c2"))
    assert accepted["result"] == _Result(note="ok")
