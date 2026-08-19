"""Tests for the generic workspace prep and the per-chain seam under it
(``composer.rustapp.adapter.run_workspace_prep`` / ``composer.rustapp.toolchain``).

The prep has two halves, and the split is the point: the host writes the plan's ``files`` itself
(that is the same in every ecosystem), and hands the plan's ``toolchain_request`` to the chain's
registered :class:`ProjectToolchain`, which is the only part that has to know what
``cargo-build-sbf`` or an IDL is. Everything crossing that seam is chain-shaped and opaque here —
these tests assert the framework *transports* it, never that it understands it. The ``source_unit``
half of the same seam has the opposite failure mode: unregistered, it answers "nothing resolved"
rather than raising, because that is a state the wheel already handles.

Fake wheel, fake chain — no toolchain, no LLM.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from composer.pipeline.ecosystem import EVM, SOLANA
from composer.rustapp.adapter import run_workspace_prep, source_unit_of
from composer.rustapp.toolchain import PROJECT_TOOLCHAINS
from composer.rustapp.wire import PreflightInput
from composer.spec.context import SourceFields
from composer.spec.system_model import SolidityIdentifier
from tests.conftest import wire_workspace_prep

pytestmark = pytest.mark.asyncio

#: What a registered toolchain reports about the analyzed project. Framework-side this is an opaque
#: object; that these keys spell a Cargo crate is the *chain's* business.
SOURCE_UNIT = {"dir": "programs/lend", "package": "example-lending", "lib": "example_lending"}
#: A chain-shaped prep request, and the facts carrying it out established.
REQUEST = {"warm_dirs": ["fuzz/vault"], "build_program": "example_lending",
           "idl_dest": "fuzz/vault/idls/example_lending.json"}
PREP_FACTS = {"idl": "fuzz/vault/idls/example_lending.json"}


class FakeWheel:
    """A wheel whose ``workspace_prep`` returns a fixed plan."""

    def __init__(self, **plan):
        self._plan = wire_workspace_prep(**plan)

    def workspace_prep(self, _input_json: str) -> str:
        return json.dumps(self._plan)


class FakeToolchain:
    """A chain implementation that records every call and reports fixed facts."""

    def __init__(self, *, source_unit: dict[str, Any], prep_facts: dict[str, Any]):
        self._source_unit = source_unit
        self._prep_facts = prep_facts
        self.calls: list[dict[str, Any]] = []

    def source_unit(self, _source: SourceFields) -> dict[str, Any]:
        return self._source_unit

    async def prepare(self, plan, input, *, source, sandbox, timeout_s) -> dict[str, Any]:
        self.calls.append({
            "plan": plan, "input": input, "source": source,
            "sandbox": sandbox, "timeout_s": timeout_s,
        })
        return self._prep_facts


def _source(root: Path) -> SourceFields:
    return SourceFields(
        project_root=str(root),
        contract_name=SolidityIdentifier("vault"),
        relative_path="programs/lend/src/lib.rs",
        forbidden_read="",
    )


def _register(
    monkeypatch, *, source_unit: dict[str, Any] | None = None,
    prep_facts: dict[str, Any] | None = None,
) -> FakeToolchain:
    toolchain = FakeToolchain(source_unit=source_unit or {}, prep_facts=prep_facts or {})
    monkeypatch.setitem(PROJECT_TOOLCHAINS, "solana", toolchain)
    return toolchain


async def _prep(wheel, root: Path, args: dict | None = None) -> dict[str, Any]:
    return await run_workspace_prep(
        wheel,
        PreflightInput(program="vault", source_unit=SOURCE_UNIT, args=args or {}),
        chain="solana",
        source=_source(root),
        sandbox=None, command_timeout_s=60,
    )


async def test_a_files_only_plan_is_complete_once_they_are_written(tmp_path, monkeypatch):
    # An empty toolchain_request asks for nothing, so no toolchain is consulted. This is what makes
    # an empty PROJECT_TOOLCHAINS a resting state rather than a broken one.
    toolchain = _register(monkeypatch)
    wheel = FakeWheel(files={"fuzz/vault/Cargo.toml": "[package]\n", "fuzz/vault/src/lib.rs": "//"})

    assert await _prep(wheel, tmp_path) == {}
    assert (tmp_path / "fuzz/vault/Cargo.toml").read_text() == "[package]\n"
    assert (tmp_path / "fuzz/vault/src/lib.rs").read_text() == "//"
    assert toolchain.calls == []


async def test_wheel_written_files_are_path_confined(tmp_path, monkeypatch):
    # The wheel is trusted, but its file paths go through the same confinement as everything else
    # the host writes on its behalf — a traversal is refused rather than followed.
    _register(monkeypatch)
    with pytest.raises(ValueError, match="unsafe file path"):
        await _prep(FakeWheel(files={"../../etc/evil": "x"}), tmp_path)
    assert not (tmp_path.parent.parent / "etc" / "evil").exists()


async def test_the_toolchain_half_is_handed_the_plan_and_the_analyzed_source(tmp_path, monkeypatch):
    toolchain = _register(monkeypatch, prep_facts=PREP_FACTS)
    wheel = FakeWheel(files={"fuzz/vault/Cargo.toml": "[package]\n"}, toolchain_request=REQUEST)

    # Whatever the toolchain reports having established is what the caller reports back.
    assert await _prep(wheel, tmp_path, {"program_idl": "/elsewhere/lend.json"}) == PREP_FACTS
    assert len(toolchain.calls) == 1
    call = toolchain.calls[0]
    # The request reaches it whole and uninterpreted — the host never reads a key of it, which is
    # what lets a chain add one without touching composer.rustapp.wire.
    assert call["plan"].toolchain_request == REQUEST
    # The analyzed source, not just its root — an implementation resolves its own project facts from
    # `relative_path` rather than being handed a shape the framework would have to understand.
    assert call["source"].project_root == str(tmp_path)
    assert call["source"].relative_path == "programs/lend/src/lib.rs"
    # …and the whole AuthorInput, so it can read its own declared args (Crucible's `program_idl`)
    # without the generic host having to know which flags mean anything.
    assert call["input"].args["program_idl"] == "/elsewhere/lend.json"
    assert call["timeout_s"] == 60
    # The files land before the toolchain runs: the manifest a warm or build reads is one of them.
    assert (tmp_path / "fuzz/vault/Cargo.toml").is_file()


async def test_a_plan_needing_an_unregistered_toolchain_fails_loudly(tmp_path, monkeypatch):
    # Skipping the preparation the wheel asked for would surface much later as a compile error the
    # authoring agent cannot fix, so the seam refuses up front and names the fix.
    monkeypatch.delitem(PROJECT_TOOLCHAINS, "solana", raising=False)
    wheel = FakeWheel(toolchain_request={"build_program": "example_lending"})

    with pytest.raises(ValueError, match="no project toolchain is registered"):
        await _prep(wheel, tmp_path)


async def test_the_source_unit_comes_from_the_chains_toolchain(tmp_path, monkeypatch):
    _register(monkeypatch, source_unit=SOURCE_UNIT)
    assert source_unit_of(SOLANA, _source(tmp_path)) == SOURCE_UNIT


async def test_an_unresolved_source_unit_is_empty_rather_than_an_error(tmp_path, monkeypatch):
    # Three different situations answer the same way — no toolchain for the chain, a language with no
    # such unit (Solidity), and a layout the toolchain couldn't read — because the wheel does the
    # same thing with all three: fall back to its own convention.
    monkeypatch.delitem(PROJECT_TOOLCHAINS, "solana", raising=False)
    assert source_unit_of(SOLANA, _source(tmp_path)) == {}
    assert source_unit_of(EVM, _source(tmp_path)) == {}

    _register(monkeypatch, source_unit={})
    assert source_unit_of(SOLANA, _source(tmp_path)) == {}
