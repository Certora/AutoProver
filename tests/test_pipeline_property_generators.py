"""Property generators: an installed generator replaces system analysis and property extraction
with the property set it returns, and the run continues from that set as it would after its own
extraction. Also the ``property_generation`` run mode, which stops once the set is written.

Stubs throughout — no LLM, no DB, no backend wheel.
"""

from contextlib import asynccontextmanager
from typing import Any

import pytest

import composer.pipeline.core as core
import composer.pipeline.property_generators as gen_mod
from composer.pipeline.core import run_pipeline
from composer.pipeline.plugin_api import AnyEcosystem
from composer.pipeline.property_generators import (
    PROPERTY_GENERATOR_ENTRY_POINT_GROUP, PROPERTY_GENERATOR_ENV, PropertyGeneratorImpl,
    PropertyGeneratorLoader, load_property_generator, resolve_property_generator,
)
from composer.pipeline.property_set import PropertySet, PropertySetComponent
from composer.pipeline.run_mode import RunMode

from tests.test_evaluate_mode import _EntryPoint, _Impl, _install, _loader_for
from tests.test_pipeline_property_set import (
    ANALYZED, ECOSYSTEM, UNITS, _CachingRun, _Drive, _set_prop,
)
from tests.test_pipeline_staged_formalizer import _prop, _ranking

pytestmark = pytest.mark.asyncio


def _generated(**update: Any) -> PropertySet:
    ps = PropertySet(
        ecosystem="evm", contract_name="Vault", run_mode=RunMode.COMPREHENSIVE,
        system_model=ANALYZED.model_dump(mode="json"),
        property_components=[
            PropertySetComponent(
                name=name, unit_index=i, properties=[_set_prop("gen_" + t) for t in titles],
            )
            for i, (name, titles) in enumerate(UNITS.items())
        ],
    )
    return ps.model_copy(update=update)


class _Generator(PropertyGeneratorImpl[Any]):
    def __init__(self, result: PropertySet) -> None:
        self.result = result
        self.seen: list[Any] = []

    async def generate(self, ctx):
        self.seen.append(ctx)
        return self.result


async def _drive_generated(
    monkeypatch, generator: _Generator, *,
    run_mode: RunMode = RunMode.COMPREHENSIVE,
    ranking=None,
    interactive: bool = False,
    predefined: PropertySet | None = None,
) -> _Drive:
    """Run the driver with ``generator`` as the run's property generator. The native analysis
    and extraction fail the test if they are reached."""
    d = _Drive()

    async def never(*_a, **_kw):
        raise AssertionError("a generated run must not reach the native analysis or extraction")

    async def fake_report(*_a, components, **_kw):
        d.report_components = components
        return object()

    @asynccontextmanager
    async def fake_load(name, _unit_type):
        yield generator if name is not None else None

    monkeypatch.setattr(core, "run_component_analysis", never)
    monkeypatch.setattr(core, "_extract_all", never)
    monkeypatch.setattr(core, "build_report", fake_report)
    monkeypatch.setattr(core, "load_property_generator", fake_load)
    if ranking is not None:
        async def fake_rank(**_kw): return ranking
        monkeypatch.setattr(core, "rank_properties", fake_rank)

    run = _CachingRun()
    run.run_mode = run_mode  # type: ignore[misc]
    run.property_generator = "fake"  # type: ignore[misc]
    d.result = await run_pipeline(
        d.backend, run, max_bug_rounds=1, ecosystem=ECOSYSTEM,  # type: ignore[arg-type]
        interactive=interactive, predefined=predefined,
    )
    return d


async def test_a_generator_replaces_analysis_and_extraction(monkeypatch):
    g = _Generator(_generated())

    d = await _drive_generated(monkeypatch, g)

    assert d.backend.analyzed == ANALYZED
    assert sorted(d.formalizer.calls) == [
        ("formalize:admin", ["gen_c"]), ("formalize:deposits", ["gen_a", "gen_b"]),
    ]
    [ctx] = g.seen
    assert not ctx.existing_specs
    recorded = d.backend.artifact_store.property_set
    assert recorded is not None
    assert [(c.name, [p.title for p in c.properties]) for c in recorded.property_components] == [
        ("deposits", ["gen_a", "gen_b"]), ("admin", ["gen_c"]),
    ]


async def test_generated_properties_go_through_the_post_inference_plugins(monkeypatch):
    seen: list[str] = []

    async def drop_admin(_prop_ctx, _plugins, feat, props):
        seen.append(feat.display_name)
        return [] if feat.display_name == "admin" else props

    monkeypatch.setattr(core, "_post_process_props", drop_admin)

    d = await _drive_generated(monkeypatch, _Generator(_generated()))

    assert seen == ["deposits", "admin"]
    # A component the plugins empty is dropped, as after the native extraction.
    assert d.formalizer.calls == [("formalize:deposits", ["gen_a", "gen_b"])]


async def test_a_generated_set_is_ranked_in_a_prioritized_run(monkeypatch):
    order = [("0: deposits", "gen_a"), ("0: deposits", "gen_b"), ("1: admin", "gen_c")]
    d = await _drive_generated(
        monkeypatch, _Generator(_generated()), run_mode=RunMode.PRIORITIZED,
        ranking=_ranking(("1: admin", "gen_c"), [], order),
    )

    assert d.formalizer.calls == [("formalize:admin", ["gen_c"])]


