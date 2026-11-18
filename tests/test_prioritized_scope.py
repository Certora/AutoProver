"""A prioritized run checks a property at its own component's entry points.

A component's extraction may state a property over other components' entry points too. A
prioritized run formalizes one component, so such a property is narrowed to that component's entry
points, and the author, the judge, the ranker and the report are all told so.
"""

from composer.spec.prioritize import build_candidates, scope_to_unit
from composer.spec.types import (
    ComponentName, PropertyFormulation, PropertyTitle, ScopedPropertyFormulation,
)
from composer.templates.loader import load_jinja_template

WITHDRAWAL = ["withdraw"]
SPLIT_MERGE = ["split", "merge"]
EVERYONE = WITHDRAWAL + SPLIT_MERGE + ["move_lamports"]


def _prop(title: str, description: str) -> PropertyFormulation:
    return PropertyFormulation(title=PropertyTitle(title), sort="invariant", description=description)


FLOOR = _prop("floor", "The balance floor holds after withdraw, split and merge.")
CLOSES = _prop("closes", "A full withdraw closes the account.")
SPLIT_ONLY = _prop("split_only", "split never creates lamports.")


def _flat(text: str) -> str:
    return " ".join(text.split())


# --- scoping ---------------------------------------------------------------


def test_a_property_naming_only_its_own_entry_points_is_kept_as_extracted():
    scoped = scope_to_unit([CLOSES], WITHDRAWAL, EVERYONE)
    assert scoped.props == [CLOSES]
    assert type(scoped.props[0]) is PropertyFormulation
    assert scoped.unreachable == []


def test_a_property_reaching_into_other_units_is_narrowed_to_its_own():
    (floor,) = scope_to_unit([FLOOR], WITHDRAWAL, EVERYONE).props
    assert isinstance(floor, ScopedPropertyFormulation)
    assert floor.checked_at == ["withdraw"]
    assert floor.excluded == ["split", "merge"]
    assert floor.description == FLOOR.description


def test_a_property_naming_none_of_its_own_entry_points_cannot_be_narrowed():
    scoped = scope_to_unit([SPLIT_ONLY, CLOSES], WITHDRAWAL, EVERYONE)
    assert scoped.props == [CLOSES]
    assert scoped.unreachable == [SPLIT_ONLY]


def test_scoping_keeps_the_focus_order():
    scoped = scope_to_unit([FLOOR, CLOSES], WITHDRAWAL, EVERYONE)
    assert [p.title for p in scoped.props] == ["floor", "closes"]


def test_the_scope_changes_the_property_digest():
    # The formalization cache key is built from model_dump_json, so a narrowed focus must not
    # replay a formalization of the unnarrowed property.
    (floor,) = scope_to_unit([FLOOR], WITHDRAWAL, EVERYONE).props
    assert floor.model_dump_json() != FLOOR.model_dump_json()


# --- the ranker --------------------------------------------------------------


def test_candidates_record_what_reaches_into_other_units():
    cands = build_candidates(
        [(0, ComponentName("Withdrawal"), [FLOOR, CLOSES]), (1, ComponentName("Split"), [SPLIT_ONLY])],
        {0: WITHDRAWAL, 1: SPLIT_MERGE},
    )
    assert cands[0].beyond == {"floor": ["merge", "split"]}
    assert cands[1].beyond == {}


def test_without_entry_points_nothing_is_marked():
    (cand,) = build_candidates([(0, ComponentName("Vault"), [FLOOR])])
    assert cand.beyond == {}


def test_the_ranker_sees_the_mark():
    cands = build_candidates(
        [(0, ComponentName("Withdrawal"), [FLOOR, CLOSES])], {0: WITHDRAWAL, 1: SPLIT_MERGE},
    )
    # Unit 1 contributes entry points but no candidate, as a unit whose extraction found nothing.
    listing = _flat(load_jinja_template(
        "property_prioritization_prompt.j2", contract_name="stake", candidates=cands,
    ))
    assert "Also names other components' entry points: merge, split" in listing
    assert listing.count("Also names other components' entry points") == 1


# --- the author and the judge -------------------------------------------------------


def _scoped_floor() -> ScopedPropertyFormulation:
    (floor,) = scope_to_unit([FLOOR], WITHDRAWAL, EVERYONE).props
    assert isinstance(floor, ScopedPropertyFormulation)
    return floor


def _author_prompt(props: list[PropertyFormulation]) -> str:
    from composer.spec.cvlr.author import CvlrPropertyGenParams, _PropertyGenTemplate

    params: CvlrPropertyGenParams = {
        "context": None,
        "properties": props,
        "program": "stake",
        "module": "withdrawal",
        "cvlr_versions": "cvlr 0.6.2",
        "sort": "existing",
        "focused": True,
    }
    return _flat(_PropertyGenTemplate.bind(params).render_to(load_jinja_template))


def _judge_prompt(props: list[PropertyFormulation]) -> str:
    from composer.spec.cvlr.author import _JudgeTemplate

    return _flat(_JudgeTemplate.bind(
        {"properties": props, "context": None, "sort": "existing", "program": "stake"}
    ).render_to(load_jinja_template))


def test_the_author_is_told_the_scope():
    text = _author_prompt([_scoped_floor(), CLOSES])
    assert "this run checks the property at withdraw only" in text
    assert "so split, merge are out of its scope: write no rule for them and record no skip" in text
    assert text.count("Scope:") == 1


def test_the_judge_is_told_the_scope():
    text = _judge_prompt([_scoped_floor(), CLOSES])
    assert "this run checks the property at withdraw only" in text
    assert "Do not ask for rules or skips for them" in text
    assert text.count("Scope:") == 1


def test_an_unscoped_batch_renders_no_scope():
    assert "Scope:" not in _author_prompt([CLOSES])
    assert "Scope:" not in _judge_prompt([CLOSES])
