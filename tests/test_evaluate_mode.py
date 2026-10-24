"""The ``evaluate`` run mode's hook: resolution, loading, and the driver's hand-off.

``evaluate`` is implemented outside this package. These tests pin what this package owns: a run in
that mode stops at resolution when no implementation is installed; the implementation is built
only for a run in its mode; and the driver hands it the extracted batches without ever starting
``prepare_formalization``, then writes the report it returns.

Stubs throughout: no LLM, no DB, no prover, no real entry points.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

import composer.pipeline.core as core
from composer.pipeline import modes as modes_mod
from composer.pipeline import run_mode as run_mode_mod
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import EVM
from composer.pipeline.modes import (
    ModeContext, ModeResult, ModeSupport, ProjectBuild, RunModeImpl, RunModeLoader, load_run_mode,
)
from composer.prover.core import ProverOptions
from composer.pipeline.plugin_api import AnyEcosystem, ForEcosystem
from composer.pipeline.plugins import PLUGIN_ENTRY_POINT_GROUP
from composer.pipeline.run_mode import (
    MODE_ENTRY_POINT_GROUP, RUN_MODE_ENV, RunMode, resolve_run_mode, run_mode_name,
)
from composer.spec.solana.model import SolanaComponentInstance
from composer.spec.source.report.schema import AutoProverReport, CoverageReport
from composer.spec.system_model import ContractComponentInstance
from composer.spec.types import PropertyFormulation


# ---------------------------------------------------------------------------
# Fake entry points
# ---------------------------------------------------------------------------


@dataclass
class _EntryPoint:
    """The subset of ``importlib.metadata.EntryPoint`` the mode lookup touches."""

    name: str
    _target: Any
    module: str = "fake.module"
    attr: str = "Loader"

    def load(self) -> Any:
        return self._target


def _install(monkeypatch, *modes: _EntryPoint, plugins_too: bool = True) -> list[str]:
    """Make ``modes`` the only entry points under the mode group, and nothing under the plugin
    group. Returns the list of groups looked up, in order, so a test can assert a lookup never
    happened."""
    asked: list[str] = []

    def fake_entry_points(*, group: str) -> list[_EntryPoint]:
        asked.append(group)
        if group == MODE_ENTRY_POINT_GROUP:
            return list(modes)
        assert plugins_too and group == PLUGIN_ENTRY_POINT_GROUP, group
        return []

    # One function object serves both modules: they reach it through the shared
    # ``importlib.metadata`` module.
    monkeypatch.setattr(run_mode_mod.importlib.metadata, "entry_points", fake_entry_points)
    return asked


def _report(contract: str = "Vault") -> AutoProverReport:
    return AutoProverReport(
        contract_name=contract, properties=[], rules=[], groups=[],
        coverage=CoverageReport(
            total_properties=0, total_rules=0, total_groups=0,
            properties_per_group_min=0, properties_per_group_max=0,
            property_coverage_complete=True,
        ),
    )


class _Impl(RunModeImpl[Any, Any]):
    def __init__(self, succeeded: bool = True, failures: list[str] | None = None):
        self.seen: list[ModeContext[Any, Any, Any, Any]] = []
        self._succeeded = succeeded
        self._failures = failures or []

    async def run(self, ctx: ModeContext[Any, Any, Any, Any]) -> ModeResult:
        self.seen.append(ctx)
        return ModeResult(report=_report(), succeeded=self._succeeded, failures=self._failures)


def _loader_for(impl: _Impl, events: list[str], *, name: RunMode = RunMode.EVALUATE, scope: Any = None):
    """A loader class whose ``initialize`` yields ``impl`` and records its lifecycle in
    ``events``. Construction is recorded too, so a test can see the class was never built."""

    class Loader(RunModeLoader[Any, Any]):
        def __init__(self) -> None:
            events.append("constructed")

        @property
        def scope(self) -> Any:
            return scope if scope is not None else AnyEcosystem()

        @asynccontextmanager
        async def initialize(self):
            events.append("enter")
            try:
                yield impl
            finally:
                events.append("exit")

    Loader.name = name
    return Loader


# ---------------------------------------------------------------------------
# resolve_run_mode
# ---------------------------------------------------------------------------


def test_evaluate_without_an_installed_implementation_fails_at_resolution(monkeypatch):
    monkeypatch.delenv(RUN_MODE_ENV, raising=False)
    _install(monkeypatch)
    with pytest.raises(ValueError) as e:
        resolve_run_mode("evaluate")
    # The error names the mode and where an implementation registers, so a misconfigured image
    # is diagnosable from the message alone.
    assert "'evaluate'" in str(e.value)
    assert MODE_ENTRY_POINT_GROUP in str(e.value)


def test_evaluate_from_the_environment_is_checked_the_same_way(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setenv(RUN_MODE_ENV, "evaluate")
    with pytest.raises(ValueError, match=MODE_ENTRY_POINT_GROUP):
        resolve_run_mode(None)


def test_evaluate_with_an_installed_implementation_resolves(monkeypatch):
    _install(monkeypatch, _EntryPoint("evaluate", object()))
    assert resolve_run_mode("evaluate") is RunMode.EVALUATE
    assert resolve_run_mode(" Evaluate ") is RunMode.EVALUATE


def test_resolution_reads_metadata_only(monkeypatch):
    # The check runs before any service starts, so it must not import the implementation.
    class _Boom:
        name = "evaluate"
        module = "fake"
        attr = "Loader"

        def load(self):
            raise AssertionError("resolution must not load the entry point")

    monkeypatch.setattr(
        run_mode_mod.importlib.metadata, "entry_points", lambda *, group: [_Boom()]
    )
    assert resolve_run_mode("evaluate") is RunMode.EVALUATE


def test_built_in_modes_never_look_for_an_implementation(monkeypatch):
    monkeypatch.delenv(RUN_MODE_ENV, raising=False)
    asked = _install(monkeypatch)
    assert resolve_run_mode("comprehensive") is RunMode.COMPREHENSIVE
    assert resolve_run_mode("prioritized") is RunMode.PRIORITIZED
    assert resolve_run_mode(None) is RunMode.COMPREHENSIVE
    assert asked == []


def test_run_mode_name_narrows_evaluate():
    assert run_mode_name("evaluate") == "evaluate"
    assert run_mode_name("prioritized") == "prioritized"
    assert run_mode_name(None) == "comprehensive"
    assert run_mode_name("something-else") == "comprehensive"


# ---------------------------------------------------------------------------
# load_run_mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_enters_the_implementation_for_its_mode_only(monkeypatch):
    impl, events = _Impl(), []
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(impl, events)))

    async with load_run_mode(RunMode.COMPREHENSIVE, ContractComponentInstance) as got:
        assert got is None
    assert events == []

    async with load_run_mode(RunMode.EVALUATE, ContractComponentInstance) as got:
        assert got is impl
        assert events == ["constructed", "enter"]
    assert events == ["constructed", "enter", "exit"]


@pytest.mark.asyncio
async def test_load_rejects_a_loader_for_another_mode(monkeypatch):
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(_Impl(), [], name=RunMode.PRIORITIZED)))
    with pytest.raises(RuntimeError, match="implements 'prioritized'"):
        async with load_run_mode(RunMode.EVALUATE, ContractComponentInstance):
            pass


@pytest.mark.asyncio
async def test_load_rejects_something_that_is_not_a_loader(monkeypatch):
    _install(monkeypatch, _EntryPoint("evaluate", object))
    with pytest.raises(RuntimeError, match="is not a RunModeLoader"):
        async with load_run_mode(RunMode.EVALUATE, ContractComponentInstance):
            pass


@pytest.mark.asyncio
async def test_load_rejects_two_implementations(monkeypatch):
    loader = _loader_for(_Impl(), [])
    _install(monkeypatch, _EntryPoint("evaluate", loader), _EntryPoint("evaluate", loader))
    with pytest.raises(RuntimeError, match="Multiple implementations"):
        async with load_run_mode(RunMode.EVALUATE, ContractComponentInstance):
            pass


@pytest.mark.asyncio
async def test_load_rejects_an_implementation_for_another_ecosystem(monkeypatch):
    events: list[str] = []
    loader = _loader_for(_Impl(), events, scope=ForEcosystem(SolanaComponentInstance))
    _install(monkeypatch, _EntryPoint("evaluate", loader))
    with pytest.raises(RuntimeError, match="does not accept ContractComponentInstance"):
        async with load_run_mode(RunMode.EVALUATE, ContractComponentInstance):
            pass
    assert "enter" not in events


# ---------------------------------------------------------------------------
# The driver's hand-off
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.reports: list[AutoProverReport] = []

    def write_properties(self, *_a, **_kw): ...
    def write_artifact(self, *_a, **_kw): return "artifact"
    def write_report(self, report): self.reports.append(report)


class _Unit:
    """The minimum ``FeatureUnit`` the driver touches."""

    def __init__(self, name: str, index: int):
        self.display_name, self.unit_index, self.slug = name, index, name

    def cache_material(self) -> str: return self.display_name
    def context_tag(self) -> dict: return {}
    def feature_json(self) -> dict: return {}


_OPTS = ProverOptions(app="evm")


class _Prepared:
    main = "main-unit"

    def __init__(self, build: ProjectBuild | None = None) -> None:
        self.prepared = 0
        self.external = 0
        self._build = build if build is not None else ProjectBuild(
            succeeded=True, compilation_config={"solc": "solc8.28"}, solc_versions=["solc8.28"],
        )

    async def prepare_formalization(self, _run):
        self.prepared += 1
        raise AssertionError("an external mode must never start prepare_formalization")

    async def prepare_external_mode(self, _run):
        self.external += 1
        return ModeSupport(build=self._build, prover_options=_OPTS)


class _Backend:
    analysis_spec = core.SystemAnalysisSpec("analysis-key", "properties-key")
    core_phases = {"analysis": 1, "extraction": 2, "formalization": 3, "report": 4}
    backend_guidance = "guidance"

    def __init__(self, build: ProjectBuild | None = None) -> None:
        self.prepared = _Prepared(build)
        self.artifact_store = _Store()

    async def preflight(self, _run): return None

    async def prepare_system(self, _analyzed, _run, _preflight): return self.prepared

    def to_artifact_id(self, _c): return "artifact-id"


class _Ctx:
    def child(self, *_a, **_kw): return self
    async def cache_get(self, _ty): return None
    async def cache_put(self, _v): ...


class _Source:
    contract_name = "Vault"
    relative_path = "src/Vault.sol"
    content = None
    project_root = "/nonexistent/project"
    forbidden_read = None


class _Env:
    sort = "source"

    def llm_heavy(self): return None
    def llm_lite(self): return None


class _Run:
    source = _Source()
    env = _Env()
    ctx = _Ctx()

    def __init__(self, run_mode: RunMode) -> None:
        self.run_mode = run_mode

    async def runner(self, task_info, job):
        return await job()


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description="d")


def _patch_front_half(monkeypatch, units: dict[str, list[str]], *, fail: list[str] = ()) -> None:
    async def fake_analysis(*_a, **_kw): return "analyzed"

    async def fake_extract_all(*_a, **_kw):
        named = list(units.items())
        return core.ExtractionResult(
            [
                core.ExtractedBatch(_Unit(name, i), [_prop(t) for t in titles], _Ctx())  # type: ignore[arg-type]
                for i, (name, titles) in enumerate(named)
            ],
            [
                core.ExtractionFailure(_Unit(name, len(named) + i), RuntimeError("inference broke"))
                for i, name in enumerate(fail)
            ],
        )

    async def no_report(*_a, **_kw):
        raise AssertionError("an external mode's run must not build the formalization report")

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    monkeypatch.setattr(core, "build_report", no_report)


@pytest.mark.asyncio
async def test_evaluate_hands_the_batches_to_the_mode_and_writes_its_report(monkeypatch):
    impl, events = _Impl(), []
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(impl, events)))
    _patch_front_half(monkeypatch, {"deposits": ["a", "b"], "admin": ["c"]}, fail=["farms"])

    backend = _Backend()
    run = _Run(RunMode.EVALUATE)
    result = await run_pipeline(backend, run, max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]

    assert backend.prepared.prepared == 0
    assert backend.prepared.external == 1
    assert events == ["constructed", "enter", "exit"]

    [ctx] = impl.seen
    assert ctx.run is run
    assert ctx.main == "main-unit"
    assert [(b.feat.display_name, [p.title for p in b.props]) for b in ctx.batches] == [
        ("deposits", ["a", "b"]), ("admin", ["c"]),
    ]
    assert [f.unit.display_name for f in ctx.extraction_failures] == ["farms"]
    assert ctx.artifact_store is backend.artifact_store
    assert ctx.active_plugins == []
    # The backend's build and prover options reach the mode, and its work is tagged with the
    # formalization phase when the backend names no phase of its own for it.
    assert ctx.project_build is not None and ctx.project_build.solc_versions == ["solc8.28"]
    assert ctx.prover_options is _OPTS
    assert ctx.phase == 3

    # The driver stamps the run's mode on the report the mode returned, then writes it.
    [written] = backend.artifact_store.reports
    assert written.run_mode == "evaluate"
    assert written.contract_name == "Vault"

    assert result.run_mode is RunMode.EVALUATE
    assert result.n_components == 3 and result.n_properties == 3
    assert result.outcomes == []
    assert result.failures == ["farms: inference broke"]
    assert result.mode_succeeded is True and not result.all_failed


@pytest.mark.asyncio
async def test_the_modes_verdict_decides_the_exit_status(monkeypatch):
    impl = _Impl(succeeded=False, failures=["2 confs did not compile"])
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(impl, [])))
    _patch_front_half(monkeypatch, {"deposits": ["a"]})

    result = await run_pipeline(_Backend(), _Run(RunMode.EVALUATE), max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]
    assert result.all_failed
    assert result.failures == ["2 confs did not compile"]


@pytest.mark.asyncio
async def test_the_mode_runs_even_when_nothing_was_extracted(monkeypatch):
    # The project's specs exist whatever inference found, so the mode decides what an empty
    # property list means; the driver does not raise "no properties extracted" for it.
    impl = _Impl()
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(impl, [])))
    _patch_front_half(monkeypatch, {})

    await run_pipeline(_Backend(), _Run(RunMode.EVALUATE), max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]
    assert len(impl.seen) == 1 and list(impl.seen[0].batches) == []


@pytest.mark.asyncio
async def test_a_comprehensive_run_never_builds_an_installed_mode(monkeypatch):
    events: list[str] = []
    asked = _install(monkeypatch, _EntryPoint("evaluate", _loader_for(_Impl(), events)))

    async with load_run_mode(RunMode.COMPREHENSIVE, ContractComponentInstance):
        pass
    async with load_run_mode(RunMode.PRIORITIZED, ContractComponentInstance):
        pass
    assert events == []
    assert MODE_ENTRY_POINT_GROUP not in asked


@pytest.mark.asyncio
async def test_the_driver_refuses_an_external_mode_without_its_implementation():
    with pytest.raises(ValueError, match="needs a mode implementation"):
        await core.run_pipeline_inner(
            _Backend(), _Run(RunMode.EVALUATE), None, ecosystem=EVM,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_a_failed_build_is_reported_and_the_mode_still_runs(monkeypatch):
    impl = _Impl(failures=["3 confs did not compile"])
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(impl, [])))
    _patch_front_half(monkeypatch, {"deposits": ["a"]})

    build = ProjectBuild(succeeded=False, error="solc8.30 is not installed")
    result = await run_pipeline(_Backend(build), _Run(RunMode.EVALUATE), max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]

    [ctx] = impl.seen
    assert ctx.project_build is build
    assert result.failures == ["project build: solc8.30 is not installed", "3 confs did not compile"]
    # The mode's verdict, not the build, decides the exit status.
    assert not result.all_failed


@pytest.mark.asyncio
async def test_the_backends_external_mode_phase_tags_the_modes_work(monkeypatch):
    impl = _Impl()
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(impl, [])))
    _patch_front_half(monkeypatch, {"deposits": ["a"]})

    backend = _Backend()
    backend.core_phases = {**_Backend.core_phases, "external_mode": 7}  # type: ignore[assignment]
    await run_pipeline(backend, _Run(RunMode.EVALUATE), max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]
    assert impl.seen[0].phase == 7


@pytest.mark.asyncio
async def test_a_backend_with_nothing_to_build_hands_over_nothing():
    class _Plain(core.PreparedSystem[Any, Any, Any]):
        async def prepare_formalization(self, run):  # pragma: no cover - never called
            raise AssertionError

    support = await _Plain(main="m").prepare_external_mode(_Run(RunMode.EVALUATE))  # type: ignore[arg-type]
    assert support == ModeSupport()


def test_the_failure_headline_fits_the_mode():
    evaluate = core.CorePipelineResult(1, 1, [], [], RunMode.EVALUATE, mode_succeeded=False)
    assert evaluate.all_failed
    assert evaluate.failure_headline == "RUN FAILED: the evaluation produced no usable result."
    assert "every component" not in evaluate.failure_headline

    formalizing = core.CorePipelineResult(1, 1, [], [], RunMode.COMPREHENSIVE)
    assert formalizing.failure_headline == "RUN FAILED: every component failed to generate or gave up."
