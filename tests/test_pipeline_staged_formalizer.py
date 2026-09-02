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

from composer.pipeline.run_mode import RunMode
from composer.authoring.state import SkippedProperty
from composer.spec.prioritize import PropertyGroup, PropertyRanking, RankedProperty
import composer.pipeline.core as core
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import EVM
from composer.spec.types import PropertyFormulation

pytestmark = pytest.mark.asyncio


class _Store:
    def write_properties(self, *_a, **_kw): ...
    def write_artifact(self, *_a, **_kw): return "artifact"
    def write_report(self, *_a, **_kw): ...
    def write_ranking(self, *_a, **_kw): return "property_ranking.json"


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

    async def cache_get(self, _ty): return None

    async def cache_put(self, _v): ...


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
    content = None


class _Env:
    """Only the model accessor the ranker's call site reaches for."""

    def llm_heavy(self): return None


class _Run:
    source = _Source()
    run_mode = RunMode.COMPREHENSIVE
    env = _Env()
    ctx = _Ctx()

    async def runner(self, task_info, job):
        return await job()


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description="d")


async def _drive[F: (_Staged, _Formalizer)](
    monkeypatch, units: dict[str, list[str]], formalizer: F, *,
    run_mode: RunMode = RunMode.COMPREHENSIVE,
    ranking=None,
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
    if ranking is not None:
        async def fake_rank(**_kw): return ranking
        monkeypatch.setattr(core, "rank_properties", fake_rank)

    run = _Run()
    run.run_mode = run_mode  # type: ignore[misc]
    await run_pipeline(_Backend(_Prepared(formalizer)), run, max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]
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



# ---------------------------------------------------------------------------
# Prioritized mode: the cut, and what it must leave alone
# ---------------------------------------------------------------------------


def _ranking(winner, also, order):
    """Score ``winner`` above everything else, so the derived focus is deterministic. ``also``
    names the other titles sharing the winner's claim."""
    with_winner = [winner, *((winner[0], t) for t in also)]
    return PropertyRanking(
        ranked=[
            RankedProperty(key=k, score=90 if k == winner else 10, critical_match=False,
                           rationale="r")
            for k in order
        ],
        groups=[
            PropertyGroup(claim="the claim under test", members=with_winner),
            *(PropertyGroup(claim=f"claim:{k[1]}", members=[k])
              for k in order if k not in with_winner),
        ],
        justification="j",
    )


async def test_prioritized_formalizes_one_batch_and_begin_still_sees_only_it(monkeypatch):
    # The whole saving: three components' properties are extracted, one batch is formalized.
    # ``begin`` is handed the pruned list too, so a backend that authors a shared artifact from
    # the batches authors it from the focus rather than from everything.
    units = {"deposits": ["a", "b"], "admin": ["c"], "farms": ["d"]}
    order = [("0: deposits", "a"), ("0: deposits", "b"), ("1: admin", "c"), ("2: farms", "d")]
    s = await _drive(
        monkeypatch, units, _Staged(), run_mode=RunMode.PRIORITIZED,
        ranking=_ranking(("0: deposits", "a"), ["b"], order),
    )
    assert s.calls[0] == ("begin", ["a", "b"])
    assert [c[0] for c in s.calls[1:]] == ["formalize:deposits"]


async def test_prioritized_leaves_the_pre_formalization_overlap_alone(monkeypatch):
    # D2: the fixed prefix is not what this mode makes cheaper, and it must keep running —
    # ``begin`` is still called exactly once, before any formalization.
    units = {"deposits": ["a"], "admin": ["b"]}
    order = [("0: deposits", "a"), ("1: admin", "b")]
    s = await _drive(
        monkeypatch, units, _Staged(), run_mode=RunMode.PRIORITIZED,
        ranking=_ranking(("1: admin", "b"), [], order),
    )
    names = [c[0] for c in s.calls]
    assert names.count("begin") == 1 and names[0] == "begin"
    assert names[1:] == ["formalize:admin"]


async def test_comprehensive_never_calls_the_ranker(monkeypatch):
    def boom(**_kw):
        raise AssertionError("the ranker must not run in comprehensive mode")

    monkeypatch.setattr(core, "rank_properties", boom)
    s = await _drive(monkeypatch, {"deposits": ["a"], "admin": ["b"]}, _Staged())
    assert sorted(c[0] for c in s.calls) == ["begin", "formalize:admin", "formalize:deposits"]


# ---------------------------------------------------------------------------
# A cache hit must not smuggle a retired focus past the protections
# ---------------------------------------------------------------------------


class _Result:
    """The two members ``_focus_satisfied`` reads off a backend result."""

    def __init__(self, mapped: list[str], skipped: list[str] = []):
        self._mapped = mapped
        self.skipped = [SkippedProperty(property_title=t, reason="r") for t in skipped]

    def property_checks(self):
        return [(t, ["rule_" + t]) for t in self._mapped]


def _props(*titles):
    return [PropertyFormulation(title=t, sort="invariant", description="d") for t in titles]


def test_a_result_that_maps_every_focus_property_satisfies_the_focus():
    assert core._focus_satisfied(_Result(["a", "b"]), _props("a", "b"))  # type: ignore[arg-type]


def test_a_skipped_focus_property_does_not_satisfy_the_focus():
    # The exact hazard: a cached comprehensive result that retired the property a prioritized run
    # exists to establish. A replay never runs the author's tools, so nothing else would catch it.
    assert not core._focus_satisfied(  # type: ignore[arg-type]
        _Result(["b"], skipped=["a"]), _props("a", "b")
    )


def test_an_unmapped_focus_property_does_not_satisfy_the_focus():
    assert not core._focus_satisfied(_Result(["a"]), _props("a", "b"))  # type: ignore[arg-type]
    assert not core._focus_satisfied(_Result([]), _props("a"))  # type: ignore[arg-type]


async def test_ranking_does_not_wait_for_pre_formalization(monkeypatch):
    """The ranking reads nothing ``prepare_formalization`` produces, so it must not queue
    behind it. On a real contract that setup is hours of autosetup and invariant proving, and
    the focus is knowable the moment extraction lands."""
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowPrepared(_Prepared):
        async def prepare_formalization(self, run):
            order.append("prep:start")
            started.set()
            await release.wait()
            order.append("prep:done")
            return await super().prepare_formalization(run)

    async def fake_analysis(*_a, **_kw): return "analyzed"

    async def fake_extract_all(*_a, **_kw):
        await started.wait()  # the setup is in flight, exactly as in a real run
        return [core._Batch(_Unit("deposits", 0), [_prop("a")], _FeatCtx())]  # type: ignore[arg-type]

    async def fake_rank(**_kw):
        order.append("rank")
        release.set()  # only now let the setup finish; if the driver awaited it first, we deadlock
        return _ranking(("0: deposits", "a"), [], [("0: deposits", "a")])

    async def fake_report(*_a, **_kw): return object()

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    monkeypatch.setattr(core, "rank_properties", fake_rank)
    monkeypatch.setattr(core, "build_report", fake_report)

    run = _Run()
    run.run_mode = RunMode.PRIORITIZED  # type: ignore[misc]
    formalizer = _Staged()
    backend = _Backend(_SlowPrepared(formalizer))
    async with asyncio.timeout(5):
        await run_pipeline(backend, run, max_bug_rounds=1, ecosystem=EVM)  # type: ignore[arg-type]

    assert order == ["prep:start", "rank", "prep:done"], (
        f"ranking must run while pre-formalization is still in flight; got {order}"
    )
