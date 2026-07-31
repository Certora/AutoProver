"""``StagedFormalizer.begin``: called once, with every unit's properties, before any unit is
formalized — and what it *returns* is the formalizer the driver then fans out over.

The staged type exists for backends with a *shared* artifact all units build on (Crucible's fixture;
a future CVLR backend's setup module). Such an artifact must be authored from the union of every
unit's properties and cannot be authored in ``prepare_formalization`` (which overlaps extraction, so
no properties exist yet). Doing it lazily inside ``formalize`` instead means the first unit to
arrive decides the shared artifact for all of them — harmless at one unit, silently wrong at several.
These tests pin both halves: that the driver drives the staged type in that order, and that a backend
returning a plain ``Formalizer`` is never staged at all.

Stubs throughout — no LLM, no DB, no backend wheel.
"""

import asyncio

import pytest

import composer.pipeline.core as core
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import EVM
from composer.spec.types import PropertyFormulation

pytestmark = pytest.mark.asyncio


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
    def property_units(self): return []


class _Formalizer:
    """Records the order of ``begin`` / ``formalize`` calls. ``calls`` is shared with the staged half
    that built it, so one list holds the whole sequence."""

    formalized_type = _Result
    backend_tag = "foundry"

    def __init__(self, calls: list[tuple[str, list[str]]] | None = None):
        self.calls = [] if calls is None else calls

    async def formalize(self, _label, feat, props, _ctx, _run):
        # A yield point, so a driver that started the fan-out before `begin` finished would
        # interleave here and be caught by the ordering assertion.
        await asyncio.sleep(0)
        self.calls.append((f"formalize:{feat.display_name}", [p.title for p in props]))
        return _Result()

    def extra_report_inputs(self): return []
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

    async def prepare_system(self, _analyzed, _run): return self._prepared

    def to_artifact_id(self, _c): return "artifact-id"


class _Cache:
    async def cache_get(self, _ty): return None
    async def cache_put(self, _v): return None


class _Ctx:
    recursion_limit = 10

    def child(self, *_a, **_kw): return self

    async def achild(self, *_a, **_kw): return _Cache()


class _FeatCtx:
    async def child(self, *_a, **_kw): return _Cache()


class _Source:
    contract_name = "Vault"
    relative_path = "programs/vault/src/lib.rs"


class _Run:
    source = _Source()
    env = None
    ctx = _Ctx()

    async def runner(self, _task_info, job):
        return await job()


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description="d")


async def _drive[F: (_Staged, _Formalizer)](
    monkeypatch, units: dict[str, list[str]], formalizer: F
) -> F:
    async def fake_analysis(*_a, **_kw): return "analyzed"

    async def fake_extract_all(*_a, **_kw):
        return [
            core._Batch(_Unit(name, i), [_prop(t) for t in titles], _FeatCtx())  # type: ignore[arg-type]
            for i, (name, titles) in enumerate(units.items())
        ]

    async def fake_report(*_a, **_kw): return object()

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    monkeypatch.setattr(core, "build_report", fake_report)
    await run_pipeline(_Backend(_Prepared(formalizer)), _Run(), max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]
    return formalizer


async def test_begin_runs_once_before_any_unit_is_formalized(monkeypatch):
    s = await _drive(monkeypatch, {"deposits": ["a"], "admin": ["b"], "farms": ["c"]}, _Staged())
    names = [c[0] for c in s.calls]
    assert names[0] == "begin", f"begin must precede every formalize; got {names}"
    assert names.count("begin") == 1
    assert sorted(names[1:]) == ["formalize:admin", "formalize:deposits", "formalize:farms"]


async def test_begin_sees_every_unit_s_properties(monkeypatch):
    # The point of the staged type: the shared artifact is designed around all of them, not around
    # whichever unit won the race to formalize first.
    s = await _drive(monkeypatch, {"deposits": ["a", "b"], "admin": ["c"], "farms": ["d"]}, _Staged())
    assert s.calls[0] == ("begin", ["a", "b", "c", "d"])


async def test_begin_still_runs_for_a_single_unit(monkeypatch):
    # The K=1 case must go down the same path, or the shared artifact would be authored lazily
    # again the moment a second unit appears.
    s = await _drive(monkeypatch, {"whole-program": ["a", "b"]}, _Staged())
    assert [c[0] for c in s.calls] == ["begin", "formalize:whole-program"]


async def test_an_unstaged_formalizer_is_formalized_directly(monkeypatch):
    # The other arm of the union: a backend with no shared artifact returns the formalizer itself,
    # and the driver must fan out over exactly that object rather than looking for a staging step.
    f = await _drive(monkeypatch, {"deposits": ["a"], "admin": ["b"]}, _Formalizer())
    assert sorted(c[0] for c in f.calls) == ["formalize:admin", "formalize:deposits"]
