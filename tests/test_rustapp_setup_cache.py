"""Tests for the shared setup artifact: when it is authored, from what, and its cache.

Three things are pinned here. **When**: not during ``prepare_formalization`` (which runs
concurrently with property extraction, so the properties don't exist yet) but in
``StagedFormalizer.begin`` — after extraction, before the per-unit fan-out, which is also the call
that produces the formalizer. **From what**: the union of *every* unit's
properties, not whichever unit happened to formalize first; the artifact is what makes those
properties checkable, so a multi-component run whose fixture only knew one component's properties
would tell the rest to work within a surface designed without them
(docs/crucible-component-units.md (PR3) §8.2). **Caching**: authoring it is a full LLM loop, on a large
program the longest single step of a run, so a re-run after something failed downstream must not pay
for it again. Like the driver's other caches it only stores when the run has a cache namespace
(``--cache-ns``); without one every step is recomputed, by design.

Stubs throughout: the "author" step is a counter, the store is a dict.
"""

import json
from typing import cast

import pytest

from composer.pipeline.ptypes import BackendJob
from composer.spec.types import PropertyFormulation

import composer.rustapp.adapter as adapter
from composer.rustapp.adapter import (
    RustFormalizer, RustPreparedSystem, RustStagedFormalizer, _setup_identity
)
from composer.rustapp.descriptor import AppDescriptor
from composer.spec.context import WorkflowContext

pytestmark = pytest.mark.asyncio

FIXTURE = "// FIXTURE\nstruct Fixture {}"


class _Store:
    """The subset of ``BaseStore`` the typed cache uses."""

    def __init__(self):
        self.items: dict[tuple, dict] = {}

    async def aget(self, ns, key):
        value = self.items.get((tuple(ns), key))
        return None if value is None else type("Item", (), {"value": value})()

    async def aput(self, ns, key, value):
        self.items[(tuple(ns), key)] = value

    async def adelete(self, ns, key):
        self.items.pop((tuple(ns), key), None)


class _Run:
    def __init__(self, ctx):
        self.ctx = ctx
        self.env = None
        self.source = None

    async def runner(self, _task_info, job):
        return await job()


def _descriptor() -> AppDescriptor:
    return AppDescriptor.model_validate(
        {
            "name": "crucible",
            "header_text": "h",
            "ecosystem": "solana",
            "backend_tag": "crucible",
            "backend_guidance": "g",
            "analysis_key": "k",
            "phases": [
                {"key": "analysis", "label": "A", "order": 0, "core_slot": "analysis"},
                {"key": "extraction", "label": "E", "order": 1, "core_slot": "extraction"},
                {"key": "build_harness", "label": "Build Harness", "order": 2},
                {"key": "formalization", "label": "F", "order": 3, "core_slot": "formalization"},
                {"key": "report", "label": "R", "order": 4, "core_slot": "report"},
            ],
            "artifact_layout": {
                "deliverable_dir": "d", "internal_dir": "i", "report_dir": "r",
                "artifact_dir": "a", "artifact_prefix": "p", "artifact_extension": "rs",
                "property_suffix": "s",
            },
            "setup": {"phase_key": "build_harness", "label": "Build Harness",
                      "context_key": "fixture"},
        }
    )


PROPS = [
    PropertyFormulation(title="no overflow", sort="invariant", description="balance never overflows")
]


def _jobs(*prop_lists: list[PropertyFormulation]) -> list[BackendJob]:
    """One `BackendJob` per unit — what the driver hands `begin` after extraction."""
    return [BackendJob(feat=cast(object, f"unit{i}"), props=p) for i, p in enumerate(prop_lists)]


