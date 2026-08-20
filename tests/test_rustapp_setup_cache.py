"""Tests for the shared setup spec: when it is authored, from what, and its cache.

Three things are pinned here. **When**: not during ``prepare_formalization`` (which runs
concurrently with property extraction, so the properties don't exist yet) but in
``StagedFormalizer.begin`` — after extraction, before the per-unit fan-out, which is also the call
that produces the formalizer. **From what**: the union of *every* unit's
properties, not whichever unit happened to formalize first; the artifact is what makes those
properties checkable, so a multi-component run whose fixture only knew one component's properties
would tell the rest to work within a surface designed without them
(docs/crucible.md §7). **Caching**: authoring it is a full LLM loop, on a large
program the longest single step of a run, so a re-run after something failed downstream must not pay
for it again. Like the driver's other caches it only stores when the run has a cache namespace
(``--cache-ns``); without one every step is recomputed, by design.

Stubs throughout: the "author" step is a counter, the store is a dict.
"""

import json
from dataclasses import dataclass
from typing import Any, cast

import pytest

from composer.pipeline.ptypes import BackendJob
from composer.spec.types import PropertyFormulation

import composer.rustapp.adapter as adapter
from composer.rustapp.adapter import (
    RustFormalizer, RustPreparedSystem, RustStagedFormalizer, _setup_identity
)
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.session import SessionResult
from tests.conftest import wire_descriptor, wire_phase, wire_required_phases
from composer.rustapp.wire import Property, SetupInput
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
        #: Every ``TaskInfo`` the backend built, in order.
        self.tasks: list = []

    async def runner(self, task_info, job):
        self.tasks.append(task_info)
        return await job()


def _descriptor() -> AppDescriptor:
    return AppDescriptor.model_validate(
        wire_descriptor(
            ecosystem="solana",
            phases=[
                *wire_required_phases(),
                wire_phase("build_harness", "Build Harness", 4, "setup"),
            ],
        )
    )


PROPS = [
    PropertyFormulation(title="no overflow", sort="invariant", description="balance never overflows")
]


class _Module:
    """Stands in for the compiled wheel. Only ``crate_root`` is reachable from these tests — ``begin``
    asks for the run's build scaffolding once it knows the unit set — and this app declares none."""

    #: Every payload ``begin`` sent, so a test can assert what the wheel was told.
    def __init__(self):
        self.crate_root_calls: list[dict] = []

    def crate_root(self, input_json: str) -> str | None:
        self.crate_root_calls.append(json.loads(input_json))
        return None

    def checks(self, _input_json: str) -> str:
        return "[]"


@dataclass(frozen=True)
class _Unit:
    """The two `FeatureUnit` members `begin` reads: the display name a property carries onto the
    wire, and the unit object itself — which the setup turn and the crate-root write both send, so a
    wheel can name the build target for a unit that has not been formalized yet."""
    display_name: str

    def feature_json(self) -> dict[str, object]:
        return {"slug": self.display_name}


def _jobs(*prop_lists: list[PropertyFormulation], names: list[str] | None = None) -> list[BackendJob]:
    """One `BackendJob` per unit — what the driver hands `begin` after extraction."""
    units = names or [f"unit{i}" for i in range(len(prop_lists))]
    return [
        BackendJob(feat=cast(object, _Unit(n)), props=p) for n, p in zip(units, prop_lists)
    ]


async def _formalizer(
    monkeypatch, ctx, authored: list[str], tmp_path, *, props=None, jobs=None, run=None, module=None
) -> RustFormalizer:
    """Drive prepare→begin with the LLM authoring stubbed, returning the formalizer ``begin`` built
    around the authored fixture."""
    from composer.rustapp.host import build_backend, build_phase_model
    from composer.spec.context import SourceCode
    from composer.spec.system_model import SolidityIdentifier

    async def fake_session(*, input, **_kw):
        authored.append(input)
        return SessionResult(
            commentary="", spec=FIXTURE, skipped=[], property_checks=[],
            verdicts={}, ran=[], expected_failures={},
        )

    async def fake_prep(_module, _input, **_kw):
        return {}  # a plan that asked for nothing establishes nothing

    monkeypatch.setattr(adapter, "run_session", fake_session)
    monkeypatch.setattr(adapter, "run_workspace_prep", fake_prep)

    source = SourceCode(
        content=None,  # type: ignore[arg-type]  — unused by prepare_formalization
        project_root=str(tmp_path),
        contract_name=SolidityIdentifier("example_lending"),
        relative_path="programs/lend/src/lib.rs",
        forbidden_read="",
    )
    descriptor = _descriptor()
    backend = build_backend(
        module or _Module(),  # type: ignore[arg-type]
        descriptor, source, phases=build_phase_model(descriptor),
    )
    run = run or _Run(ctx)
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