async def test_an_evaluate_run_hands_the_generated_set_to_the_mode(monkeypatch):
    impl = _Impl()
    _install(monkeypatch, _EntryPoint("evaluate", _loader_for(impl, [])))
    g = _Generator(_generated())

    d = await _drive_generated(monkeypatch, g, run_mode=RunMode.EVALUATE)

    [gen_ctx] = g.seen
    assert gen_ctx.existing_specs
    [ctx] = impl.seen
    assert [(b.feat.display_name, [p.title for p in b.props]) for b in ctx.batches] == [
        ("deposits", ["gen_a", "gen_b"]), ("admin", ["gen_c"]),
    ]
    assert not d.prepared.prepared_formalization


async def test_a_property_generation_run_stops_once_the_set_is_written(monkeypatch):
    d = await _drive_generated(
        monkeypatch, _Generator(_generated()), run_mode=RunMode.PROPERTY_GENERATION,
    )

    ps = d.backend.artifact_store.property_set
    assert ps is not None and ps.run_mode is RunMode.PROPERTY_GENERATION
    assert not d.prepared.prepared_formalization and not d.prepared.prepared_external_mode
    assert d.formalizer.calls == [] and d.report_components is None
    assert d.result is not None and (d.result.n_components, d.result.n_properties) == (2, 3)
    assert not d.result.all_failed


async def test_the_recorded_set_keeps_the_generated_ranks(monkeypatch):
    ps = _generated()
    first, *rest = ps.property_components
    ranked = ps.model_copy(update={"property_components": [
        first.model_copy(update={"properties": [
            first.properties[0].model_copy(update={"rank": 2}), *first.properties[1:],
        ]}),
        *rest,
    ]})
    d = await _drive_generated(
        monkeypatch, _Generator(ranked), run_mode=RunMode.PROPERTY_GENERATION,
    )

    recorded = d.backend.artifact_store.property_set
    assert recorded is not None
    assert recorded.ranks() == {(first.name, first.properties[0].title): 2}
    assert [p.rank for c in recorded.property_components for p in c.properties] == [2, None, None]


async def test_a_property_generation_set_loads_into_a_comprehensive_run():
    assert _generated(run_mode=RunMode.PROPERTY_GENERATION).loadable_in(RunMode.COMPREHENSIVE)
    assert not _generated(run_mode=RunMode.PROPERTY_GENERATION).loadable_in(RunMode.PRIORITIZED)


async def test_a_generated_set_for_another_contract_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="contract"):
        await _drive_generated(monkeypatch, _Generator(_generated(contract_name="Other")))


async def test_a_generated_set_that_does_not_match_the_units_is_rejected(monkeypatch):
    ps = _generated()
    renamed = ps.model_copy(update={"property_components": [
        ps.property_components[0].model_copy(update={"name": "gone"}), *ps.property_components[1:],
    ]})
    with pytest.raises(ValueError, match="gone"):
        await _drive_generated(monkeypatch, _Generator(renamed))


async def test_a_generator_and_a_predefined_set_are_exclusive(monkeypatch):
    with pytest.raises(ValueError, match="not both"):
        await _drive_generated(monkeypatch, _Generator(_generated()), predefined=_generated())


async def test_generated_properties_cannot_be_refined_interactively(monkeypatch):
    with pytest.raises(ValueError, match="interactively"):
        await _drive_generated(monkeypatch, _Generator(_generated()), interactive=True)


# ---------------------------------------------------------------------------
# Selection and loading
# ---------------------------------------------------------------------------


def _install_generators(monkeypatch, *eps: _EntryPoint) -> None:
    def fake_entry_points(*, group: str):
        assert group == PROPERTY_GENERATOR_ENTRY_POINT_GROUP, group
        return list(eps)

    monkeypatch.setattr(gen_mod.importlib.metadata, "entry_points", fake_entry_points)


def _generator_loader(impl: PropertyGeneratorImpl[Any], scope: Any = None):
    class Loader(PropertyGeneratorLoader[Any]):
        @property
        def scope(self) -> Any:
            return scope if scope is not None else AnyEcosystem()

        @asynccontextmanager
        async def initialize(self):
            yield impl

    return Loader


@pytest.mark.parametrize("raw", [None, "", "native", " native "])
async def test_no_name_and_native_select_the_native_extraction(monkeypatch, raw):
    monkeypatch.delenv(PROPERTY_GENERATOR_ENV, raising=False)
    _install_generators(monkeypatch)
    assert resolve_property_generator(raw) is None


async def test_an_installed_generator_resolves_from_the_flag_or_the_environment(monkeypatch):
    _install_generators(monkeypatch, _EntryPoint("propgen", object))
    assert resolve_property_generator("propgen") == "propgen"
    monkeypatch.setenv(PROPERTY_GENERATOR_ENV, "propgen")
    assert resolve_property_generator(None) == "propgen"
    # The flag wins over the environment.
    assert resolve_property_generator("native") is None


async def test_a_generator_that_is_not_installed_fails_resolution(monkeypatch):
    _install_generators(monkeypatch)
    with pytest.raises(ValueError, match="Unknown property generator 'propgen'"):
        resolve_property_generator("propgen")


async def test_loading_yields_the_initialized_generator(monkeypatch):
    impl = _Generator(_generated())
    _install_generators(monkeypatch, _EntryPoint("propgen", _generator_loader(impl)))
    async with load_property_generator("propgen", object) as got:
        assert got is impl
    async with load_property_generator(None, object) as got:
        assert got is None


async def test_loading_rejects_something_that_is_not_a_loader(monkeypatch):
    _install_generators(monkeypatch, _EntryPoint("propgen", object))
    with pytest.raises(RuntimeError, match="not a PropertyGeneratorLoader"):
        async with load_property_generator("propgen", object):
            pass
