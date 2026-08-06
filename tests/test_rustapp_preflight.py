"""Tests for the backend preflight: prepare the workspace, then *gate* it — before any property
exists, concurrently with system analysis (``composer.rustapp.adapter``).

The gate is what makes the prep mean something. Placing a manifest and running ``cargo fetch``
resolves a dependency graph but compiles nothing, and warming is deliberately best-effort — so
until this existed, the first thing that actually *built* the workspace was the compile of the first
LLM-authored draft, at the far end of the extraction phase. A dependency graph that won't resolve, a
harness that won't link, or codegen the generator rejects would surface there as compiler errors
an authoring agent cannot fix (it does not own the project's build files) and would consume every one
of its revise attempts.

So: the wheel renders its own skeleton, the host builds it through the same ``compile`` callout the
authored artifacts use, and a failure is terminal. Fake wheel, fake project toolchain — no toolchain,
no LLM.
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from composer.rustapp.adapter import PreflightFailed, ProjectFacts
from composer.rustapp.descriptor import AppDescriptor
from tests.conftest import (
    wire_descriptor, wire_phase, wire_required_phases, wire_workspace_prep,
)
from composer.rustapp.host import build_backend
from composer.rustapp.toolchain import PROJECT_TOOLCHAINS
from composer.spec.context import SourceCode
from composer.spec.system_model import SolidityIdentifier

pytestmark = pytest.mark.asyncio

CRATE_DIR = "programs/lend"
#: What the chain's registered toolchain reports for this project. Framework-side this is an opaque
#: object — that these keys spell a Cargo crate is the chain implementation's business.
SOURCE_UNIT = {"dir": CRATE_DIR, "package": "example-lending", "lib": "example_lending"}
IDL_DEST = "fuzz/vault/idls/example_lending.json"
#: A prep request in the chain's own shape, and the facts carrying it out establishes.
BUILD = {"build_program": "example_lending"}
BUILD_WITH_IDL = {**BUILD, "idl_dest": IDL_DEST}
ADDR = "LendvUkXRmuDKxGCCFJra9uxWMdMooPEmJk3qp7Tg1Z"


class FakeWheel:
    """A wheel with a fixed ``workspace_prep`` plan and a scripted ``compile``."""

    def __init__(self, request: dict, *, compile_errors: str | None = None):
        self._plan = wire_workspace_prep(
            toolchain_request={"warm_dirs": ["fuzz/vault"], **request}
        )
        self._compile_errors = compile_errors
        #: Every ``compile`` call, as ``(input, spec)`` — the assertion surface for these tests.
        self.compiles: list[tuple[dict, str]] = []

    def workspace_prep(self, _input_json: str) -> str:
        return json.dumps(self._plan)

    def compile(self, input_json: str, spec: str, _workdir: str, _sandbox_json: str) -> str:
        self.compiles.append((json.loads(input_json), spec))
        if self._compile_errors is None:
            return json.dumps({"status": "ok"})
        return json.dumps({"status": "failed", "errors": self._compile_errors})


def _descriptor(*, with_preflight: bool = True) -> AppDescriptor:
    # Without the role claimed the phase only groups, which is how an application says it has no
    # gate — the phase itself stays, so the two descriptors differ in exactly one thing.
    gate = wire_phase("preflight", "Build Preflight", 4, "preflight" if with_preflight else "grouping")
    return AppDescriptor.model_validate(
        wire_descriptor(ecosystem="solana", phases=[*wire_required_phases(), gate])
    )


class _Run:
    """Just enough ``PipelineRun``: both runners await the job inline, and record which was used."""

    def __init__(self, source: SourceCode):
        self.source = source
        self.env = None
        self.ctx = None
        self.agent_tasks: list[str] = []
        self.cpu_tasks: list[str] = []
        #: Every ``TaskInfo`` the backend built, in order — the phase member matters as much as the
        #: id (see the phase-tagging test).
        self.tasks: list[Any] = []

    async def runner(self, task_info, job):
        self.agent_tasks.append(task_info.task_id)
        self.tasks.append(task_info)
        return await job()

    async def cpu_runner(self, task_info, job):
        self.cpu_tasks.append(task_info.task_id)
        self.tasks.append(task_info)
        return await job()


def _source(root: Path) -> SourceCode:
    return SourceCode(
        content=None,  # type: ignore[arg-type]  — unused by preflight
        project_root=str(root),
        contract_name=SolidityIdentifier("vault"),
        relative_path=f"{CRATE_DIR}/src/lib.rs",
        forbidden_read="",
    )


def _project(root: Path) -> None:
    """Just the source file the analysis identifier points at."""
    (root / CRATE_DIR / "src").mkdir(parents=True)
    (root / CRATE_DIR / "src" / "lib.rs").write_text("// program")


class _FakeToolchain:
    """A stand-in for the chain implementation the real Solana one is registered as
    (``composer.rustapp.toolchain``). It does what any implementation must: read the request in its
    own shape, do the work, and report what it established."""

    def source_unit(self, _source):
        return SOURCE_UNIT

    async def prepare(self, plan, _input, *, source, sandbox, timeout_s):
        request = plan.toolchain_request
        assert request["build_program"]  # what these plans all ask for
        if (idl_dest := request.get("idl_dest")) is None:
            return {}
        dest = Path(source.project_root) / idl_dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"metadata": {"address": ADDR}}))
        return {"idl": idl_dest}


def _fake_chain(monkeypatch):
    monkeypatch.setitem(PROJECT_TOOLCHAINS, "solana", _FakeToolchain())


async def _preflight(monkeypatch, tmp_path, wheel, *, with_preflight=True, declared=None):
    _project(tmp_path)
    _fake_chain(monkeypatch)
    source = _source(tmp_path)
    backend = build_backend(wheel, _descriptor(with_preflight=with_preflight), source)
    backend.declared_args = declared or {}
    run = _Run(source)
    return await backend.preflight(cast(Any, run)), run


async def test_the_gate_compiles_a_wheel_authored_skeleton_with_no_spec(tmp_path, monkeypatch):
    wheel = FakeWheel(BUILD)
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    assert isinstance(result, ProjectFacts)
    assert len(wheel.compiles) == 1
    gate_input, spec = wheel.compiles[0]
    # `kind` is what the wheel dispatches on, and the spec is empty: nothing has been authored yet,
    # so the wheel renders the skeleton itself.
    assert gate_input["kind"] == "preflight"
    assert spec == ""
    assert gate_input["props"] == []


async def test_the_resolved_source_unit_is_carried_forward(tmp_path, monkeypatch):
    wheel = FakeWheel(BUILD)
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    # Whatever the chain's toolchain reported — none of it follows from the analysis identifier
    # ("vault"), and the gated build, every authoring turn and the deliverable must all agree on it.
    assert result.source_unit == SOURCE_UNIT
    assert wheel.compiles[0][0]["source_unit"] == SOURCE_UNIT


async def test_what_the_prep_established_reaches_the_gate_that_builds_against_it(
    tmp_path, monkeypatch
):
    wheel = FakeWheel(BUILD_WITH_IDL)
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    assert result.prep_facts == {"idl": IDL_DEST}
    # The gate renders the same workspace the prep just set up, so it must see what the prep
    # established — here, that the harness can generate its types from a placed file rather than
    # linking the program's crate.
    assert wheel.compiles[0][0]["prep_facts"] == {"idl": IDL_DEST}


async def test_a_prep_that_established_nothing_says_so_with_an_empty_answer(tmp_path, monkeypatch):
    wheel = FakeWheel(BUILD)
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    # Empty is the whole spelling of "nothing established" — a fact present but empty would read as
    # something being in place at an empty path.
    assert result.prep_facts == {}
    assert wheel.compiles[0][0]["prep_facts"] == {}


async def test_declared_args_are_in_scope_for_the_gate(tmp_path, monkeypatch):
    # Prep may need one (Crucible reads `program_idl` when deciding how to source the types), so they
    # are on the input from the very first callout.
    wheel = FakeWheel(BUILD)
    _result, _run = await _preflight(
        monkeypatch, tmp_path, wheel, declared={"fuzz_timeout": 30}
    )
    assert wheel.compiles[0][0]["args"]["fuzz_timeout"] == 30


async def test_a_failing_gate_raises_with_the_diagnostics_and_does_not_retry(tmp_path, monkeypatch):
    errors = "error[E0432]: unresolved import `example_lending::instruction`"
    wheel = FakeWheel(BUILD, compile_errors=errors)

    with pytest.raises(PreflightFailed) as excinfo:
        await _preflight(monkeypatch, tmp_path, wheel)

    assert errors in str(excinfo.value)
    # One attempt. There is nothing to re-author: the failure is in the build files/toolchain, and the
    # message says so rather than letting a later authoring loop discover it the expensive way.
    assert len(wheel.compiles) == 1


async def test_without_a_declared_preflight_the_prep_runs_but_nothing_is_gated(tmp_path, monkeypatch):
    # The gate is opt-in per wheel; the workspace prep is not.
    wheel = FakeWheel(BUILD_WITH_IDL)
    result, run = await _preflight(monkeypatch, tmp_path, wheel, with_preflight=False)

    assert result.prep_facts == {"idl": IDL_DEST}  # the prep still ran
    assert wheel.compiles == []
    assert run.agent_tasks == [] and run.cpu_tasks == []  # the prep is silent, so there is no task


async def test_the_build_spends_a_cpu_slot_not_an_agent_slot(tmp_path, monkeypatch):
    # The agent semaphore budgets concurrent *agents*; a multi-minute cargo build charged to it would
    # silently take a quarter of the default concurrency away from the analysis it overlaps. It is
    # throttled all the same — against the CPU budget, which is what it actually spends.
    wheel = FakeWheel(BUILD)
    _result, run = await _preflight(monkeypatch, tmp_path, wheel)

    assert run.cpu_tasks == ["demoprover-preflight"]
    assert run.agent_tasks == []


async def test_the_gate_is_tagged_with_the_declared_phase_member(tmp_path, monkeypatch):
    # The task id comes from the step's kind, and the phase from its declared `phase_key` resolved
    # against the backend's own synthesized enum. That identity is load-bearing: the frontend looks
    # up section labels by enum *member*, so a member from any other copy of the enum would land the
    # task in no section at all. `RustBackend.task_info` is the only thing that resolves it.
    wheel = FakeWheel(BUILD)
    _project(tmp_path)
    _fake_chain(monkeypatch)
    source = _source(tmp_path)
    backend = build_backend(wheel, _descriptor(), source)
    run = _Run(source)
    await backend.preflight(cast(Any, run))

    info = run.tasks[0]
    assert info.task_id == "demoprover-preflight"
    assert info.label == "Build Preflight"
    assert info.phase is backend.phase["preflight"]
    # …and it is the same enum the frontend's labels are keyed by, not a fresh synthesis of it.
    assert type(info.phase) is backend.phase
