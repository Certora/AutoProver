"""The run's property set: written by every run, and read back in place of system analysis and
property extraction (``--predefined-properties``).

The contract under test is the round trip — feeding a run's own property set back yields the
formalization input (units + properties) and report input that run had — plus the stop that ends
a run once its property set is written (``--stop-after-properties``).

Stubs throughout — no LLM, no DB, no backend wheel.
"""

import asyncio
import dataclasses
from pathlib import Path

import pytest
from pydantic import BaseModel

import composer.pipeline.core as core
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import EVM
from composer.pipeline.property_set import PropertySet, PropertySetComponent
from composer.pipeline.run_mode import RunMode
from composer.spec.system_model import (
    ComponentInteraction, ContractComponent, ExternalDependency, SourceApplication,
    SourceExplicitContract, SourceExternalActor,
)

from tests.test_pipeline_staged_formalizer import (
    _Backend, _Cache, _Env, _FeatCtx, _Formalizer, _Prepared, _Run, _Store, _Unit, _prop,
)

pytestmark = pytest.mark.asyncio

UNITS = {"deposits": ["a", "b"], "admin": ["c"]}


class _Model(BaseModel):
    """Stands in for the ecosystem's analyzed model; only its JSON round trip matters."""
    application_type: str
    components: list[str]


ANALYZED = _Model(application_type="vault", components=list(UNITS))


class _RecStore(_Store):
    def __init__(self):
        self.property_set: PropertySet | None = None

    def write_property_set(self, property_set, **_kw):
        self.property_set = property_set
        return "property_set.json"


class _ReportingFormalizer(_Formalizer):
    """Enough formalizer for the driver to reach the report call."""

    async def source_edits(self, _outcomes, _run): return []


class _RecPrepared(_Prepared):
    def __init__(self, formalizer):
        super().__init__(formalizer)
        self.prepared_formalization = False

    async def prepare_formalization(self, run):
        self.prepared_formalization = True
        return await super().prepare_formalization(run)


class _RecBackend(_Backend):
    def __init__(self, prepared):
        super().__init__(prepared)
        self.artifact_store = _RecStore()
        self.analyzed: object = None

    async def prepare_system(self, analyzed, run, preflight):
        self.analyzed = analyzed
        return await super().prepare_system(analyzed, run, preflight)


class _ReportingEnv(_Env):
    def llm_lite(self): return None


class _CachingRun(_Run):
    """A run whose root context opens awaitable children, as the property-set loader needs,
    and whose env reaches the report call."""
    ctx = _Cache()  # type: ignore[assignment]
    env = _ReportingEnv()  # type: ignore[assignment]


def _units() -> list[_Unit]:
    return [_Unit(name, i) for i, name in enumerate(UNITS)]


#: EVM, but enumerating stub units and validating the stub model.
ECOSYSTEM = dataclasses.replace(EVM, system_model=_Model, units=lambda _main: _units())  # type: ignore[arg-type]


class _Drive:
    def __init__(self):
        self.formalizer = _ReportingFormalizer()
        self.prepared = _RecPrepared(self.formalizer)
        self.backend = _RecBackend(self.prepared)
        self.report_components: list | None = None
        self.result = None


async def _drive(
    monkeypatch, *,
    predefined: PropertySet | None = None,
    stop_after_properties: bool = False,
    run_mode: RunMode = RunMode.COMPREHENSIVE,
    interactive: bool = False,
) -> _Drive:
    d = _Drive()

    async def fake_analysis(*_a, **_kw): return ANALYZED

    async def fake_extract_all(*_a, **_kw):
        return core.ExtractionResult(
            [
                core._Batch(u, [_prop(t) for t in UNITS[u.display_name]], _FeatCtx())  # type: ignore[arg-type]
                for u in _units()
            ],
            [],
        )

    async def fake_report(*_a, components, **_kw):
        d.report_components = components
        return object()

    async def never(*_a, **_kw):
        raise AssertionError("a predefined property set must not reach analysis or extraction")

    async def no_ranking(**_kw):
        raise AssertionError("a predefined property set must not be re-ranked")

    if predefined is None:
        monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
        monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    else:
        monkeypatch.setattr(core, "run_component_analysis", never)
        monkeypatch.setattr(core, "_extract_all", never)
        monkeypatch.setattr(core, "rank_properties", no_ranking)
    monkeypatch.setattr(core, "build_report", fake_report)

    run = _CachingRun()
    run.run_mode = run_mode  # type: ignore[misc]
    d.result = await run_pipeline(
        d.backend, run, max_bug_rounds=1, ecosystem=ECOSYSTEM,  # type: ignore[arg-type]
        interactive=interactive, predefined=predefined,
        stop_after_properties=stop_after_properties,
    )
    return d


