"""The prioritized run's decision: ranking validation and the cut it makes.

Everything here is LLM-free — the ranker's *judgement* cannot be tested, but the guards
around it can, and those are what stand between a bad structured-output response and a run
that formalizes the wrong thing (or nothing).
"""

import pytest

from composer.spec.prioritize import (
    MAX_SUPPORTING, Candidate, PropertyRanking, RankedProperty, build_candidates, select,
    validate_ranking,
)
from composer.spec.types import ComponentName, PropertyFormulation, PropertyTitle


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=PropertyTitle(title), sort="invariant", description=f"d:{title}")


def _cands() -> list[Candidate]:
    return build_candidates([
        (0, ComponentName("Vault"), [_prop("solvency"), _prop("no_free_mint"), _prop("shares_sane")]),
        (1, ComponentName("Fees"), [_prop("fee_bounded")]),
    ])


def _ranking(
    order: list[tuple[str, str]],
    primary: tuple[str, str],
    supporting: list[tuple[str, str]],
) -> PropertyRanking:
    return PropertyRanking(
        ranked=[
            RankedProperty(
                key=(ComponentName(c), PropertyTitle(t)),
                score=100 - i, critical_match=False, rationale="r",
            )
            for i, (c, t) in enumerate(order)
        ],
        primary=(ComponentName(primary[0]), PropertyTitle(primary[1])),
        supporting=[(ComponentName(c), PropertyTitle(t)) for c, t in supporting],
        justification="j",
    )


ALL = [("Vault", "solvency"), ("Vault", "no_free_mint"), ("Vault", "shares_sane"), ("Fees", "fee_bounded")]


def test_components_sharing_a_name_get_distinct_labels():
    # Component display names come from the analysis LLM under no uniqueness constraint, so
    # resolving a ranking by name alone could land on the wrong component's batch.
    cands = build_candidates([
        (0, ComponentName("Vault"), [_prop("a")]),
        (3, ComponentName("Vault"), [_prop("b")]),
        (7, ComponentName("Fees"), [_prop("c")]),
    ])
    assert [c.label for c in cands] == ["Vault", "Vault (2)", "Fees"]
    assert [c.unit_index for c in cands] == [0, 3, 7]


def test_a_well_formed_ranking_validates_and_selects_the_focus():
    cands = _cands()
    r = _ranking(ALL, ("Vault", "solvency"), [("Vault", "shares_sane")])
    assert validate_ranking(cands, r) is None

    sel = select(cands, r)
    assert sel.unit_index == 0
    # The primary leads; the rest follow in the component's own order.
    assert sel.titles == ["solvency", "shares_sane"]


def test_supporting_is_deduplicated_and_never_repeats_the_primary():
    cands = _cands()
    r = _ranking(
        ALL, ("Vault", "solvency"),
        [("Vault", "shares_sane"), ("Vault", "shares_sane"), ("Vault", "solvency")],
    )
    assert validate_ranking(cands, r) is None
    assert select(cands, r).titles == ["solvency", "shares_sane"]


def test_supporting_is_capped():
    props = [_prop(f"p{i}") for i in range(MAX_SUPPORTING + 5)]
    cands = build_candidates([(0, ComponentName("Vault"), props)])
    order = [("Vault", p.title) for p in props]
    r = _ranking(order, ("Vault", "p0"), [("Vault", p.title) for p in props[1:]])
    assert validate_ranking(cands, r) is None
    # The cap bounds the *supporting* cluster; the primary is always kept on top of it.
    assert len(select(cands, r).titles) == MAX_SUPPORTING + 1


def test_supporting_from_another_component_is_rejected():
    # D1: one component's batch survives. A supporting property elsewhere cannot ride along,
    # because nothing would formalize it.
    cands = _cands()
    r = _ranking(ALL, ("Vault", "solvency"), [("Fees", "fee_bounded")])
    problem = validate_ranking(cands, r)
    assert problem is not None and "different component" in problem


def test_a_missing_candidate_is_rejected():
    cands = _cands()
    r = _ranking(ALL[:-1], ("Vault", "solvency"), [])
    problem = validate_ranking(cands, r)
    assert problem is not None and "missing from `ranked`" in problem


def test_a_duplicated_candidate_is_rejected():
    cands = _cands()
    r = _ranking(ALL + [("Vault", "solvency")], ("Vault", "solvency"), [])
    problem = validate_ranking(cands, r)
    assert problem is not None and "more than once" in problem


def test_an_invented_property_is_rejected():
    cands = _cands()
    r = _ranking(ALL + [("Vault", "not_a_real_property")], ("Vault", "solvency"), [])
    problem = validate_ranking(cands, r)
    assert problem is not None and "not in the candidate listing" in problem


def test_an_unresolvable_primary_is_rejected():
    cands = _cands()
    r = _ranking(ALL, ("Fees", "solvency"), [])
    problem = validate_ranking(cands, r)
    assert problem is not None and "primary" in problem


def test_selection_never_yields_an_empty_batch():
    # The driver raises "No properties extracted from any component" on an empty batch list, so
    # the cut must always leave at least the primary standing.
    cands = _cands()
    r = _ranking(ALL, ("Fees", "fee_bounded"), [])
    assert validate_ranking(cands, r) is None
    sel = select(cands, r)
    assert sel.unit_index == 1 and sel.titles == ["fee_bounded"]
