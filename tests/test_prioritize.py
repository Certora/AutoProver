"""The prioritized run's decision: how the focus is derived, and the guards around it.

Everything here is LLM-free. The ranker's *judgement* cannot be tested, but the arithmetic that
turns its scores into a focus can be, and so can the validation standing between a bad
structured-output response and a run that proves the wrong thing (or nothing).
"""

import pytest

from composer.spec.prioritize import (
    CRITICAL_MATCH_BONUS, Candidate, PropertyGroup, PropertyRanking,
    RankedProperty, build_candidates, priority, select, validate_ranking,
)
from composer.spec.types import ComponentName, PropertyFormulation, PropertyTitle

VAULT = "0: Vault"
FEES = "1: Fees"


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(title=PropertyTitle(title), sort="invariant", description=f"d:{title}")


def _cands() -> list[Candidate]:
    return build_candidates([
        (0, ComponentName("Vault"), [_prop("solvency"), _prop("no_free_mint"), _prop("shares_sane")]),
        (1, ComponentName("Fees"), [_prop("fee_bounded")]),
    ])


def _e(comp, title, score, *, critical=False):
    return RankedProperty(
        key=(ComponentName(comp), PropertyTitle(title)),
        score=score, critical_match=critical, rationale="r",
    )


def _g(claim, *keys):
    return PropertyGroup(claim=claim, members=[(ComponentName(c), PropertyTitle(t)) for c, t in keys])


def _ranking(entries, groups):
    return PropertyRanking(ranked=entries, groups=groups, justification="j")


def _default(**score_overrides):
    """Every candidate scored low, each in its own group, with named score overrides."""
    base = [(VAULT, "solvency"), (VAULT, "no_free_mint"), (VAULT, "shares_sane"), (FEES, "fee_bounded")]
    entries = [_e(c, t, score_overrides.get(t, 10)) for c, t in base]
    return _ranking(entries, [_g(f"claim:{t}", (c, t)) for c, t in base])


# --- labels ---------------------------------------------------------------


def test_labels_carry_the_component_index():
    # Two components can share a display name; the index is the stable id, so it goes in the
    # label rather than an invented suffix.
    cands = build_candidates([
        (0, ComponentName("Vault"), [_prop("a")]),
        (3, ComponentName("Vault"), [_prop("b")]),
    ])
    assert [c.label for c in cands] == ["0: Vault", "3: Vault"]
    assert [c.unit_index for c in cands] == [0, 3]


# --- deriving the focus ---------------------------------------------------


def test_the_focus_is_the_group_holding_the_highest_scoring_property():
    cands = _cands()
    r = _ranking(
        [_e(VAULT, "solvency", 90), _e(VAULT, "no_free_mint", 80),
         _e(VAULT, "shares_sane", 20), _e(FEES, "fee_bounded", 10)],
        [_g("the vault stays solvent", (VAULT, "solvency"), (VAULT, "no_free_mint")),
         _g("shares are sane", (VAULT, "shares_sane")),
         _g("fees are bounded", (FEES, "fee_bounded"))],
    )
    assert validate_ranking(cands, r) is None
    sel = select(cands, r)
    assert sel.unit_index == 0
    assert sel.claim == "the vault stays solvent"
    assert sel.titles == ["solvency", "no_free_mint"]


def test_a_lower_scoring_property_rides_along_in_the_winning_group():
    # The point of grouping: a property that would never win alone is pursued because it is part
    # of the claim that did win.
    cands = _cands()
    r = _ranking(
        [_e(VAULT, "solvency", 90), _e(VAULT, "shares_sane", 1),
         _e(VAULT, "no_free_mint", 80), _e(FEES, "fee_bounded", 10)],
        [_g("the vault stays solvent", (VAULT, "solvency"), (VAULT, "shares_sane")),
         _g("no free mint", (VAULT, "no_free_mint")),
         _g("fees", (FEES, "fee_bounded"))],
    )
    assert validate_ranking(cands, r) is None
    assert select(cands, r).titles == ["solvency", "shares_sane"]


def test_a_flagged_concern_wins_at_a_comparable_score():
    cands = _cands()
    r = _default(solvency=40)
    r.ranked[1] = _e(VAULT, "no_free_mint", 30, critical=True)
    # 30 + 15 beats 40.
    assert select(cands, r).titles == ["no_free_mint"]


def test_a_flagged_concern_does_not_drag_a_trivial_property_past_a_critical_one():
    cands = _cands()
    r = _default(solvency=90)
    r.ranked[1] = _e(VAULT, "no_free_mint", 30, critical=True)
    assert select(cands, r).titles == ["solvency"]
    assert priority(_e(VAULT, "x", 30, critical=True)) == 30 + CRITICAL_MATCH_BONUS