def _report_input(d: _Drive) -> list[tuple[str, list[str]]]:
    assert d.report_components is not None
    return [(c.name, [p.title for p in c.props]) for c in d.report_components]


async def _recorded(monkeypatch, tmp_path: Path) -> tuple[_Drive, PropertySet]:
    """A normal run, and its property set as read back from disk."""
    d = await _drive(monkeypatch)
    written = d.backend.artifact_store.property_set
    assert written is not None
    path = tmp_path / "property_set.json"
    path.write_text(written.model_dump_json())
    return d, PropertySet.load(path)


async def test_a_normal_run_records_what_it_formalizes(monkeypatch, tmp_path):
    d, ps = await _recorded(monkeypatch, tmp_path)

    assert ps.ecosystem == "evm"
    assert ps.contract_name == "Vault"
    assert ps.run_mode is RunMode.COMPREHENSIVE
    assert ps.system_model == ANALYZED.model_dump(mode="json")
    assert [(c.name, c.unit_index, [p.title for p in c.properties]) for c in ps.property_components] == [
        ("deposits", 0, ["a", "b"]), ("admin", 1, ["c"]),
    ]
    assert sorted(d.formalizer.calls) == [("formalize:admin", ["c"]), ("formalize:deposits", ["a", "b"])]


async def test_a_recorded_set_reproduces_the_run_s_formalization_and_report_input(monkeypatch, tmp_path):
    original, ps = await _recorded(monkeypatch, tmp_path)

    replay = await _drive(monkeypatch, predefined=ps)

    assert replay.backend.analyzed == ANALYZED
    assert sorted(replay.formalizer.calls) == sorted(original.formalizer.calls)
    assert _report_input(replay) == _report_input(original)
    # The replay records the same set again, so a set survives any number of round trips.
    assert replay.backend.artifact_store.property_set == ps


async def test_a_prioritized_set_is_not_re_ranked(monkeypatch, tmp_path):
    _, ps = await _recorded(monkeypatch, tmp_path)
    # What a prioritized run records: its single focus batch.
    focus = ps.model_copy(update={
        "run_mode": RunMode.PRIORITIZED, "property_components": ps.property_components[:1],
    })

    replay = await _drive(monkeypatch, predefined=focus, run_mode=RunMode.PRIORITIZED)

    assert replay.formalizer.calls == [("formalize:deposits", ["a", "b"])]


@pytest.mark.parametrize(
    "written, loaded",
    [
        (RunMode.COMPREHENSIVE, RunMode.PRIORITIZED),
        (RunMode.PRIORITIZED, RunMode.COMPREHENSIVE),
    ],
    ids=["comprehensive-into-prioritized", "prioritized-into-comprehensive"],
)
async def test_a_set_from_another_run_mode_is_rejected(monkeypatch, tmp_path, written, loaded):
    _, ps = await _recorded(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match=f"written by a {written.value} run"):
        await _drive(
            monkeypatch, predefined=ps.model_copy(update={"run_mode": written}), run_mode=loaded,
        )


@pytest.mark.parametrize("predefined", [False, True], ids=["inferred", "predefined"])
async def test_stop_after_properties_ends_the_run_once_the_set_is_written(
    monkeypatch, tmp_path, predefined: bool,
):
    ps = (await _recorded(monkeypatch, tmp_path))[1] if predefined else None

    d = await _drive(monkeypatch, predefined=ps, stop_after_properties=True)

    assert d.backend.artifact_store.property_set is not None
    assert not d.prepared.prepared_formalization
    assert d.formalizer.calls == []
    assert d.report_components is None
    assert d.result is not None
    assert (d.result.n_components, d.result.n_properties) == (2, 3)
    assert d.result.failures == [] and not d.result.all_failed


