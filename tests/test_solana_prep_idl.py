"""Tests for the IDL half of the workspace prep (``composer.spec.solana.project.SolanaToolchain``,
reached through ``composer.rustapp.adapter.run_workspace_prep`` and the registered project seam).

A wheel that cannot link the program under test asks for its **IDL** instead (``idl_dest`` in the
plan's chain-shaped ``toolchain_request`` — see ``autoprover_solana::SolanaPrep``); the host obtains
one, places it where the wheel asked, and reports the path back as a prep *fact*, so the rest of the
run renders the same crate. The framework knows none of this vocabulary, which is why these tests
live with the chain: they are Solana's contract, not the host's.

Fake wheel, fake build capability — no toolchain, no LLM.
"""

import json
from pathlib import Path

import pytest

import composer.spec.solana.project as projectmod
from composer.rustapp.adapter import run_workspace_prep
from composer.rustapp.wire import PreflightInput
from composer.spec.cargo import ProgramCrate
from composer.spec.context import SourceFields
from composer.spec.solana.build import BuiltProgram
from composer.spec.solana.project import SolanaSourceUnit
from composer.spec.system_model import SolidityIdentifier

pytestmark = pytest.mark.asyncio

#: The program id. An IDL must name the program's address; these fakes carry it except where a
#: test is specifically about filling it in.
ADDR = "LendvUkXRmuDKxGCCFJra9uxWMdMooPEmJk3qp7Tg1Z"
#: The crate the prep resolves for itself from the analyzed source, and the chain-shaped copy the
#: wheel is sent — the IDL normalization reads the real names, so it re-resolves rather than reading
#: the copy.
CRATE = ProgramCrate(dir="programs/lend", package="example_lending", lib="example_lending")
SOURCE_UNIT = SolanaSourceUnit.of(CRATE).model_dump()


def _source(root) -> SourceFields:
    return SourceFields(
        project_root=str(root),
        contract_name=SolidityIdentifier("vault"),
        relative_path=f"{CRATE.dir}/src/lib.rs",
        forbidden_read="",
    )


def _project(root) -> None:
    """The minimum Cargo layout `prepare_workspace`'s own crate resolution reads."""
    (root / CRATE.dir / "src").mkdir(parents=True, exist_ok=True)
    (root / CRATE.dir / "src" / "lib.rs").write_text("// program")
    (root / CRATE.dir / "Cargo.toml").write_text(f'[package]\nname = "{CRATE.package}"\n')
    (root / "Cargo.toml").write_text(f'[workspace]\nmembers = ["{CRATE.dir}"]\n')


class FakeWheel:
    """A wheel whose ``workspace_prep`` returns a fixed plan."""

    def __init__(self, **request):
        self._plan = {
            "files": {},
            "toolchain_request": {"warm_dirs": ["fuzz/vault"], **request},
        }

    def workspace_prep(self, _input_json: str) -> str:
        return json.dumps(self._plan)


def _fake_build(monkeypatch, root: Path, *, emits_idl: bool, address: str | None = ADDR):
    """Stub the shared build capability, recording the ``with_idl`` it was asked for."""
    calls: list[bool] = []

    async def fake_build_program(project_root, program, *, with_idl=False, **_kw):
        calls.append(with_idl)
        idl = None
        if with_idl and emits_idl:
            idl = Path(project_root) / "target" / "idl" / f"{program}.json"
            idl.parent.mkdir(parents=True, exist_ok=True)
            body: dict = {"from": "anchor idl build"}
            if address is not None:
                body["metadata"] = {"address": address}
            idl.write_text(json.dumps(body))
        return BuiltProgram(program=program, so_path=root / "x.so", idl_path=idl)

    # Patched where the seam *reads* it: `project` imported the name, so patching `build` would
    # leave that binding pointing at the real thing.
    monkeypatch.setattr(projectmod, "build_program", fake_build_program)
    return calls


async def _prep(wheel, root: Path, args: dict | None = None) -> dict:
    _project(root)
    return await run_workspace_prep(
        wheel,
        PreflightInput(program="vault", source_unit=SOURCE_UNIT, args=args or {}),
        chain="solana",
        source=_source(root),
        sandbox=None, command_timeout_s=60,
    )


async def test_no_idl_requested_builds_the_program_without_one(tmp_path, monkeypatch):
    calls = _fake_build(monkeypatch, tmp_path, emits_idl=True)
    wheel = FakeWheel(build_program="example_lending")
    # Nothing established: the prep placed no IDL, so the wheel depends on the program's crate.
    assert await _prep(wheel, tmp_path) == {}
    # The IDL build is extra work (and needs the program's anchor CLI): only when asked for.
    assert calls == [False]


