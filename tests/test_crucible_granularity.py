"""Whole-program (global) extraction + per-invariant units — docs/crucible-unit-granularity.md.

Covers the three pieces of the granularity change without the toolchain/LLM:
- the new Solana units (whole-program extraction context + per-invariant unit),
- the SOLANA ecosystem's global-extraction wiring,
- the driver branch that turns one whole-program extraction into one batch per invariant.
"""

import types

import pytest

from composer.spec.solana.model import (
    SolanaApplication,
    SolanaInvariantUnit,
    SolanaProgramInstance,
)
from composer.spec.system_model import FeatureUnit
from composer.spec.types import PropertyFormulation


def _app() -> SolanaApplication:
    return SolanaApplication.model_validate(
        {
            "application_type": "defi",
            "description": "a vault program",
            "components": [
                {
                    "name": "vault",
                    "description": "the vault program",
                    "program_identifier": "vault",
                    "account_types": ["Vault"],
                    "instructions": [
                        {"name": "deposit", "description": "d", "requirements": []},
                        {"name": "withdraw", "description": "w", "requirements": []},
                    ],
                }
            ],
        }
    )


def _inv(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description="desc")


def test_program_instance_is_a_feature_unit_with_whole_program_api():
    main = SolanaProgramInstance(0, _app())
    assert isinstance(main, FeatureUnit)  # runtime_checkable
    assert main.display_name == "vault"
    # feature_json carries the whole-program instruction API (for the test author).
    names = [i["name"] for i in main.feature_json()["instructions"]]
    assert names == ["deposit", "withdraw"]


def test_invariant_unit_maps_title_to_slug_and_carries_program_api():
    main = SolanaProgramInstance(0, _app())
    u = SolanaInvariantUnit(2, main, _inv("total shares == balance"))
    assert isinstance(u, FeatureUnit)
    assert u.display_name == "total shares == balance"
    assert u.slug == "total_shares_balance" or "total" in u.slug  # slugified
    assert u.unit_index == 2
    assert [i["name"] for i in u.feature_json()["instructions"]] == ["deposit", "withdraw"]
    # Distinct invariants → distinct cache material (so formalize caches per invariant).
    assert u.cache_material() != SolanaInvariantUnit(3, main, _inv("other")).cache_material()


def test_solana_ecosystem_uses_global_extraction():
    from composer.pipeline.ecosystem import SOLANA

    main = SolanaProgramInstance(0, _app())
    assert SOLANA.global_extraction is True
    assert SOLANA.collapse_units is True  # one whole-program batch (single harness + run)
    assert SOLANA.extraction_unit is not None and SOLANA.property_unit is not None
    # extraction context is the whole program; property_unit fans an invariant into a unit.
    assert SOLANA.extraction_unit(main) is main
    u = SOLANA.property_unit(main, _inv("x"), 0)
    assert isinstance(u, SolanaInvariantUnit) and u.display_name == "x"


# --- driver branch: one extraction → one batch per invariant ------------------------


class _FakeUnit:
    def __init__(self, name: str, i: int):
        self._n, self._i = name, i

    @property
    def display_name(self) -> str:
        return self._n

    @property
    def slug(self) -> str:
        return self._n

    @property
    def unit_index(self) -> int:
        return self._i

    def cache_material(self) -> str:
        return self._n

    def context_tag(self) -> dict:
        return {}

    def feature_json(self) -> dict:
        return {}


class _ChildCtx:
    async def child(self, key, tag=None):
        return object()


class _Ctx:
    recursion_limit = 100

    def child(self, key):
        return _ChildCtx()


class _Run:
    ctx = _Ctx()
    env = object()

    async def runner(self, info, job):
        return await job(None)  # the extraction job takes a refinement-conv arg


@pytest.mark.asyncio
async def test_global_extraction_fans_out_one_batch_per_invariant(monkeypatch):
    from composer.pipeline import core

    invs = [_inv("inv0"), _inv("inv1"), _inv("inv2")]

    async def fake_rpi(*a, **k):
        return invs

    monkeypatch.setattr(core, "run_property_inference", fake_rpi)

    eco = types.SimpleNamespace(
        global_extraction=True,
        collapse_units=False,  # per-invariant fan-out path
        property_prompts=types.SimpleNamespace(system="s.j2", initial="i.j2"),
        extraction_unit=lambda main: _FakeUnit("program", 0),
        property_unit=lambda main, prop, i: _FakeUnit(prop.title, i),
        units=lambda main: [],
    )

    batches = await core._extract_all(
        main=object(), backend_guidance="", run=_Run(), phase=None,
        interactive=False, threat_model=None, max_rounds=1, ecosystem=eco,
    )

    assert [b.feat.display_name for b in batches] == ["inv0", "inv1", "inv2"]
    # each batch carries exactly its own single invariant
    assert [[p.title for p in b.props] for b in batches] == [["inv0"], ["inv1"], ["inv2"]]


@pytest.mark.asyncio
async def test_collapse_units_makes_one_whole_program_batch(monkeypatch):
    # docs/crucible-unit-granularity.md §3: with collapse_units, global extraction keeps ALL
    # invariants in ONE batch on the whole-program unit (single harness + run).
    from composer.pipeline import core

    invs = [_inv("inv0"), _inv("inv1"), _inv("inv2")]

    async def fake_rpi(*a, **k):
        return invs

    monkeypatch.setattr(core, "run_property_inference", fake_rpi)

    eco = types.SimpleNamespace(
        global_extraction=True,
        collapse_units=True,  # the collapse: one batch, all props
        property_prompts=types.SimpleNamespace(system="s.j2", initial="i.j2"),
        extraction_unit=lambda main: _FakeUnit("program", 0),
        property_unit=lambda main, prop, i: _FakeUnit(prop.title, i),
        units=lambda main: [],
    )

    batches = await core._extract_all(
        main=object(), backend_guidance="", run=_Run(), phase=None,
        interactive=False, threat_model=None, max_rounds=1, ecosystem=eco,
    )

    assert len(batches) == 1
    assert batches[0].feat.display_name == "program"
    assert [p.title for p in batches[0].props] == ["inv0", "inv1", "inv2"]
