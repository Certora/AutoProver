"""Hoisting protocol-wide properties into one shared unit (``docs/cvlr-design-properties-plan.md``,
step 3).

Once a component may state a property over its siblings' instructions, several components state
the same protocol-wide claim: on the stake program, eleven properties stated some form of solvency.
The hoist keeps one of each claim's properties, verbatim, for a shared unit. These tests pin what
the model's plan must satisfy before anything acts on it, and the shared unit the Solana side builds.
"""

import pytest

from composer.spec import hoist
from composer.spec.solana.model import (
    PROTOCOL_UNIT_NAME,
    SolanaApplication,
    SolanaProgramInstance,
    protocol_unit,
)
from composer.spec.types import PropertyFormulation


def _prop(title: str, description: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description=description)


SOLVENT_EVERYWHERE = _prop(
    "solvent", "Every handler (`split`, `merge`, `withdraw`) leaves each account solvent."
)
SOLVENT_ON_WITHDRAW = _prop("withdraw_solvent", "`withdraw` leaves the account solvent.")
SPLIT_LOCAL = _prop("split_divides", "`split` divides the stake in proportion to the lamports.")
WITHDRAW_LOCAL = _prop("withdraw_signer", "`withdraw` requires the withdrawer to sign.")

CANDIDATES = hoist.build_candidates(
    [
        (0, "Split & Merge", ["split", "merge"], [SOLVENT_EVERYWHERE, SPLIT_LOCAL]),
        (1, "Withdrawal", ["withdraw"], [SOLVENT_ON_WITHDRAW, WITHDRAW_LOCAL]),
    ]
)
SPLIT = "0: Split & Merge"
WITHDRAWAL = "1: Withdrawal"


def _plan(*groups: tuple[tuple[str, str] | PropertyFormulation, list[tuple[str, str]]]) -> hoist.HoistPlan:
    """Each group is (shared, members): a key chooses that member, a property synthesizes it."""
    return hoist.HoistPlan(
        groups=[
            hoist.HoistGroup(
                claim="claim",
                shared=(
                    hoist.Synthesized(property=shared)
                    if isinstance(shared, PropertyFormulation)
                    else hoist.Chosen(key=shared)
                ),
                members=members,
            )
            for shared, members in groups
        ],
        justification="j",
    )


SOLVENCY = _plan(
    ((SPLIT, "solvent"), [(SPLIT, "solvent"), (WITHDRAWAL, "withdraw_solvent")])
)


def test_a_plan_that_groups_one_claim_validates():
    assert hoist.validate_plan(CANDIDATES, SOLVENCY) is None


def test_applying_a_plan_keeps_the_representative_verbatim_and_drops_the_rest_of_its_group():
    applied = hoist.apply_plan(CANDIDATES, SOLVENCY)
    assert applied.shared == [SOLVENT_EVERYWHERE]
    assert applied.kept == {0: [SPLIT_LOCAL], 1: [WITHDRAW_LOCAL]}
    assert ((WITHDRAWAL, "withdraw_solvent"), "solvent") in applied.absorbed
    assert applied.synthesized == []


def test_an_empty_plan_leaves_every_unit_as_extracted():
    applied = hoist.apply_plan(CANDIDATES, _plan())
    assert applied.shared == []
    assert applied.kept == {0: [SOLVENT_EVERYWHERE, SPLIT_LOCAL], 1: [SOLVENT_ON_WITHDRAW, WITHDRAW_LOCAL]}


def test_a_representative_confined_to_its_own_component_is_rejected():
    """Hoisting a unit-local property moves it out of the unit that can best check it, and gains
    nothing: no other unit states it."""
    plan = _plan(((SPLIT, "split_divides"), [(SPLIT, "split_divides")]))
    problem = hoist.validate_plan(CANDIDATES, plan)
    assert problem is not None and "names no entry point outside its own component" in problem



@pytest.mark.parametrize(
    "plan, complaint",
    [
        (_plan(((SPLIT, "nope"), [(SPLIT, "nope")])), "not in the candidate listing"),
        (
            _plan(
                ((SPLIT, "solvent"), [(SPLIT, "solvent"), (WITHDRAWAL, "withdraw_solvent")]),
                ((SPLIT, "solvent"), [(SPLIT, "solvent")]),
            ),
            "in more than one group",
        ),
        (
            _plan(((SPLIT, "solvent"), [(WITHDRAWAL, "withdraw_solvent")])),
            "not one of its members",
        ),
    ],
)
def test_a_malformed_plan_is_rejected_with_a_reason_the_model_can_act_on(plan, complaint):
    problem = hoist.validate_plan(CANDIDATES, plan)
    assert problem is not None and complaint in problem


def test_an_entry_point_is_matched_as_a_whole_name():
    """``merge`` in ``merged`` or ``split_merge`` is not the instruction."""
    assert hoist._named("The merged account and split_merge helper.", {"merge"}) == set()
    assert hoist._named("After `merge`, the account is solvent.", {"merge"}) == {"merge"}


# --------------------------------------------------------------------------------------------
# Synthesizing a shared property from slices
# --------------------------------------------------------------------------------------------

# The arm 3 shape: conservation stated per instruction group, never whole.
CONSERVE_SPLIT = _prop("split_conserves", "`split` and `merge` conserve the two accounts' lamports.")
CONSERVE_WITHDRAW = _prop("withdraw_conserves", "`withdraw` conserves the two accounts' lamports.")
SLICES = hoist.build_candidates(
    [
        (0, "Split & Merge", ["split", "merge"], [CONSERVE_SPLIT]),
        (1, "Withdrawal", ["withdraw"], [CONSERVE_WITHDRAW]),
        (2, "Movement", ["move_stake"], [_prop("move_signer", "`move_stake` needs the staker.")]),
    ]
)
CONSERVE_ALL = _prop(
    "lamports_conserved", "`split`, `merge` and `withdraw` conserve the lamports they move."
)
BOTH = [(SPLIT, "split_conserves"), (WITHDRAWAL, "withdraw_conserves")]


