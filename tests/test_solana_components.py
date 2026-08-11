"""The Solana ecosystem's ``ProgramComponent`` model and its analysis-time validation.

Per docs/ecosystem-abstraction.md §4: a program gains ``components`` — the Solana analog
of EVM's ``ContractComponent`` — and ``_solana_validate`` gains the rules that keep the
component→instruction mapping honest. ``units`` is still whole-program at this stage, so nothing
downstream changes; these tests cover the model and the validator in isolation.
"""

from typing import Any

import pytest

from composer.pipeline.ecosystem import _solana_validate
from composer.spec.solana.model import (
    AuthorityInteraction,
    InterComponentInteraction,
    SolanaApplication,
)
from composer.spec.types import RustIdentifier

VAULT_ID = RustIdentifier("vault")


def _raw() -> dict[str, Any]:
    """A well-formed two-component vault, as the analysis agent would emit it."""
    return {
        "application_type": "Vault",
        "description": "A single-program lamports vault.",
        "components": [
            {
                "name": "Vault",
                "program_identifier": VAULT_ID,
                "description": "Holds per-user deposits in a PDA.",
                "account_types": ["Vault"],
                "instructions": [
                    {"name": "initialize", "description": "create the vault PDA", "requirements": []},
                    {"name": "deposit", "description": "move lamports in", "requirements": []},
                    {"name": "withdraw", "description": "move lamports out", "requirements": []},
                ],
                "components": [
                    {
                        "name": "Deposits",
                        "description": "Creating a vault and funding it.",
                        "instructions": ["initialize", "deposit"],
                        "account_types": ["Vault"],
                        "interactions": [
                            {"authority": "System Program", "description": "CPI transfer in"}
                        ],
                        "requirements": ["The implementation must credit the depositor."],
                    },
                    {
                        "name": "Withdrawals",
                        "description": "Releasing funds to the recorded authority.",
                        "instructions": ["withdraw"],
                        "account_types": ["Vault"],
                        "interactions": [
                            {
                                "program": "Vault",
                                "component": "Deposits",
                                "description": "reads the balance Deposits maintains",
                            }
                        ],
                        "requirements": ["The implementation must only pay the authority."],
                    },
                ],
            },
            {
                "name": "System Program",
                "description": "Solana's native System program.",
                "assumptions": ["Behaves per the Solana runtime spec."],
            },
        ],
    }


def _app(mutate=None) -> SolanaApplication:
    raw = _raw()
    if mutate is not None:
        mutate(raw)
    return SolanaApplication.model_validate(raw)


def _program(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["components"][0]


def _components(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return _program(raw)["components"]


# --- the model ---------------------------------------------------------------------


def test_components_parse_and_instructions_resolve_through_the_program():
    prog = _app().programs[0]
    assert [c.name for c in prog.components] == ["Deposits", "Withdrawals"]
    # A component references its instructions by name; the program stays authoritative for the
    # objects, and `instructions_by_name` is the one place the names resolve.
    deposits = prog.components[0]
    resolved = [prog.instructions_by_name[n] for n in deposits.instructions]
    assert [i.description for i in resolved] == ["create the vault PDA", "move lamports in"]


def test_interaction_union_discriminates_by_shape():
    prog = _app().programs[0]
    assert isinstance(prog.components[0].interactions[0], AuthorityInteraction)
    inter = prog.components[1].interactions[0]
    assert isinstance(inter, InterComponentInteraction)
    assert (inter.program, inter.component) == ("Vault", "Deposits")


# --- validation: the happy path and the existing rules -------------------------------


def test_well_formed_application_validates():
    assert _solana_validate(_app(), VAULT_ID) is None


def test_expected_main_is_still_required():
    problem = _solana_validate(_app(), RustIdentifier("not_the_vault"))
    assert problem is not None and "not_the_vault" in problem


# --- validation: component rules -----------------------------------------------------


def test_duplicate_component_names_rejected():
    def dup(raw):
        _components(raw)[1]["name"] = "Deposits"

    problem = _solana_validate(_app(dup), VAULT_ID)
    assert problem is not None and "Duplicate component names in Vault: Deposits" in problem


def test_component_slug_collision_rejected():
    # "Deposits!" and "Deposits" are distinct names that slugify to the same artifact id.
    def collide(raw):
        _components(raw)[1]["name"] = "Deposits!"

    problem = _solana_validate(_app(collide), VAULT_ID)
    assert problem is not None and "filename slug" in problem


def test_unknown_instruction_reference_rejected():
    def typo(raw):
        _components(raw)[0]["instructions"] = ["initialize", "depsoit"]

    problem = _solana_validate(_app(typo), VAULT_ID)
    assert problem is not None
    assert "lists an instruction 'depsoit' that Vault does not declare" in problem


def test_instruction_belonging_to_no_component_rejected():
    def orphan(raw):
        _components(raw)[0]["instructions"] = ["initialize"]  # drops `deposit`

    problem = _solana_validate(_app(orphan), VAULT_ID)
    assert problem is not None and "'deposit'" in problem and "belong to no component" in problem


def test_an_instruction_may_serve_two_components():
    def overlap(raw):
        _components(raw)[1]["instructions"] = ["withdraw", "initialize"]

    assert _solana_validate(_app(overlap), VAULT_ID) is None


def test_a_program_with_no_instructions_needs_no_components():
    def empty(raw):
        _program(raw)["instructions"] = []
        _program(raw)["components"] = []

    assert _solana_validate(_app(empty), VAULT_ID) is None


# --- validation: interactions --------------------------------------------------------


@pytest.mark.parametrize(
    "interaction, expected",
    [
        ({"authority": "Pyth", "description": "reads a price"}, "unknown external authority: Pyth"),
        (
            {"program": "Staking", "component": None, "description": "x"},
            "an unknown program: Staking",
        ),
        (
            {"program": "Vault", "component": "Rewards", "description": "x"},
            "unknown component Rewards of program Vault",
        ),
    ],
)
def test_unresolvable_interactions_rejected(interaction, expected):
    def point_nowhere(raw):
        _components(raw)[0]["interactions"] = [interaction]

    problem = _solana_validate(_app(point_nowhere), VAULT_ID)
    assert problem is not None and expected in problem


def test_a_forward_reference_between_components_is_fine():
    # Interactions are resolved in a second pass, so Deposits may name a component declared after
    # it (the EVM validator does the same).
    def forward(raw):
        _components(raw)[0]["interactions"] = [
            {"program": "Vault", "component": "Withdrawals", "description": "hands off"}
        ]

    assert _solana_validate(_app(forward), VAULT_ID) is None


# --- validation: the retry feedback --------------------------------------------------


def test_feedback_lists_the_declared_names_for_the_retry():
    def typo(raw):
        _components(raw)[0]["instructions"] = ["initialize", "depsoit"]

    problem = _solana_validate(_app(typo), VAULT_ID)
    assert problem is not None
    assert "For reference, the names you declared in your submission:" in problem
    assert "- Declared programs: Vault" in problem
    assert "- Declared external authorities: System Program" in problem
    assert "- Components of Vault: Deposits, Withdrawals" in problem


def test_multiple_errors_are_all_reported():
    def two_problems(raw):
        _components(raw)[1]["name"] = "Deposits"
        _components(raw)[0]["interactions"] = [{"authority": "Pyth", "description": "x"}]

    problem = _solana_validate(_app(two_problems), VAULT_ID)
    assert problem is not None
    assert problem.startswith("Multiple validation errors")
    assert "Duplicate component names" in problem and "Pyth" in problem
