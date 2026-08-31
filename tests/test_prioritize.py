"""The prioritized run's decision: how the focus is derived, and the guards around it.

Everything here is LLM-free. The ranker's *judgement* cannot be tested, but the arithmetic that
turns its scores into a focus can be, and so can the validation that stands between a bad
structured-output response and a run that formalizes the wrong thing (or nothing).
"""

import pytest

from composer.spec.prioritize import (
    CRITICAL_MATCH_BONUS, MAX_SUPPORTING, Candidate, PropertyRanking, RankedProperty,
    build_candidates, priority, select, validate_ranking,
)
from composer.spec.types import ComponentName, PropertyFormulation, PropertyTitle


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=PropertyTitle(title), sort="invariant", description=f"d:{title}")


def _cands() -> list[Candidate]:
    return build_candidates([
        (0, ComponentName("Vault"), [_prop("solvency"), _prop("no_free_mint"), _prop("shares_sane")]),
        (1, ComponentName("Fees"), [_prop("fee_bounded")]),
    ])


def _entry(comp, title, score, *, critical=False, deps=()):
    return RankedProperty(
        key=(ComponentName(comp), PropertyTitle(title)),
        score=score, critical_match=critical,
        depends_on=[PropertyTitle(d) for d in deps], rationale="r",
    )


def _ranking(*entries) -> PropertyRanking:
    return PropertyRanking(ranked=list(entries), justification="j")


def _all(**overrides) -> PropertyRanking:
    """Every candidate scored, low by default, with named overrides."""
    base = {
        ("Vault", "solvency"): 40, ("Vault", "no_free_mint"): 30,
        ("Vault", "shares_sane"): 20, ("Fees", "fee_bounded"): 10,
    }
    return _ranking(*[
        overrides.get(f"{c}.{t}") or _entry(c, t, s) for (c, t), s in base.items()
    ])


# --- labels ---------------------------------------------------------------


def test_labels_are_unique_even_against_a_component_named_like_a_suffix():
    # Numbering repeats is not enough: a real component may already be called "Vault (2)", and a
    # second "Vault" numbered by count would collide with it and resolve onto the wrong batch.
    cands = build_candidates([
        (0, ComponentName("Vault"), [_prop("a")]),
        (3, ComponentName("Vault"), [_prop("b")]),
        (7, ComponentName("Vault (2)"), [_prop("c")]),
        (9, ComponentName("Vault"), [_prop("d")]),
    ])
    labels = [c.label for c in cands]
    assert len(set(labels)) == len(labels)
    assert [c.unit_index for c in cands] == [0, 3, 7, 9]


# --- deriving the focus ---------------------------------------------------


def test_the_focus_is_the_highest_priority_entry_wherever_it_sits_in_the_list():
    # The ranking carries no "primary" field, so the artifact and the run cannot disagree: the
    # focus is computed from the same scores the artifact records, in any order.
    cands = _cands()
    r = _all(**{"Fees.fee_bounded": _entry("Fees", "fee_bounded", 90)})
    assert validate_ranking(cands, r) is None
    sel = select(cands, r)
    assert sel.unit_index == 1 and sel.titles == ["fee_bounded"]


def test_a_flagged_concern_wins_at_a_comparable_score():
    cands = _cands()
    r = _all(**{
        "Vault.solvency": _entry("Vault", "solvency", 40),
        "Vault.no_free_mint": _entry("Vault", "no_free_mint", 30, critical=True),
    })
    # 30 + 15 beats 40.
    assert select(cands, r).titles == ["no_free_mint"]


def test_a_flagged_concern_does_not_drag_a_trivial_property_past_a_critical_one():
    cands = _cands()
    r = _all(**{
        "Vault.solvency": _entry("Vault", "solvency", 90),
        "Vault.no_free_mint": _entry("Vault", "no_free_mint", 30, critical=True),
    })
    # The bonus is bounded, so a 60-point gap survives it.
    assert select(cands, r).titles == ["solvency"]
    assert priority(_entry("x", "y", 30, critical=True)) == 30 + CRITICAL_MATCH_BONUS


def test_the_focus_pulls_in_what_the_winner_says_it_rests_on():
    cands = _cands()
    r = _all(**{
        "Vault.solvency": _entry("Vault", "solvency", 90, deps=["shares_sane"]),
    })
    assert validate_ranking(cands, r) is None
    # The primary leads; dependencies follow in the component's own order.
    assert select(cands, r).titles == ["solvency", "shares_sane"]


def test_only_the_winner_s_dependencies_are_pursued():
    cands = _cands()
    r = _all(**{
        "Vault.solvency": _entry("Vault", "solvency", 90),
        "Vault.no_free_mint": _entry("Vault", "no_free_mint", 30, deps=["shares_sane"]),
    })
    assert select(cands, r).titles == ["solvency"]


def test_dependencies_are_deduplicated_and_never_repeat_the_primary():
    cands = _cands()
    r = _all(**{
        "Vault.solvency": _entry(
            "Vault", "solvency", 90, deps=["shares_sane", "shares_sane"],
        ),
    })
    assert select(cands, r).titles == ["solvency", "shares_sane"]


def test_the_batch_is_the_primary_plus_at_most_the_cap():
    props = [_prop(f"p{i}") for i in range(MAX_SUPPORTING + 5)]
    cands = build_candidates([(0, ComponentName("Vault"), props)])
    r = _ranking(
        _entry("Vault", "p0", 90, deps=[p.title for p in props[1:]]),
        *[_entry("Vault", p.title, 10) for p in props[1:]],
    )
    assert validate_ranking(cands, r) is None
    assert len(select(cands, r).titles) == MAX_SUPPORTING + 1


def test_selection_never_yields_an_empty_batch():
    # The driver raises "No properties extracted from any component" on an empty batch list, so the
    # cut must always leave at least the primary standing.
    cands = _cands()
    sel = select(cands, _all())
    assert sel.titles


# --- validation -----------------------------------------------------------


def test_a_dependency_from_another_component_is_rejected():
    # D1: one component's batch survives, so a dependency elsewhere could never be formalized.
    cands = _cands()
    r = _all(**{"Vault.solvency": _entry("Vault", "solvency", 90, deps=["fee_bounded"])})
    problem = validate_ranking(cands, r)
    assert problem is not None and "same component" in problem


def test_a_self_dependency_is_rejected():
    cands = _cands()
    r = _all(**{"Vault.solvency": _entry("Vault", "solvency", 90, deps=["solvency"])})
    problem = validate_ranking(cands, r)
    assert problem is not None and "itself" in problem


def test_a_missing_candidate_is_rejected():
    cands = _cands()
    r = _ranking(_entry("Vault", "solvency", 40))
    problem = validate_ranking(cands, r)
    assert problem is not None and "missing from `ranked`" in problem


def test_a_duplicated_candidate_is_rejected():
    cands = _cands()
    r = _ranking(*_all().ranked, _entry("Vault", "solvency", 40))
    problem = validate_ranking(cands, r)
    assert problem is not None and "more than once" in problem


def test_an_invented_property_is_rejected():
    cands = _cands()
    r = _ranking(*_all().ranked, _entry("Vault", "not_a_real_property", 99))
    problem = validate_ranking(cands, r)
    assert problem is not None and "not in the candidate listing" in problem