def test_slices_that_no_member_states_whole_can_be_synthesized_into_one_property():
    plan = _plan((CONSERVE_ALL, BOTH))
    assert hoist.validate_plan(SLICES, plan) is None
    applied = hoist.apply_plan(SLICES, plan)
    assert applied.shared == [CONSERVE_ALL]
    assert applied.synthesized == ["lamports_conserved"]
    assert applied.kept[0] == [] and applied.kept[1] == []


def test_a_synthesized_property_may_not_name_an_entry_point_no_member_names():
    """That would assert something no extractor proposed."""
    wider = _prop("t", "`split`, `merge`, `withdraw` and `move_stake` conserve lamports.")
    problem = hoist.validate_plan(SLICES, _plan((wider, BOTH)))
    assert problem is not None and "names entry points no member names: ['move_stake']" in problem


def test_a_synthesized_property_must_keep_every_member():
    """Dropping a member's entry points would drop that member's property without a trace."""
    without_withdraw = _prop("t", "`split` and `merge` conserve lamports.")
    problem = hoist.validate_plan(SLICES, _plan((without_withdraw, BOTH)))
    assert problem is not None and "names none of the entry points of these members" in problem


def test_a_synthesized_property_may_leave_out_an_entry_point_a_member_only_excludes():
    """A member names instructions it excludes, so its mentions overstate its scope."""
    excluding = _prop(
        "withdraw_conserves", "`withdraw`, unlike `move_stake`, conserves the two accounts' lamports."
    )
    candidates = hoist.build_candidates(
        [
            (0, "Split & Merge", ["split", "merge"], [CONSERVE_SPLIT]),
            (1, "Withdrawal", ["withdraw"], [excluding]),
            (2, "Movement", ["move_stake"], []),
        ]
    )
    assert hoist.validate_plan(candidates, _plan((CONSERVE_ALL, BOTH))) is None


def test_a_synthesized_property_needs_more_than_one_member():
    problem = hoist.validate_plan(SLICES, _plan((CONSERVE_ALL, BOTH[:1])))
    assert problem is not None and "from one member" in problem


# --------------------------------------------------------------------------------------------
# The Solana shared unit
# --------------------------------------------------------------------------------------------


def _app(component_names: list[str]) -> SolanaApplication:
    instructions = ["split", "merge", "withdraw"]
    return SolanaApplication.model_validate(
        {
            "application_type": "Staking",
            "description": "d",
            "components": [
                {
                    "name": "Stake",
                    "program_identifier": "stake",
                    "description": "d",
                    "account_types": ["StakeState: all state."],
                    "instructions": [
                        {"name": n, "description": n, "args": [], "accounts": [], "signers": [],
                         "cpis": [], "requirements": []}
                        for n in instructions
                    ],
                    "components": [
                        {"name": c, "description": c, "instructions": [instructions[i]],
                         "account_types": [], "requirements": [], "interactions": []}
                        for i, c in enumerate(component_names)
                    ],
                }
            ],
        }
    )


def test_the_shared_unit_spans_every_instruction_and_follows_the_extracted_units():
    app = _app(["Splitting", "Merging", "Withdrawal"])
    unit = protocol_unit(SolanaProgramInstance(0, app))
    assert unit.display_name == PROTOCOL_UNIT_NAME
    assert unit.unit_index == 3
    assert [i.name for i in unit.instructions] == ["split", "merge", "withdraw"]
    # The extracted units keep the model they were extracted under.
    assert len(app.programs[0].components) == 3


def test_the_shared_unit_does_not_take_an_existing_component_s_slug():
    app = _app([PROTOCOL_UNIT_NAME, "Merging", "Withdrawal"])
    unit = protocol_unit(SolanaProgramInstance(0, app))
    assert unit.slug not in {c.name for c in app.programs[0].components}
    assert unit.display_name == f"{PROTOCOL_UNIT_NAME} (shared)"


def test_the_shared_unit_carries_every_component_s_computations_once():
    """A bound hoisted into the shared unit is about a computation some component named; the unit's
    author needs to see it. Two components naming one function list it once."""
    app = _app(["Splitting", "Merging", "Withdrawal"])
    raw = app.model_dump()
    math = {"function": "iface::Delegation::stake", "crate": "iface",
            "computes": "the effective stake", "used_by": ["withdraw"]}
    for comp in raw["components"][0]["components"][1:]:
        comp["computations"] = [math]
    unit = protocol_unit(SolanaProgramInstance(0, SolanaApplication.model_validate(raw)))
    assert [c.function for c in unit.component.computations] == ["iface::Delegation::stake"]


def test_salvage_keeps_the_valid_groups_of_a_plan_that_will_not_validate():
    """One group the model keeps getting wrong should not cost the others their hoisting."""
    bad = _prop("t", "`split`, `merge`, `withdraw` and `move_stake` conserve lamports.")
    plan = _plan(
        (CONSERVE_ALL, BOTH),
        (bad, [(SPLIT, "split_conserves")]),
    )
    assert hoist.validate_plan(SLICES, plan) is not None
    assert [g.shared for g in hoist.salvage(SLICES, plan).groups] == [
        hoist.Synthesized(property=CONSERVE_ALL)
    ]