async def _formalizer(
    monkeypatch, ctx, authored: list[str], tmp_path, *, props=None, jobs=None
) -> RustFormalizer:
    """Drive prepare→begin with the LLM authoring stubbed, returning the formalizer ``begin`` built
    around the authored fixture."""
    from composer.rustapp.host import build_backend
    from composer.spec.context import SourceCode
    from composer.spec.system_model import SolidityIdentifier

    async def fake_author_and_compile(_module, input_dict, **_kw):
        authored.append(input_dict)
        return FIXTURE

    async def fake_prep(_module, _input, **_kw):
        return None  # no IDL requested

    monkeypatch.setattr(adapter, "author_and_compile", fake_author_and_compile)
    monkeypatch.setattr(adapter, "run_workspace_prep", fake_prep)

    source = SourceCode(
        content=None,  # type: ignore[arg-type]  — unused by prepare_formalization
        project_root=str(tmp_path),
        contract_name=SolidityIdentifier("example_lending"),
        relative_path="programs/lend/src/lib.rs",
        forbidden_read="",
    )
    backend = build_backend(object(), _descriptor(), source)
    run = _Run(ctx)
    run.source = source  # type: ignore[assignment]
    # The workspace prep happens in the backend's preflight (concurrently with analysis, before this
    # point) and hands its outcome forward — here the stubbed "no IDL requested".
    preflight = await backend.preflight(cast(object, run))  # type: ignore[arg-type]
    prepared = RustPreparedSystem("main", backend, preflight)
    before = len(authored)  # callers reuse one list across repeat runs, so count this run's own
    staged = await prepared.prepare_formalization(run)  # type: ignore[arg-type]
    # This run has authored nothing yet, and there is no formalizer to inspect — prep runs alongside
    # extraction, so the properties aren't known here. A wheel that declares a `setup` step gets
    # the staged type back, and `begin` is the only thing that can turn it into a formalizer.
    assert isinstance(staged, RustStagedFormalizer)
    assert len(authored) == before
    return await staged.begin(jobs or _jobs(props if props is not None else PROPS), run)  # type: ignore[arg-type]


async def _prepare(monkeypatch, ctx, authored: list[str], tmp_path, *, props=None, jobs=None) -> str | None:
    """As :func:`_formalizer`, narrowed to the authored fixture itself."""
    f = await _formalizer(monkeypatch, ctx, authored, tmp_path, props=props, jobs=jobs)
    return f._setup_result


def _ctx(store, *, namespace) -> WorkflowContext:
    return WorkflowContext.create(
        services=lambda _ns: None,  # type: ignore[arg-type]
        thread_id="t", store=store, recursion_limit=10, cache_namespace=namespace,
    )


async def test_the_setup_artifact_is_authored_once_and_then_reused(monkeypatch, tmp_path):
    store, authored = _Store(), []
    first = await _prepare(monkeypatch, _ctx(store, namespace=("run-ns",)), authored, tmp_path)
    assert first == FIXTURE and len(authored) == 1

    # A second run with the same namespace (i.e. `--cache-ns` on both) reuses it: no second LLM loop.
    second = await _prepare(monkeypatch, _ctx(store, namespace=("run-ns",)), authored, tmp_path)
    assert second == FIXTURE and len(authored) == 1


async def test_the_artifact_reaches_every_component_under_its_declared_context_key(monkeypatch, tmp_path):
    # How the fixture actually gets to the components: `begin` builds the formalizer with the
    # artifact already in the context blob, under the `context_key` the wheel declared ("fixture"
    # here). This is the seam that used to be an assignment onto a live formalizer.
    store, authored = _Store(), []
    f = await _formalizer(monkeypatch, _ctx(store, namespace=None), authored, tmp_path)
    assert f._context_extra["fixture"] == FIXTURE


async def test_the_setup_artifact_is_authored_from_the_extracted_properties(monkeypatch, tmp_path):
    # The point of deferring it: the prompt input carries the properties the fixture must support,
    # so the fixture can expose an action per instruction they exercise instead of guessing.
    store, authored = _Store(), []
    await _prepare(monkeypatch, _ctx(store, namespace=None), authored, tmp_path)
    assert [p["title"] for p in authored[0]["props"]] == ["no overflow"]
    assert authored[0]["kind"] == "setup"