async def test_a_predefined_set_cannot_be_refined_interactively(monkeypatch, tmp_path):
    _, ps = await _recorded(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="interactively"):
        await _drive(monkeypatch, predefined=ps, interactive=True)


async def test_a_predefined_set_for_another_contract_is_rejected(monkeypatch, tmp_path):
    _, ps = await _recorded(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="contract"):
        await _drive(monkeypatch, predefined=ps.model_copy(update={"contract_name": "Other"}))


async def test_a_set_that_does_not_match_the_units_fails_before_the_setup_starts(monkeypatch, tmp_path):
    _, ps = await _recorded(monkeypatch, tmp_path)
    renamed = ps.model_copy(update={"property_components": [
        ps.property_components[0].model_copy(update={"name": "gone"}), *ps.property_components[1:],
    ]})
    prepared: list[_RecPrepared] = []
    original_init = _RecPrepared.__init__

    def tracking_init(self, formalizer):
        original_init(self, formalizer)
        prepared.append(self)

    monkeypatch.setattr(_RecPrepared, "__init__", tracking_init)
    with pytest.raises(ValueError, match="'gone' matches 0 units"):
        await _drive(monkeypatch, predefined=renamed)
    await asyncio.sleep(0.01)  # an orphaned setup task would get to run here
    assert prepared and not prepared[0].prepared_formalization


def _set(*components: tuple[str, int]) -> PropertySet:
    return PropertySet(
        ecosystem="evm", contract_name="Vault", run_mode=RunMode.COMPREHENSIVE, system_model={},
        property_components=[
            PropertySetComponent(name=n, unit_index=i, properties=[_prop("p")])
            for n, i in components
        ],
    )


def test_match_units_pairs_in_file_order():
    units = _units()
    paired = _set(("admin", 1), ("deposits", 0)).match_units(units)  # type: ignore[arg-type]
    assert [u.display_name for u, _ in paired] == ["admin", "deposits"]


@pytest.mark.parametrize(
    "components, units, match",
    [
        ([("gone", 0)], _units(), "matches 0 units"),
        ([("admin", 0)], _units(), "unit index"),
        ([("admin", 1)], [_Unit("admin", 1), _Unit("admin", 2)], "matches 2 units"),
    ],
    ids=["missing", "wrong-index", "ambiguous"],
)
def test_match_units_rejects_any_disagreement(components, units, match):
    with pytest.raises(ValueError, match=match):
        _set(*components).match_units(units)


def test_the_evm_system_model_survives_the_round_trip_byte_for_byte():
    # The per-unit cache key hashes ``app.model_dump_json()``, and the model's unions carry no
    # discriminator — so a reloaded model must not just compare equal, it must serialize the same.
    app = SourceApplication(
        application_type="vault", description="d",
        components=[
            SourceExplicitContract(
                sort="singleton", name="Vault", solidity_identifier="Vault", description="d",
                path="src/Vault.sol",
                components=[ContractComponent(
                    name="deposits", description="d", external_entry_points=["deposit()"],
                    state_variables=["uint total"], requirements=["r"],
                    interactions=[
                        ComponentInteraction(contract_name="Vault", component=None, description="d"),
                        ExternalDependency(external_actor="Owner", description="d"),
                    ],
                )],
            ),
            SourceExternalActor(name="Owner", description="d", assumptions=[], path=None),
        ],
    )
    ps = PropertySet(
        ecosystem="evm", contract_name="Vault", run_mode=RunMode.COMPREHENSIVE,
        system_model=app.model_dump(mode="json"), property_components=[],
    )

    reloaded = PropertySet.model_validate_json(ps.model_dump_json()).analyzed(SourceApplication)

    assert reloaded.model_dump_json() == app.model_dump_json()
    assert [type(c) for c in reloaded.components] == [SourceExplicitContract, SourceExternalActor]


def test_a_malformed_file_is_reported_with_its_path(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": 2}')
    with pytest.raises(ValueError, match="bad.json"):
        PropertySet.load(path)
