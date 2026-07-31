"""Tests for the backend preflight: prepare the workspace, then *gate* it — before any property
exists, concurrently with system analysis (``composer.rustapp.adapter``).

The gate is what makes the prep mean something. Placing a manifest and running ``cargo fetch``
resolves a dependency graph but compiles nothing, and the fetch is deliberately best-effort — so
until this existed, the first thing that actually *built* the workspace was the compile of the first
LLM-authored draft, at the far end of the extraction phase. A dependency graph that won't resolve, a
harness that won't link, or IDL codegen the generator rejects would surface there as compiler errors
an authoring agent cannot fix (it does not own the manifest) and would consume every one of its
revise attempts.

So: the wheel renders its own skeleton, the host builds it through the same ``compile`` callout the
authored artifacts use, and a failure is terminal. Fake wheel, fake build capability — no toolchain,
no LLM.
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest

import composer.spec.solana.build as buildmod
from composer.rustapp.adapter import PreflightFailed, RustPreflight
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.host import build_backend
from composer.spec.context import SourceCode
from composer.spec.solana.build import BuiltProgram
from composer.spec.system_model import SolidityIdentifier

pytestmark = pytest.mark.asyncio

CRATE_DIR = "programs/lend"
IDL_DEST = "fuzz/vault/idls/example_lending.json"
ADDR = "LendvUkXRmuDKxGCCFJra9uxWMdMooPEmJk3qp7Tg1Z"


class FakeWheel:
    """A wheel with a fixed ``workspace_prep`` plan and a scripted ``compile``."""

    def __init__(self, plan: dict, *, compile_errors: str | None = None):
        self._plan = {"files": {}, "warm_dirs": ["fuzz/vault"], **plan}
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
    body: dict = {
        "name": "crucible",
        "header_text": "h",
        "ecosystem": "solana",
        "backend_tag": "crucible",
        "backend_guidance": "g",
        "analysis_key": "k",
        "phases": [
            {"key": "preflight", "label": "Build Preflight", "order": 0},
            {"key": "analysis", "label": "A", "order": 1, "core_slot": "analysis"},
            {"key": "extraction", "label": "E", "order": 2, "core_slot": "extraction"},
            {"key": "formalization", "label": "F", "order": 3, "core_slot": "formalization"},
            {"key": "report", "label": "R", "order": 4, "core_slot": "report"},
        ],
        "artifact_layout": {
            "deliverable_dir": "d", "internal_dir": "i", "report_dir": "r",
            "artifact_dir": "a", "artifact_prefix": "p", "artifact_extension": "rs",
            "property_suffix": "s",
        },
    }
    if with_preflight:
        body["preflight"] = {"phase_key": "preflight", "label": "Build Preflight"}
    return AppDescriptor.model_validate(body)


class _Run:
    """Just enough ``PipelineRun``: both runners await the job inline, and record which was used."""

    def __init__(self, source: SourceCode):
        self.source = source
        self.env = None
        self.ctx = None
        self.metered: list[str] = []
        self.unmetered: list[str] = []

    async def runner(self, task_info, job):
        self.metered.append(task_info.task_id)
        return await job()

    async def unmetered_runner(self, task_info, job):
        self.unmetered.append(task_info.task_id)
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
    """The minimum Cargo layout the crate resolver reads."""
    (root / CRATE_DIR / "src").mkdir(parents=True)
    (root / CRATE_DIR / "src" / "lib.rs").write_text("// program")
    (root / CRATE_DIR / "Cargo.toml").write_text(
        '[package]\nname = "example-lending"\n\n[lib]\nname = "example_lending"\n\n'
        '[dependencies]\nanchor-lang = "1.0.1"\n'
    )
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["programs/lend"]\n')


def _fake_build(monkeypatch, root: Path, *, emits_idl: bool = True):
    async def fake_build_program(project_root, program, *, with_idl=False, **_kw):
        idl = None
        if with_idl and emits_idl:
            idl = Path(project_root) / "target" / "idl" / f"{program}.json"
            idl.parent.mkdir(parents=True, exist_ok=True)
            idl.write_text(json.dumps({"metadata": {"address": ADDR}}))
        return BuiltProgram(program=program, so_path=root / "x.so", idl_path=idl)

    monkeypatch.setattr(buildmod, "build_program", fake_build_program)


async def _preflight(monkeypatch, tmp_path, wheel, *, with_preflight=True, declared=None):
    _project(tmp_path)
    _fake_build(monkeypatch, tmp_path)
    source = _source(tmp_path)
    backend = build_backend(wheel, _descriptor(with_preflight=with_preflight), source)
    backend.declared_args = declared or {}
    run = _Run(source)
    return await backend.preflight(cast(Any, run)), run


async def test_the_gate_compiles_a_wheel_authored_skeleton_with_no_spec(tmp_path, monkeypatch):
    wheel = FakeWheel({"build_program": "example_lending"})
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    assert isinstance(result, RustPreflight)
    assert len(wheel.compiles) == 1
    gate_input, spec = wheel.compiles[0]
    # `kind` is what the wheel dispatches on, and the spec is empty: nothing has been authored yet,
    # so the wheel renders the skeleton itself.
    assert gate_input["kind"] == "preflight"
    assert spec == ""
    assert gate_input["props"] == []


async def test_the_resolved_program_crate_is_carried_forward(tmp_path, monkeypatch):
    wheel = FakeWheel({"build_program": "example_lending"})
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    # Resolved from the source file's manifest — none of it follows from the analysis identifier
    # ("vault"), and the gated build, every authoring turn and the deliverable must all agree on it.
    assert result.program_crate["dir"] == CRATE_DIR
    assert result.program_crate["package"] == "example-lending"
    assert result.program_crate["lib"] == "example_lending"
    assert wheel.compiles[0][0]["program_crate"] == result.program_crate


async def test_a_placed_idl_reaches_the_gate_that_builds_against_it(tmp_path, monkeypatch):
    wheel = FakeWheel({"build_program": "example_lending", "idl_dest": IDL_DEST})
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    assert result.idl == IDL_DEST
    # The gate renders the same crate the prep just set up, so it must see where the IDL landed —
    # under the IDL path the harness generates its types from that file rather than linking the crate.
    assert wheel.compiles[0][0]["context"]["idl"] == IDL_DEST


async def test_no_idl_means_no_idl_key_rather_than_an_empty_one(tmp_path, monkeypatch):
    wheel = FakeWheel({"build_program": "example_lending"})
    result, _run = await _preflight(monkeypatch, tmp_path, wheel)

    # "The key is set" is the signal the wheel reads to mean "the file is in place"; an empty string
    # would read as the crate path either way, but only by accident.
    assert result.idl is None
    assert "idl" not in wheel.compiles[0][0]["context"]


async def test_declared_args_are_in_scope_for_the_gate(tmp_path, monkeypatch):
    # Prep may need one (Crucible reads `program_idl` when deciding how to source the types), so they
    # are in the context from the very first callout.
    wheel = FakeWheel({"build_program": "example_lending"})
    _result, _run = await _preflight(
        monkeypatch, tmp_path, wheel, declared={"fuzz_timeout": 30}
    )
    assert wheel.compiles[0][0]["context"]["fuzz_timeout"] == 30


async def test_a_failing_gate_raises_with_the_diagnostics_and_does_not_retry(tmp_path, monkeypatch):
    errors = "error[E0432]: unresolved import `example_lending::instruction`"
    wheel = FakeWheel({"build_program": "example_lending"}, compile_errors=errors)

    with pytest.raises(PreflightFailed) as excinfo:
        await _preflight(monkeypatch, tmp_path, wheel)

    assert errors in str(excinfo.value)
    # One attempt. There is nothing to re-author: the failure is in the manifest/toolchain, and the
    # message says so rather than letting a later authoring loop discover it the expensive way.
    assert len(wheel.compiles) == 1


async def test_without_a_declared_preflight_the_prep_runs_but_nothing_is_gated(tmp_path, monkeypatch):
    # The gate is opt-in per wheel; the workspace prep is not. A wheel that declares no preflight
    # keeps exactly the old behaviour.
    wheel = FakeWheel({"build_program": "example_lending", "idl_dest": IDL_DEST})
    result, run = await _preflight(monkeypatch, tmp_path, wheel, with_preflight=False)

    assert result.idl == IDL_DEST  # the prep still ran
    assert wheel.compiles == []
    assert run.metered == [] and run.unmetered == []  # no task, as before


async def test_the_build_does_not_spend_an_agent_slot(tmp_path, monkeypatch):
    # The run's semaphore budgets concurrent *agents*; a multi-minute cargo build charged to it would
    # silently take a quarter of the default concurrency away from the analysis it overlaps.
    wheel = FakeWheel({"build_program": "example_lending"})
    _result, run = await _preflight(monkeypatch, tmp_path, wheel)

    assert run.unmetered == ["crucible-preflight"]
    assert run.metered == []