async def _prepare(
    monkeypatch, ctx, authored: list[str], tmp_path, *, props=None, jobs=None, module=None
) -> str | None:
    """As :func:`_formalizer`, narrowed to the authored fixture itself."""
    f = await _formalizer(monkeypatch, ctx, authored, tmp_path, props=props, jobs=jobs, module=module)
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


async def test_the_artifact_reaches_every_component_as_the_inputs_setup(monkeypatch, tmp_path):
    # How the fixture actually gets to the components: `begin` builds the formalizer around the
    # authored artifact, which it puts on every component's ``AuthorInput.setup`` — never assigned
    # onto a formalizer that is already running.
    store, authored = _Store(), []
    f = await _formalizer(monkeypatch, _ctx(store, namespace=None), authored, tmp_path)
    assert f._setup_result == FIXTURE


async def test_the_setup_task_is_tagged_with_the_declared_phase_member(monkeypatch, tmp_path):
    # The authoring loop runs as its own visible task, tagged with the phase the wheel declared for
    # it ("build_harness" here) — resolved against the backend's own synthesized enum, since the
    # frontend keys its section labels by enum member identity. `RustBackend.task_info` is what does
    # that; the id half comes from the step's kind, not from a string at the call site.
    store, authored = _Store(), []
    ctx = _ctx(store, namespace=None)
    run = _Run(ctx)
    f = await _formalizer(monkeypatch, ctx, authored, tmp_path, run=run)
    assert f is not None

    info = run.tasks[0]
    assert info.task_id == "demoprover-setup"
    assert info.label == "Build Harness"
    assert info.phase.name == "build_harness"


async def test_the_setup_artifact_is_authored_from_the_extracted_properties(monkeypatch, tmp_path):
    # The point of deferring it: the prompt input carries the properties the fixture must support,
    # so the fixture can expose an action per instruction they exercise instead of guessing.
    store, authored = _Store(), []
    await _prepare(monkeypatch, _ctx(store, namespace=None), authored, tmp_path)
    assert [p.title for p in authored[0].props] == ["no overflow"]
    assert authored[0].kind == "setup"


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
    assert [p.title for p in authored[0].props] == [
        "deposit_conserves", "only_admin_sets_fee", "stake_matches_position"
    ]


async def test_the_crate_root_is_written_once_from_the_whole_unit_set(monkeypatch, tmp_path):
    # The other half of what `begin` is for. Scaffolding for a multi-unit build (a manifest's feature
    # list, a crate root's module declarations) is a function of the SET, so it can only be written
    # here — after the setup spec exists and before the units fan out. Writing it once is what lets
    # each per-unit gate emit only its own files instead of re-rendering the whole crate.
    store, authored, module = _Store(), [], _Module()
    props = [PropertyFormulation(title="p", sort="invariant", description="d")]
    await _prepare(
        monkeypatch, _ctx(store, namespace=None), authored, tmp_path,
        jobs=_jobs(props, props, names=["deposits", "farms"]), module=module,
    )
    assert len(module.crate_root_calls) == 1, "written once per run, not once per unit"
    sent = module.crate_root_calls[0]
    # Every unit, whole — including ones that will later give up, since the declaration cannot wait
    # for an outcome.
    assert [u["slug"] for u in sent["units"]] == ["deposits", "farms"]
    # …alongside the authored fixture, which is the other input the scaffolding needs.
    assert sent["setup"] == FIXTURE


