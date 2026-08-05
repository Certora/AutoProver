"""What the host hands ``validate``, and what it does with the answer.

A report row is a *unit*; the thing the host actually runs is a **target**, and several units can
share one (Crucible checks a component's whole property set in a single fuzz run). The host owns
that grouping — it decides what runs and in what order — so it passes each target together with the
rows that target covers, rather than leaving the wheel to recover them by re-deriving its own
``units()`` and filtering by name.

Stubbed authoring; the wheel here is a plain object recording what it was called with.
"""

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from composer.rustapp import adapter
from composer.rustapp.adapter import RustFormalizer
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.wire import Target, Unit
from composer.spec.source.report.schema import Outcome
from composer.spec.types import PropertyFormulation

SPEC = "fn c_farms(f: &mut Fixture) {}"

#: Two properties checked by one shared target, and a third that is its own.
UNITS = [
    Unit(property="stake matches", unit="c_stake", target="c_farms"),
    Unit(property="no double stake", unit="c_dbl", target="c_farms"),
    Unit(property="fees capped", unit="c_fees"),
]


@dataclass
class _Wheel:
    """The callouts ``formalize`` reaches. ``validate`` answers GOOD for every covered unit."""

    #: Every ``Target`` the host passed, in call order.
    targets: list[Target] = field(default_factory=list)

    def units(self, _input_json: str) -> str:
        return json.dumps([u.model_dump() for u in UNITS])

    def judge_prompt(self, _input_json: str, _spec: str) -> str | None:
        return None  # no judge, so no review machinery is bound

    def validate(
        self, _input_json: str, _spec: str, target_json: str, _workdir: str, _sandbox: str
    ) -> str:
        target = Target.model_validate_json(target_json)
        self.targets.append(target)
        return json.dumps({
            "kind": "verdicts",
            "verdicts": [[u.unit, {"outcome": "GOOD"}] for u in target.units],
        })


@dataclass
class _Feat:
    display_name: str = "Farms"
    slug: str = "farms"

    def feature_json(self) -> dict[str, Any]:
        return {"name": self.display_name, "slug": self.slug}


@dataclass
class _Source:
    project_root: str
    contract_name: str = "lending"


@dataclass
class _Ctx:
    recursion_limit: int = 10


@dataclass
class _Run:
    source: _Source
    ctx: _Ctx
    env: None = None


def _descriptor() -> AppDescriptor:
    return AppDescriptor.model_validate({
        "name": "demoprover", "header_text": "h", "backend_tag": "prover",
        "backend_guidance": "g", "analysis_key": "k",
        "phases": [
            {"key": "analysis", "label": "A", "role": "analysis"},
            {"key": "extraction", "label": "E", "role": "extraction"},
            {"key": "formalization", "label": "F", "role": "formalization"},
            {"key": "report", "label": "R", "role": "report"},
        ],
        "artifact_layout": {
            "deliverable_dir": "d", "internal_dir": "i", "report_dir": "r", "artifact_dir": "a",
            "artifact_prefix": "p", "artifact_extension": "rs", "property_suffix": "s",
        },
    })


async def _formalize(monkeypatch, tmp_path: pathlib.Path, wheel: _Wheel):
    """Run one component through the loop with the authoring turn stubbed."""
    async def fake_author_turn(*_a, **_kw) -> str:
        return SPEC

    monkeypatch.setattr(adapter, "_author_turn", fake_author_turn)
    formalizer = RustFormalizer(cast(Any, wheel), _descriptor())
    run = _Run(_Source(str(tmp_path)), _Ctx())
    props = [
        PropertyFormulation(title=u.property, sort="invariant", description="d") for u in UNITS
    ]
    return await formalizer.formalize("Farms", cast(Any, _Feat()), props, cast(Any, _Ctx()),
                                      cast(Any, run))


@pytest.mark.asyncio
async def test_each_distinct_target_runs_once_carrying_the_rows_it_covers(monkeypatch, tmp_path):
    wheel = _Wheel()
    await _formalize(monkeypatch, tmp_path, wheel)

    # One run per DISTINCT target, not per row — the two properties sharing `c_farms` are one run.
    assert [t.name for t in wheel.targets] == ["c_farms", "c_fees"]
    # …and each run is told exactly which rows it owes a verdict for, so the wheel never re-derives
    # the grouping the host just computed.
    assert [[u.unit for u in t.units] for t in wheel.targets] == [["c_stake", "c_dbl"], ["c_fees"]]
    # The rows keep their property titles, which is what a backend attributes a counterexample by.
    assert wheel.targets[0].units[0].property == "stake matches"


@pytest.mark.asyncio
async def test_every_covered_row_gets_its_verdict_recorded(monkeypatch, tmp_path):
    # A shared target answers for several rows in one call; all of them must reach the result, or
    # the report silently loses the rows that shared a run.
    result = await _formalize(monkeypatch, tmp_path, _Wheel())

    assert isinstance(result, adapter.RustFormalResult)
    assert set(result.verdicts) == {"c_stake", "c_dbl", "c_fees"}
    assert all(v.outcome is Outcome.GOOD for v in result.verdicts.values())
    # Grouped by property for the report's property→units map.
    assert dict(result.units) == {
        "stake matches": ["c_stake"], "no double stake": ["c_dbl"], "fees capped": ["c_fees"],
    }