def test_a_large_group_is_pursued_whole():
    # The claim is the unit of work: every property stating it is formalized, however many that
    # is. Dropping the tail would spend most of the budget and leave the claim open.
    props = [_prop(f"p{i}") for i in range(6)]
    cands = build_candidates([(0, ComponentName("Vault"), props)])
    label = "0: Vault"
    entries = [_e(label, p.title, 100 - i) for i, p in enumerate(props)]
    r = _ranking(entries, [_g("everything at once", *[(label, p.title) for p in props])])
    assert validate_ranking(cands, r) is None

    assert select(cands, r).titles == [p.title for p in props]


def test_a_low_scoring_member_of_the_winning_group_is_still_pursued():
    # The shape that motivated dropping the cap: the claim's own conclusion, outscored by the
    # properties it depends on because they carry critical_match. It is in the group, so it is
    # pursued.
    props = [_prop("identity"), _prop("ledger_sums"), _prop("no_over_burn"), _prop("redemption")]
    cands = build_candidates([(0, ComponentName("Vault"), props)])
    label = "0: Vault"
    entries = [
        _e(label, "identity", 100, critical=True),
        _e(label, "ledger_sums", 92, critical=True),
        _e(label, "no_over_burn", 90, critical=True),
        _e(label, "redemption", 95),
    ]
    r = _ranking(entries, [_g("solvency", *[(label, p.title) for p in props])])
    assert validate_ranking(cands, r) is None

    assert set(select(cands, r).titles) == {p.title for p in props}


def test_selection_never_yields_an_empty_batch():
    # The driver raises "No properties extracted from any component" on an empty batch list.
    assert select(_cands(), _default()).titles


# --- validation -----------------------------------------------------------


def test_a_group_spanning_components_is_rejected():
    # A run formalizes one component's batch, so a cross-component claim could not be pursued.
    cands = _cands()
    r = _ranking(
        [_e(VAULT, "solvency", 90), _e(VAULT, "no_free_mint", 10),
         _e(VAULT, "shares_sane", 10), _e(FEES, "fee_bounded", 10)],
        [_g("everything", (VAULT, "solvency"), (FEES, "fee_bounded")),
         _g("rest", (VAULT, "no_free_mint"), (VAULT, "shares_sane"))],
    )
    problem = validate_ranking(cands, r)
    assert problem is not None and "spans components" in problem


def test_a_property_in_no_group_is_rejected():
    cands = _cands()
    r = _default()
    r.groups = r.groups[:-1]
    problem = validate_ranking(cands, r)
    assert problem is not None and "in no group" in problem


def test_a_property_in_two_groups_is_rejected():
    cands = _cands()
    r = _default()
    r.groups.append(_g("again", (VAULT, "solvency")))
    problem = validate_ranking(cands, r)
    assert problem is not None and "more than one group" in problem


def test_a_missing_candidate_is_rejected():
    cands = _cands()
    r = _default()
    r.ranked = r.ranked[:-1]
    problem = validate_ranking(cands, r)
    assert problem is not None and "missing from `ranked`" in problem


def test_an_invented_property_is_rejected():
    cands = _cands()
    r = _default()
    r.ranked.append(_e(VAULT, "not_a_real_property", 99))
    r.groups.append(_g("invented", (VAULT, "not_a_real_property")))
    problem = validate_ranking(cands, r)
    assert problem is not None and "not in the candidate listing" in problem


def test_every_problem_is_reported_at_once():
    # One error per attempt would run out of attempts before it ran out of mistakes.
    cands = _cands()
    r = _default()
    r.ranked = r.ranked[:-1]          # a candidate missing from `ranked`
    r.groups = r.groups[:-1]          # and the same one in no group
    r.ranked.append(_e(VAULT, "invented", 50))
    r.groups.append(_g("invented", (VAULT, "invented")))
    problem = validate_ranking(cands, r)
    assert problem is not None
    assert "not in the candidate listing" in problem
    assert "missing from `ranked`" in problem
    assert "in no group" in problem


# --- the retry -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_retry_shows_the_model_what_it_produced():
    """Structured output hands back a parsed object and leaves no assistant turn behind, so
    without replaying it the model is asked to fix a ranking it has no memory of making."""
    import composer.spec.prioritize as prioritize

    cands = _cands()
    bad = _default()
    bad.ranked = bad.ranked[:-1]          # a candidate missing from `ranked`
    good = _default()
    seen: list[list] = []

    class _Bound:
        async def ainvoke(self, messages):
            seen.append(list(messages))
            return bad if len(seen) == 1 else good

    class _LLM:
        def with_structured_output(self, _ty): return _Bound()

    out = await prioritize.rank_properties(
        llm=_LLM(), contract_name="Vault", candidates=cands,  # type: ignore[arg-type]
        design_doc=None, threat_model=None, extra_context=[],
    )
    assert out is good
    assert len(seen) == 2, "the invalid ranking should have been retried"

    replayed = "".join(str(m.content) for m in seen[1])
    assert "solvency" in replayed and "missing from `ranked`" in replayed