async def test_every_units_properties_reach_every_components_own_input(monkeypatch, tmp_path):
    # The other thing only `begin` holds. The shared artifact is authored from every unit's
    # properties, so a failure it reports can name any of them — including one belonging to a unit
    # other than the one whose target is running. A gate told only its own properties cannot tell
    # that from a title nobody owns, and the safe reading of an unplaceable failure (refute
    # everything) is exactly wrong for the first case
    # (docs/crucible.md §8).
    store, authored = _Store(), []
    ctx = _ctx(store, namespace=None)
    run = _Run(ctx)
    deposits = [PropertyFormulation(title="deposit_conserves", sort="invariant", description="d")]
    admin = [PropertyFormulation(title="only_admin_sets_fee", sort="safety_property", description="a")]
    f = await _formalizer(
        monkeypatch, ctx, authored, tmp_path, run=run,
        jobs=_jobs(deposits, admin, names=["deposits", "admin"]),
    )
    await f.formalize("Deposits", cast(object, _Unit("deposits")), deposits, ctx, run, cast(Any, None))  # type: ignore[arg-type]

    component = authored[-1]
    assert component.kind == "component"
    # Its own properties are what it must formalize…
    assert [p.title for p in component.props] == ["deposit_conserves"]
    # …and the run's are context beside them, each naming the unit that owns it.
    assert [(p.component, p.title) for p in component.run_props] == [
        ("deposits", "deposit_conserves"), ("admin", "only_admin_sets_fee"),
    ]


async def test_same_titled_properties_of_two_components_are_both_carried(monkeypatch, tmp_path):
    # A title is unique only within the component it was inferred for (extraction validates that
    # much), so two components can each carry a "solvency" property and mean different things by it.
    # Both are properties the fixture has to make checkable, and both are properties their component
    # will be asked to formalize — merging them by title would author the artifact around one.
    store, authored = _Store(), []
    pool = PropertyFormulation(title="solvency", sort="invariant", description="the pool is solvent")
    vault = PropertyFormulation(title="solvency", sort="invariant", description="the vault is solvent")
    other = PropertyFormulation(title="only_admin", sort="safety_property", description="a")
    await _prepare(
        monkeypatch, _ctx(store, namespace=None), authored, tmp_path,
        jobs=_jobs([pool], [vault, other], names=["Pool", "Vault"]),
    )
    # Each carries the unit that inferred it, which is what tells the two "solvency" apart — the
    # wheel is authoring one artifact for both and has to know whose surface each is stated over.
    assert [(p.component, p.title) for p in authored[0].props] == [
        ("Pool", "solvency"), ("Vault", "solvency"), ("Vault", "only_admin"),
    ]
    # …and the wheel can still name a check per property: the host-assigned slugs stay distinct.
    assert len({p.slug for p in authored[0].props}) == 3


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
    base = SetupInput(
        program="example_lending",
        source_unit={"dir": "programs/lend", "lib": "example_lending"},
        model={"programs": [{"name": "example_lending"}]},
        props=[
            Property(component="Lend", title="no overflow", sort="invariant", description="d")
        ],
        args={"fuzz_timeout": 30},
        prep_facts={"idl": "fuzz/x/idls/example_lending.json"},
    )
    same = _setup_identity(base)

    def varied(**update) -> str:
        return _setup_identity(base.model_copy(update=update))

    # A fuzz budget doesn't change what gets authored — keying on it would discard the artifact.
    assert varied(args={"fuzz_timeout": 900}) == same
    # Everything the prompt is built from does.
    assert varied(program="other") != same
    assert varied(model={"programs": []}) != same
    assert varied(source_unit={"dir": "programs/other"}) != same
    # …including the properties: they are what the fixture is designed around.
    assert varied(props=[]) != same
    # …down to the unit each was inferred for: the same title stated over another unit's surface is
    # another property, and the fixture has to support it there too.
    assert varied(props=[base.props[0].model_copy(update={"component": "Farms"})]) != same
    # …including what the prep established, which is what decides where the types come from.
    assert varied(prep_facts={}) != same
    # Stable across key ordering *within* the opaque model — it arrives as JSON, and the wire model
    # fixes the order of everything else.
    model = {"programs": [{"name": "example_lending"}], "extra": {"a": 1, "b": 2}}
    reordered = json.loads(json.dumps({k: model[k] for k in reversed(list(model))}))
    assert varied(model=model) == varied(model=reordered)