async def test_the_artifact_is_authored_from_every_unit_s_properties(monkeypatch, tmp_path):
    # The multi-component case, and the reason `begin` exists. With K units the fixture must be
    # designed around ALL of their properties; authoring it from whichever unit formalized first
    # would leave the other K-1 asserting against a surface built without them.
    store, authored = _Store(), []
    deposits = [PropertyFormulation(title="deposit_conserves", sort="invariant", description="d")]
    admin = [PropertyFormulation(title="only_admin_sets_fee", sort="safety_property", description="a")]
    farms = [PropertyFormulation(title="stake_matches_position", sort="invariant", description="f")]
    await _prepare(
        monkeypatch, _ctx(store, namespace=None), authored, tmp_path,
        jobs=_jobs(deposits, admin, farms),
    )
    assert len(authored) == 1, "the shared artifact is authored once, not once per unit"
    assert [p["title"] for p in authored[0]["props"]] == [
        "deposit_conserves", "only_admin_sets_fee", "stake_matches_position"
    ]


async def test_a_property_two_components_share_is_carried_once(monkeypatch, tmp_path):
    # Units are disjoint but their properties need not be; the union is de-duplicated by title so
    # the artifact's cache identity doesn't change just because two components agreed.
    store, authored = _Store(), []
    shared = PropertyFormulation(title="solvency", sort="invariant", description="s")
    other = PropertyFormulation(title="only_admin", sort="safety_property", description="a")
    await _prepare(
        monkeypatch, _ctx(store, namespace=None), authored, tmp_path,
        jobs=_jobs([shared], [shared, other]),
    )
    assert [p["title"] for p in authored[0]["props"]] == ["solvency", "only_admin"]


async def test_a_different_property_set_authors_a_different_artifact(monkeypatch, tmp_path):
    # …and since the properties shape it, they are part of its cache identity.
    store, authored = _Store(), []
    ns = ("run-ns",)
    await _prepare(monkeypatch, _ctx(store, namespace=ns), authored, tmp_path)
    other = [PropertyFormulation(title="other", sort="invariant", description="d")]
    await _prepare(monkeypatch, _ctx(store, namespace=ns), authored, tmp_path, props=other)
    assert len(authored) == 2


async def test_without_a_cache_namespace_nothing_is_stored(monkeypatch, tmp_path):
    # The default: `--cache-ns` absent → `cache_namespace=None` → every step is recomputed, and the
    # store is never even written to. This is why a re-run repeats the whole pipeline.
    store, authored = _Store(), []
    for _ in range(2):
        assert await _prepare(monkeypatch, _ctx(store, namespace=None), authored, tmp_path) == FIXTURE
    assert len(authored) == 2
    assert store.items == {}


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_setup_key_covers_what_it_is_authored_from_and_not_run_knobs():
    base = {
        "program": "example_lending",
        "program_crate": {"dir": "programs/lend", "lib": "example_lending"},
        "component": {"programs": [{"name": "example_lending"}]},
        "props": [{"title": "no overflow", "sort": "invariant", "description": "d"}],
        "context": {"fuzz_timeout": 30, "idl": "fuzz/x/idls/example_lending.json"},
    }
    same = _setup_identity(base)

    # A fuzz budget doesn't change what gets authored — keying on it would discard the artifact.
    assert _setup_identity({**base, "context": {**base["context"], "fuzz_timeout": 900}}) == same
    # Everything the prompt is built from does.
    assert _setup_identity({**base, "program": "other"}) != same
    assert _setup_identity({**base, "component": {"programs": []}}) != same
    assert _setup_identity({**base, "program_crate": {"dir": "programs/other"}}) != same
    # …including the properties: they are what the fixture is designed around.
    assert _setup_identity({**base, "props": []}) != same
    # …including which source the types come from: crate deps and IDL generation differ.
    assert _setup_identity({**base, "context": {"fuzz_timeout": 30}}) != same
    # Stable across dict ordering (the analyzed model arrives as JSON).
    reordered = json.loads(json.dumps({k: base[k] for k in reversed(list(base))}))
    assert _setup_identity(reordered) == same
