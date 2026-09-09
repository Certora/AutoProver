"""``StagedFormalizer.begin``: called once, with every unit's properties, before any unit is
formalized — and what it *returns* is the formalizer the driver then fans out over.

The staged type exists for backends with a *shared* artifact all units build on (Crucible's fixture;
a future CVLR backend's setup module). Such an artifact must be authored from the union of every
unit's properties and cannot be authored in ``prepare_formalization`` (which overlaps extraction, so
no properties exist yet). Doing it lazily inside ``formalize`` instead means the first unit to
arrive decides the shared artifact for all of them — harmless at one unit, silently wrong at several.
These tests pin both halves: that the driver drives the staged type in that order, and that a backend
returning a plain ``Formalizer`` is never staged at all.

The barrier has a price when a unit's extraction is parked on a person (its refinement
conversation raised ``GraphSuspended``): ``begin`` cannot run, so the units whose properties are
ready are reported *stalled* behind the parked one, and the execution ends. Without the barrier,
the ready units are formalized while the parked one waits alone. Both are pinned here too.

Stubs throughout — no LLM, no DB, no backend wheel.
"""

import asyncio

import pytest
from langgraph.types import Interrupt

import composer.pipeline.core as core
from composer.io.protocol import GraphSuspended
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import EVM
from composer.pipeline.ptypes import AwaitingInput, CorePipelineResult, Delivered, Stalled
from composer.spec.types import PropertyFormulation

pytestmark = pytest.mark.asyncio

#: A unit's extraction in these tests: its property titles, None when it is parked on a
#: person, or the exception it dies with.
type Extracted = list[str] | None | Exception


class _Store:
    def write_properties(self, *_a, **_kw): ...
    def write_artifact(self, *_a, **_kw): return "artifact"
    def write_report(self, *_a, **_kw): ...


class _Unit:
    """The minimum ``FeatureUnit`` the driver touches."""

    def __init__(self, name: str, index: int):
        self.display_name, self.unit_index, self.slug = name, index, name

    def cache_material(self) -> str: return self.display_name
    def context_tag(self) -> dict: return {}
    def feature_json(self) -> dict: return {}


class _Result:
    """A ``BackendResult``: only what the driver reads off it."""
    artifact_text = ""
    unit_file = None
    run_link = None
    def property_checks(self): return []


class _Formalizer:
    """Records the order of ``begin`` / ``formalize`` calls. ``calls`` is shared with the staged half
    that built it, so one list holds the whole sequence."""

    formalized_type = _Result
    backend_tag = "foundry"

    def __init__(self, calls: list[tuple[str, list[str]]] | None = None):
        self.calls = [] if calls is None else calls

    async def formalize(self, _label, feat, props, _ctx, _run, _extra_tools):
        # A yield point, so a driver that started the fan-out before `begin` finished would
        # interleave here and be caught by the ordering assertion.
        await asyncio.sleep(0)
        self.calls.append((f"formalize:{feat.display_name}", [p.title for p in props]))
        return _Result()

    def extra_report_inputs(self): return []
    def findings_evidence(self): return None
    async def fetch_verdicts(self, _inp): return {}
    async def finalize(self, _outcomes, _run): return None


class _Staged(core.StagedFormalizer):
    """The staged half. ``begin`` is the only way to obtain the formalizer, so an artifact authored
    from every unit's properties cannot be skipped or raced by the fan-out."""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    async def begin(self, jobs, _run):
        self.calls.append(("begin", [p.title for j in jobs for p in j.props]))
        return _Formalizer(self.calls)


class _Prepared:
    main = "main-unit"

    def __init__(self, formalizer): self._f = formalizer

    async def prepare_formalization(self, _run): return self._f


class _Backend:
    analysis_spec = core.SystemAnalysisSpec("analysis-key", "properties-key")
    core_phases = {"analysis": 1, "extraction": 2, "formalization": 3, "report": 4}
    backend_guidance = "guidance"
    artifact_store = _Store()

    def __init__(self, prepared): self._prepared = prepared

    async def preflight(self, _run): return None

    async def prepare_system(self, _analyzed, _run, _preflight): return self._prepared

    def to_artifact_id(self, _c): return "artifact-id"


class _Cache:
    async def cache_get(self, _ty): return None
    async def cache_put(self, _v): return None

    def child(self, key, tags = None):
        if tags is None:
            return _Cache()
        async def thunk():
            return _Cache()
        return thunk()



class _Ctx:
    recursion_limit = 10

    def child(self, *_a, **_kw): return self


class _FeatCtx:
    def child(self, key, tags = None):
        if tags is None:
            return _Cache()
        async def thunk():
            return _Cache()
        return thunk()


class _Source:
    contract_name = "Vault"
    relative_path = "programs/vault/src/lib.rs"


class _Run:
    source = _Source()
    env = None
    ctx = _Ctx()

    async def runner(self, task_info, job):
        return await job()


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description="d")


def _parked() -> GraphSuspended:
    return GraphSuspended([Interrupt(value={"q": "keep this property?"}, id="i1")])


