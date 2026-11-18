"""The checked-in Solana property sets (``tests/data/property_sets/``).

Each was recovered from a run's checkpoints, for a run that cost ~$100, and the run that would
notice one had rotted is the one it exists to make cheap. So a change that stops one loading, or
stops its components answering to the units of its own analysis, is caught here.
"""

import pathlib

import pytest

from composer.pipeline.ecosystem import SOLANA
from composer.pipeline.property_set import PropertySet
from composer.pipeline.run_mode import RunMode
from composer.spec.context import SourceCode

SETS = pathlib.Path(__file__).resolve().parent / "data" / "property_sets"


def _load(name: str) -> PropertySet:
    return PropertySet.load(SETS / f"{name}.property_set.json")


def _paired(ps: PropertySet) -> list[tuple[str, int]]:
    """Each recorded component's unit, rebuilt from the set's own analysis."""
    main = SOLANA.locate_main(ps.analyzed(SOLANA.system_model), SourceCode(
        project_root="/nonexistent", relative_path="src/lib.rs", contract_name=ps.contract_name,
        content=None, forbidden_read=None,
    ))
    return [(u.slug, len(props)) for u, props in ps.match_units(list(SOLANA.units(main)))]


def test_the_stake_pool_set_holds_the_run_it_was_taken_from():
    ps = _load("spl-stake-pool")

    assert ps.ecosystem == SOLANA.name and ps.run_mode is RunMode.COMPREHENSIVE
    assert ps.target_commit == "22834f8fee10484e8393a1be7a051035193ed312"
    assert len(ps.property_components) == 10
    assert len(ps.keys()) == 219, "the run this was taken from logged 219 extracted properties"


def test_every_stake_pool_component_answers_to_a_unit_of_its_own_analysis():
    paired = _paired(_load("spl-stake-pool"))

    assert len(paired) == 10
    assert sum(n for _, n in paired) == 219


def test_the_component_a_gate_should_replay_is_present():
    """``pool_initialization`` is component 0 and the wrong default: the normative verification of
    this program has no Initialize rule. ``admin_fee_configuration`` covers ``set_fee``, which it
    does cover. See this directory's README."""
    assert ("admin_fee_configuration", 23) in _paired(_load("spl-stake-pool"))


@pytest.mark.parametrize(
    "name, components",
    [
        ("vault", [
            ("vault_lifecycle_initialization", 9), ("deposits", 9),
            ("withdrawals_fee_sharing", 12),
        ]),
        ("vault-deposits", [("deposits", 9)]),
    ],
)
def test_the_vault_sets_answer_to_units_of_their_own_analysis(name, components):
    assert _paired(_load(name)) == components