async def test_requested_idl_is_built_and_placed_where_the_wheel_asked(tmp_path, monkeypatch):
    calls = _fake_build(monkeypatch, tmp_path, emits_idl=True)
    dest = "fuzz/vault/idls/example_lending.json"
    wheel = FakeWheel(build_program="example_lending", idl_dest=dest)

    assert await _prep(wheel, tmp_path) == {"idl": dest}
    assert calls == [True]
    # Placed inside the harness crate, so the delivered crate carries the IDL it was built against.
    placed = json.loads((tmp_path / dest).read_text())
    assert placed["from"] == "anchor idl build" and placed["metadata"]["address"] == ADDR


async def test_a_supplied_idl_wins_and_skips_the_idl_build(tmp_path, monkeypatch):
    # The usual reason a wheel needs an IDL is that the program's toolchain isn't installed — so
    # `anchor idl build` can't run, and --program-idl is the way in.
    calls = _fake_build(monkeypatch, tmp_path, emits_idl=False)
    supplied = tmp_path / "elsewhere" / "lend.json"
    supplied.parent.mkdir()
    supplied.write_text(json.dumps({"from": "the operator", "metadata": {"address": ADDR}}))
    dest = "fuzz/vault/idls/example_lending.json"
    wheel = FakeWheel(build_program="example_lending", idl_dest=dest)

    assert await _prep(wheel, tmp_path, {"program_idl": str(supplied)}) == {"idl": dest}
    assert calls == [False]
    assert json.loads((tmp_path / dest).read_text())["from"] == "the operator"


async def test_a_required_idl_that_cannot_be_produced_fails_with_the_way_out(tmp_path, monkeypatch):
    _fake_build(monkeypatch, tmp_path, emits_idl=False)
    wheel = FakeWheel(build_program="example_lending", idl_dest="fuzz/vault/idls/x.json")
    with pytest.raises(RuntimeError, match="--program-idl"):
        await _prep(wheel, tmp_path)


async def test_a_supplied_idl_that_does_not_exist_is_reported_as_such(tmp_path, monkeypatch):
    _fake_build(monkeypatch, tmp_path, emits_idl=True)
    wheel = FakeWheel(build_program="example_lending", idl_dest="fuzz/vault/idls/x.json")
    with pytest.raises(RuntimeError, match="no such file"):
        await _prep(wheel, tmp_path, {"program_idl": str(tmp_path / "nope.json")})


async def test_the_idl_destination_is_path_confined(tmp_path, monkeypatch):
    # `idl_dest` comes from the wheel, which is trusted — but it goes through the same confinement
    # as every other file the wheel writes, so a traversal is refused rather than followed.
    _fake_build(monkeypatch, tmp_path, emits_idl=True)
    wheel = FakeWheel(build_program="example_lending", idl_dest="../../etc/idl.json")
    with pytest.raises(ValueError, match="unsafe file path"):
        await _prep(wheel, tmp_path)
    assert not (tmp_path.parent.parent / "etc" / "idl.json").exists()


async def test_a_legacy_idl_gains_the_program_id_from_the_project(tmp_path, monkeypatch):
    # `anchor idl build` under 0.29 emits no address at all, and the type generator rejects such an
    # IDL — so prep fills it in from Anchor.toml rather than making that the operator's problem.
    _fake_build(monkeypatch, tmp_path, emits_idl=True, address=None)
    (tmp_path / "Anchor.toml").write_text(f'[programs.localnet]\nlend = "{ADDR}"\n')
    dest = "fuzz/vault/idls/example_lending.json"

    assert await _prep(
        FakeWheel(build_program="example_lending", idl_dest=dest), tmp_path
    ) == {"idl": dest}
    assert json.loads((tmp_path / dest).read_text())["metadata"]["address"] == ADDR


async def test_an_idl_with_no_resolvable_program_id_is_refused(tmp_path, monkeypatch):
    # Nothing to resolve from (no Anchor.toml, no source): fail rather than fuzz a wrong address.
    _fake_build(monkeypatch, tmp_path, emits_idl=True, address=None)
    wheel = FakeWheel(build_program="example_lending", idl_dest="fuzz/vault/idls/x.json")
    with pytest.raises(ValueError, match="address"):
        await _prep(wheel, tmp_path)
