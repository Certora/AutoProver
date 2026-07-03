"""
Tests for the write_mock tool and the state-held mock lifecycle: mocks are
recorded in graph state under a per-generation namespace (never written to the
shared project tree during generation), materialized on disk only for the
duration of a prover run, and persisted by the artifact store for completed
generations so cache replay works from a fresh checkout.
"""
import pytest

from langgraph.graph import MessagesState

from composer.prover.core import ProverOptions, ProverReport
from composer.spec.cvl_generation import GeneratedCVL
from composer.spec.source.artifacts import ComponentSpec, ProverArtifactStore
from composer.spec.source.author import WriteMockTool
from composer.spec.source.prover import ProverStateExtra, get_prover_tool

from graphcore.testing import Scenario, ToolCallDict, tool_call_raw

pytestmark = pytest.mark.asyncio


_CONTENT = "// SPDX-License-Identifier: MIT\ncontract MockOracle { function price() external pure returns (uint256) { return 1; } }\n"
_NAMESPACE = "autospec_oracle"
_MOCK_PATH = f"certora/mocks/{_NAMESPACE}/MockOracle.sol"


class MockTestState(MessagesState, ProverStateExtra):
    pass


_WRITE = "write_mock"


def _scenario():
    return Scenario(MockTestState, WriteMockTool.bind(_NAMESPACE).as_tool(_WRITE)).init(
        config={}, rule_skips={}, mocks={},
    )


def _write(file_name: str, content: str = _CONTENT) -> ToolCallDict:
    return tool_call_raw(_WRITE, file_name=file_name, content=content)


# =========================================================================
# State updates (no disk writes during generation)
# =========================================================================


async def test_records_mock_in_state_under_namespace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # would expose any accidental relative-path disk write
    mocks = await _scenario().turn(_write("MockOracle.sol")).map_run(lambda st: st["mocks"])
    assert mocks == {_MOCK_PATH: _CONTENT}
    # Nothing lands on the shared tree at write time.
    assert not (tmp_path / "certora").exists()


async def test_response_names_path_and_edit_config():
    msg = await _scenario().turn(_write("MockOracle.sol")).run_last_single_tool(_WRITE)
    assert _MOCK_PATH in msg
    assert "edit_config" in msg


async def test_overwrite_replaces_state_entry():
    mocks = await _scenario().turns(
        _write("M.sol", "// v1"),
        _write("M.sol", "// v2"),
    ).map_run(lambda st: st["mocks"])
    assert mocks == {f"certora/mocks/{_NAMESPACE}/M.sol": "// v2"}


async def test_rejects_directories_and_traversal():
    for bad in ("sub/Evil.sol", "../Evil.sol", "..", "/Evil.sol", "certora/mocks/Evil.sol"):
        mocks = await _scenario().turn(_write(bad)).map_run(lambda st: st["mocks"])
        assert mocks == {}


async def test_rejects_non_solidity():
    mocks = await _scenario().turn(_write("notes.txt", "hi")).map_run(lambda st: st["mocks"])
    assert mocks == {}


# =========================================================================
# Materialization around prover runs
# =========================================================================


async def test_mocks_materialized_only_during_prover_run(tmp_path, monkeypatch, fake_llm):
    """The state-held mock must exist on disk while run_prover executes (the
    prover compiles from the real tree) and be gone afterwards (a gave-up
    generation must not leave stray sources)."""
    target = tmp_path / _MOCK_PATH
    seen: list[bool] = []

    async def observing_prover(*args, **kwargs):
        seen.append(target.read_text() == _CONTENT if target.exists() else False)
        return ProverReport(rule_status={"r": True}, result_str="ok", link="local://x")

    monkeypatch.setattr("composer.spec.source.prover.run_prover", observing_prover)
    monkeypatch.setattr(
        "composer.spec.source.prover.get_stream_writer", lambda: (lambda _: None)
    )
    prover_tool = get_prover_tool(
        prover_opts=ProverOptions(), llm=fake_llm,
        main_contract="Dummy", project_root=str(tmp_path),
    )
    from composer.spec.source.prover import StateWithSkips
    await Scenario(StateWithSkips, prover_tool).init(
        curr_spec="rule r { assert true; }",
        skipped=[], property_rules=[], validations={}, required_validations=[],
        rule_skips={}, vacuous_methods={}, acknowledged_vacuous={},
        mocks={_MOCK_PATH: _CONTENT},
        config={"files": ["src/Foo.sol", _MOCK_PATH]},
    ).turn(tool_call_raw("verify_spec", rules=None)).map_run(lambda st: st)
    assert seen == [True]
    assert not target.exists()


# =========================================================================
# Artifact-store persistence (cache replay from a fresh checkout)
# =========================================================================


def _generated(mocks: dict[str, str]) -> GeneratedCVL:
    return GeneratedCVL(
        commentary="c", cvl="rule r { assert true; }",
        config={"files": ["src/Foo.sol", *mocks]},
        mocks=mocks,
    )


async def test_artifact_store_persists_mocks(tmp_path):
    store = ProverArtifactStore(str(tmp_path), "Dummy")
    store.write_generated_spec(ComponentSpec("oracle"), _generated({_MOCK_PATH: _CONTENT}))
    assert (tmp_path / _MOCK_PATH).read_text() == _CONTENT
    # The conf referencing the mock is written alongside it.
    conf = (tmp_path / "certora" / "confs" / "autospec_oracle.conf").read_text()
    assert _MOCK_PATH in conf


async def test_artifact_store_rejects_out_of_tree_mock_paths(tmp_path):
    store = ProverArtifactStore(str(tmp_path), "Dummy")
    for bad in ("src/Evil.sol", "certora/mocks/../../Evil.sol", "/abs/Evil.sol"):
        with pytest.raises(AssertionError):
            store.write_generated_spec(ComponentSpec("oracle"), _generated({bad: "x"}))
