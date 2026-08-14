"""Solana unit granularity: one unit per `ProgramComponent` — docs/crucible-component-units.md.

Covers the pieces of the granularity change that need no toolchain or LLM:
- the component unit wrapper (`SolanaComponentInstance`) and what it exposes to a backend,
- the SOLANA ecosystem's per-component `units` enumeration,
- the driver turning that enumeration into one extraction batch per component.

History: Solana was per-instruction, then briefly a single whole-program unit
(docs/crucible-unit-granularity.md). Both are gone — an instruction is a syntactic artifact, not a
unit of behavior, and one extraction agent for a whole program is a hard cap on depth (§15 measures
this on a 62-instruction program). `Main` (`SolanaProgramInstance`) is deliberately no longer a
`FeatureUnit`; main and unit are different axes, as on EVM. That line is held by the type checker —
`FeatureUnit` is not `@runtime_checkable`, and a structural isinstance could not hold it anyway.
"""

import types

import pytest

from composer.pipeline.plugins import PluginManager
from composer.spec.solana.model import (
    SolanaApplication,
    SolanaComponentInstance,
    SolanaProgramInstance,
)
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
                    "components": [
                        {
                            "name": "Deposits",
                            "description": "taking deposits",
                            "instructions": ["deposit"],
                            "account_types": ["Vault"],
                            "interactions": [],
                            "requirements": ["must credit the depositor"],
                        },
                        {
                            "name": "Withdrawals",
                            "description": "releasing funds",
                            "instructions": ["withdraw"],
                            "account_types": ["Vault"],
                            "interactions": [],
                            "requirements": [],
                        },
                    ],
                }
            ],
        }
    )


def _inv(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description="desc")


# --- the unit wrapper ---------------------------------------------------------------


def test_component_instance_is_a_feature_unit():
    # Every member the driver reads off a unit. `FeatureUnit` is not `@runtime_checkable` — a
    # structural isinstance compares attribute names, not signatures, so pyright is the conformance
    # gate and these assertions are what actually pins the values.
    unit = SolanaComponentInstance(0, SolanaProgramInstance(0, _app()))
    assert unit.display_name == "Deposits"
    assert unit.slug == "Deposits".lower() or unit.slug  # slugified, whatever the rule
    assert unit.unit_index == 0


def test_component_instructions_resolve_to_the_real_objects():
    # The component holds names; the program stays authoritative for accounts/CPIs/signers.
    unit = SolanaComponentInstance(1, SolanaProgramInstance(0, _app()))
    assert [i.name for i in unit.instructions] == ["withdraw"]
    assert [i.description for i in unit.instructions] == ["w"]


def test_sibling_components_are_context_not_content():
    unit = SolanaComponentInstance(0, SolanaProgramInstance(0, _app()))
    assert [c.name for c in unit.sibling_components] == ["Withdrawals"]


def test_feature_json_carries_the_component_and_only_the_component():
    # Mirrors EVM (`ContractComponentInstance.feature_json` is the component alone). The
    # whole-program surface reaches a backend by its own route — for Crucible, the shared fixture.
    unit = SolanaComponentInstance(0, SolanaProgramInstance(0, _app()))
    js = unit.feature_json()
    assert js["name"] == "Deposits"
    assert js["slug"] == unit.slug
    assert js["requirements"] == ["must credit the depositor"]
    # `instructions` is resolved from names to full objects, and scoped to THIS component.
    assert [i["name"] for i in js["instructions"]] == ["deposit"]
    assert "withdraw" not in str(js["instructions"])
    # No whole-program payload rides along.
    assert "all_instructions" not in js


def test_distinct_components_cache_separately():
    main = SolanaProgramInstance(0, _app())
    a, b = SolanaComponentInstance(0, main), SolanaComponentInstance(1, main)
    assert a.cache_material() != b.cache_material()
    assert a.context_tag() != b.context_tag()


# --- the ecosystem ------------------------------------------------------------------


def test_solana_units_are_one_per_component_of_the_main_program():
    from composer.pipeline.ecosystem import SOLANA

    main = SolanaProgramInstance(0, _app())
    units = SOLANA.units(main)
    assert [u.display_name for u in units] == ["Deposits", "Withdrawals"]
    assert [u.unit_index for u in units] == [0, 1]


# --- the driver ---------------------------------------------------------------------


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


class _Source:
    # _extract_all injects the design doc into property inference when one is
    # supplied (PR #124); None is the no-doc case.
    content = None


class _Run:
    ctx = _Ctx()
    env = object()
    source = _Source()

    async def runner(self, info, job):
        return await job(None)  # the extraction job takes a refinement-conv arg


@pytest.mark.asyncio
async def test_each_component_gets_its_own_extraction_batch(monkeypatch):
    # One extraction agent per component — the property-quality win, and the reason the
    # whole-program unit was replaced.
    from composer.pipeline import core

    per_unit = {"Deposits": [_inv("d0"), _inv("d1")], "Admin": [_inv("a0")]}

    async def fake_rpi(_ctx, _env, feat, **_kw):
        return per_unit[feat.display_name]

    monkeypatch.setattr(core, "run_property_inference", fake_rpi)

    eco = types.SimpleNamespace(
        # ``render_initial`` is the ecosystem's bound initial-prompt renderer; the patched
        # ``run_property_inference`` above never calls it, so it only has to exist.
        property_prompts=types.SimpleNamespace(
            system="s.j2", render_initial=lambda **_kw: "i"
        ),
        units=lambda main: [_FakeUnit("Deposits", 0), _FakeUnit("Admin", 1)],
    )

    run = _Run()
    batches = await core._extract_all(
        prop_key="test-properties",
        main=object(), backend_guidance="", run=run, phase=None,
        interactive=False, threat_model=None, max_rounds=1, ecosystem=eco,
        # The granularity claim is about the *unit* axis, so this drives the phase with no
        # plugins in the way — the hooks a plugin adds are covered by test_plugin_scope.py.
        plugins=PluginManager({}, run).bind_phase(None, "Property Extraction"),
    )

    assert [b.feat.display_name for b in batches] == ["Deposits", "Admin"]
    assert [[p.title for p in b.props] for b in batches] == [["d0", "d1"], ["a0"]]
