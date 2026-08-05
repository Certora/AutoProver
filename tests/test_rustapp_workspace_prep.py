"""Tests for the generic workspace prep and the two per-chain seams under it
(``composer.rustapp.adapter.run_workspace_prep`` / ``composer.rustapp.toolchain``).

The prep has two halves, and the split is the point: the host writes the plan's ``files`` itself
(that is the same in every ecosystem), and hands anything further — warm these dirs, build this
program, place its IDL — to the chain's registered :class:`WorkspaceToolchain`, which is the only
part that has to know what ``cargo-build-sbf`` or an IDL is. The crate resolver behind
``AuthorInput.program_crate`` is the same story with the opposite failure mode: unregistered, it
answers "nothing resolved" rather than raising, because that is a state the wheel already handles.
Fake wheel, fake chain — no toolchain, no LLM.
"""

import json
from pathlib import Path

import pytest

from composer.pipeline.ecosystem import EVM, SOLANA
from composer.rustapp.adapter import program_crate_of, run_workspace_prep
from composer.rustapp.toolchain import SOURCE_CRATES, WORKSPACE_TOOLCHAINS
from composer.rustapp.wire import PreflightInput, ProgramCrate
from composer.spec.context import SourceFields
from composer.spec.system_model import SolidityIdentifier

pytestmark = pytest.mark.asyncio

#: What a registered resolver reports. Framework-side these are opaque strings; deriving them from a
#: Cargo manifest is the registered resolver's business, not the framework's.
CRATE = ProgramCrate(dir="programs/lend", package="example-lending", lib="example_lending")
IDL_DEST = "fuzz/vault/idls/example_lending.json"


class FakeWheel:
    """A wheel whose ``workspace_prep`` returns a fixed plan."""

    def __init__(self, **plan):
        self._plan = {"files": {}, **plan}

    def workspace_prep(self, _input_json: str) -> str:
        return json.dumps(self._plan)


def _source(root: Path) -> SourceFields:
    return SourceFields(
        project_root=str(root),
        contract_name=SolidityIdentifier("vault"),
        relative_path="programs/lend/src/lib.rs",
        forbidden_read="",
    )


def _record_toolchain(monkeypatch, *, idl: str | None = None) -> list[dict]:
    """Register a fake toolchain for ``solana`` that records every call it gets."""
    calls: list[dict] = []

    async def fake_prepare(plan, input, *, source, sandbox, timeout_s):
        calls.append({
            "plan": plan, "input": input, "source": source,
            "sandbox": sandbox, "timeout_s": timeout_s,
        })
        return idl

    monkeypatch.setitem(WORKSPACE_TOOLCHAINS, "solana", fake_prepare)
    return calls


async def _prep(wheel, root: Path, args: dict | None = None) -> str | None:
    return await run_workspace_prep(
        wheel,
        PreflightInput(program="vault", program_crate=CRATE, args=args or {}),
        chain="solana",
        source=_source(root),
        sandbox=None, command_timeout_s=60,
    )


async def test_a_files_only_plan_is_complete_once_they_are_written(tmp_path, monkeypatch):
    # No warm, no build, no IDL — so nothing that needs a toolchain, and none is consulted. This is
    # what makes an empty WORKSPACE_TOOLCHAINS a resting state rather than a broken one.
    calls = _record_toolchain(monkeypatch)
    wheel = FakeWheel(files={"fuzz/vault/Cargo.toml": "[package]\n", "fuzz/vault/src/lib.rs": "//"})

    assert await _prep(wheel, tmp_path) is None
    assert (tmp_path / "fuzz/vault/Cargo.toml").read_text() == "[package]\n"
    assert (tmp_path / "fuzz/vault/src/lib.rs").read_text() == "//"
    assert calls == []


async def test_wheel_written_files_are_path_confined(tmp_path, monkeypatch):
    # The wheel is trusted, but its file paths go through the same confinement as everything else
    # the host writes on its behalf — a traversal is refused rather than followed.
    _record_toolchain(monkeypatch)
    with pytest.raises(ValueError, match="unsafe file path"):
        await _prep(FakeWheel(files={"../../etc/evil": "x"}), tmp_path)
    assert not (tmp_path.parent.parent / "etc" / "evil").exists()


async def test_the_toolchain_half_is_handed_the_plan_and_the_analyzed_source(tmp_path, monkeypatch):
    calls = _record_toolchain(monkeypatch, idl=IDL_DEST)
    wheel = FakeWheel(
        files={"fuzz/vault/Cargo.toml": "[package]\n"},
        warm_dirs=["fuzz/vault"], build_program="example_lending", idl_dest=IDL_DEST,
    )

    # Whatever the toolchain reports as the IDL's home is what the caller reports back as `idl`.
    assert await _prep(wheel, tmp_path, {"program_idl": "/elsewhere/lend.json"}) == IDL_DEST
    assert len(calls) == 1
    call = calls[0]
    # The plan reaches it whole: which dirs to warm, which program to build, where the IDL goes.
    assert call["plan"].warm_dirs == ["fuzz/vault"]
    assert call["plan"].build_program == "example_lending"
    assert call["plan"].idl_dest == IDL_DEST
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
    # Skipping the build the wheel asked for would surface much later as a compile error the
    # authoring agent cannot fix, so the seam refuses up front and names the fix.
    monkeypatch.delitem(WORKSPACE_TOOLCHAINS, "solana", raising=False)
    wheel = FakeWheel(build_program="example_lending")

    with pytest.raises(ValueError, match="no workspace toolchain is registered"):
        await _prep(wheel, tmp_path)


async def test_the_program_crate_comes_from_the_chains_resolver(tmp_path, monkeypatch):
    monkeypatch.setitem(SOURCE_CRATES, "solana", lambda _source: CRATE)
    assert program_crate_of(SOLANA, _source(tmp_path)) == CRATE


async def test_an_unresolved_program_crate_is_empty_rather_than_an_error(tmp_path, monkeypatch):
    # Three different situations answer the same way — no resolver for the chain, a language with no
    # compilation unit (Solidity), and a layout the resolver couldn't read — because the wheel does
    # the same thing with all three: fall back to its own convention (`ProgramCrate::resolved`).
    monkeypatch.delitem(SOURCE_CRATES, "solana", raising=False)
    assert program_crate_of(SOLANA, _source(tmp_path)) == ProgramCrate()
    assert program_crate_of(EVM, _source(tmp_path)) == ProgramCrate()

    monkeypatch.setitem(SOURCE_CRATES, "solana", lambda _source: ProgramCrate())
    assert program_crate_of(SOLANA, _source(tmp_path)) == ProgramCrate()