async def _drive(
    monkeypatch, units: dict[str, Extracted], formalizer: _Staged | _Formalizer
) -> CorePipelineResult:
    async def fake_analysis(*_a, **_kw): return "analyzed"

    async def extraction(unit: _Unit, extracted: Extracted) -> core._Batch | None:
        await asyncio.sleep(0)  # a yield point, like the real thing
        if extracted is None:
            raise _parked()
        if isinstance(extracted, Exception):
            raise extracted
        return core._Batch(unit, [_prop(t) for t in extracted], _FeatCtx())  # type: ignore[arg-type]

    async def fake_extract_all(*_a, **_kw):
        started = []
        for i, (name, extracted) in enumerate(units.items()):
            unit = _Unit(name, i)
            started.append(core._Extraction(unit, asyncio.create_task(extraction(unit, extracted))))
        return started

    async def fake_report(*_a, **_kw): return object()

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    monkeypatch.setattr(core, "build_report", fake_report)
    return await run_pipeline(_Backend(_Prepared(formalizer)), _Run(), max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]


def _by_name(result: CorePipelineResult) -> dict[str, object]:
    return {o.feat.display_name: o.result for o in result.outcomes}


async def test_begin_runs_once_before_any_unit_is_formalized(monkeypatch):
    s = _Staged()
    await _drive(monkeypatch, {"deposits": ["a"], "admin": ["b"], "farms": ["c"]}, s)
    names = [c[0] for c in s.calls]
    assert names[0] == "begin", f"begin must precede every formalize; got {names}"
    assert names.count("begin") == 1
    assert sorted(names[1:]) == ["formalize:admin", "formalize:deposits", "formalize:farms"]


async def test_begin_sees_every_unit_s_properties(monkeypatch):
    # The point of the staged type: the shared artifact is designed around all of them, not around
    # whichever unit won the race to formalize first.
    s = _Staged()
    await _drive(monkeypatch, {"deposits": ["a", "b"], "admin": ["c"], "farms": ["d"]}, s)
    assert s.calls[0] == ("begin", ["a", "b", "c", "d"])


async def test_begin_still_runs_for_a_single_unit(monkeypatch):
    # The K=1 case must go down the same path, or the shared artifact would be authored lazily
    # again the moment a second unit appears.
    s = _Staged()
    await _drive(monkeypatch, {"whole-program": ["a", "b"]}, s)
    assert [c[0] for c in s.calls] == ["begin", "formalize:whole-program"]


async def test_an_unstaged_formalizer_is_formalized_directly(monkeypatch):
    # The other arm of the union: a backend with no shared artifact returns the formalizer itself,
    # and the driver must fan out over exactly that object rather than looking for a staging step.
    f = _Formalizer()
    await _drive(monkeypatch, {"deposits": ["a"], "admin": ["b"]}, f)
    assert sorted(c[0] for c in f.calls) == ["formalize:admin", "formalize:deposits"]


async def test_a_parked_unit_stalls_its_siblings_behind_the_shared_artifact(monkeypatch):
    # The artifact needs every unit's properties, so nothing can start while admin's conversation
    # waits on a person: begin is never called, and the ready units are stalled, not awaiting input.
    s = _Staged()
    result = await _drive(monkeypatch, {"deposits": ["a"], "admin": None, "farms": ["c"]}, s)
    assert s.calls == [], "no artifact is authored from a partial union"
    by_name = _by_name(result)
    assert isinstance(by_name["admin"], AwaitingInput) and by_name["admin"].n_questions == 1
    assert by_name["deposits"] == Stalled(behind=("admin",), n_questions=1)
    assert by_name["farms"] == Stalled(behind=("admin",), n_questions=1)
    assert result.unfinished and not result.all_failed
    assert result.awaiting_input == ["admin: awaiting input on 1 question(s)"]
    assert result.stalled == [
        "deposits: properties ready, stalled behind admin (1 open question(s))",
        "farms: properties ready, stalled behind admin (1 open question(s))",
    ]
    assert result.n_properties == 2, "the stalled units' properties are ready and counted"


async def test_without_a_shared_artifact_the_ready_units_proceed_while_a_sibling_waits(monkeypatch):
    # No barrier: deposits flows from extraction into formalization; admin parks alone.
    f = _Formalizer()
    result = await _drive(monkeypatch, {"deposits": ["a"], "admin": None}, f)
    assert [c[0] for c in f.calls] == ["formalize:deposits"]
    by_name = _by_name(result)
    assert isinstance(by_name["deposits"], Delivered)
    assert isinstance(by_name["admin"], AwaitingInput)
    assert result.unfinished and result.stalled == []
    assert result.awaiting_input == ["admin: awaiting input on 1 question(s)"]


async def test_a_unit_with_no_properties_is_dropped_on_both_paths(monkeypatch):
    for formalizer in (_Staged(), _Formalizer()):
        result = await _drive(monkeypatch, {"deposits": ["a"], "empty": []}, formalizer)
        assert list(_by_name(result)) == ["deposits"]


async def test_a_failed_extraction_is_the_runs_failure_on_both_paths(monkeypatch):
    # A crash in one unit's extraction is not a lane outcome the way a formalization crash is: the
    # run fails with it, once every sibling has settled.
    boom = RuntimeError("extraction blew up")
    for formalizer in (_Staged(), _Formalizer()):
        with pytest.raises(RuntimeError) as caught:
            await _drive(monkeypatch, {"deposits": ["a"], "admin": boom}, formalizer)
        assert caught.value is boom
