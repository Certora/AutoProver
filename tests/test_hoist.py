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


def _plan(*groups: tuple[tuple[str, str], list[tuple[str, str]]]) -> hoist.HoistPlan:
    return hoist.HoistPlan(
        groups=[
            hoist.HoistGroup(claim="claim", representative=rep, members=members)
            for rep, members in groups
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
    assert ((WITHDRAWAL, "withdraw_solvent"), (SPLIT, "solvent")) in applied.absorbed


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
    assert not hoist._names_outside("The merged account and split_merge helper.", set(), {"merge"})
    assert hoist._names_outside("After `merge`, the account is solvent.", set(), {"merge"})


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
