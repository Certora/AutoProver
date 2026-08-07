"""The Soroban ecosystem's model and its analysis-time validation.

The peer of ``tests/test_solana_components.py``: the same rules over
``ContractComponent`` (unique names/slugs, the component→entry-point mapping valid and total,
interactions resolving), plus the two rules Soroban adds because its storage is
durability-tagged — a component's ``storage_keys`` must resolve, and a key may not be declared
twice. See docs/ecosystem-abstraction.md §5.
"""

from typing import Any, cast

import pytest

from composer.pipeline.ecosystem import SOROBAN, _soroban_validate
from composer.spec.soroban.model import (
    AuthorityInteraction,
    InterComponentInteraction,
    SorobanApplication,
    SorobanContractInstance,
)
from composer.spec.types import RustIdentifier

VAULT_ID = RustIdentifier("vault")


def _raw() -> dict[str, Any]:
    """A well-formed two-component vault, as the analysis agent would emit it."""
    return {
        "application_type": "Vault",
        "description": "A single-contract token vault.",
        "components": [
            {
                "name": "Vault",
                "contract_identifier": VAULT_ID,
                "description": "Holds per-address deposits.",
                "storage_entries": [
                    {
                        "key": "Balance(Address)",
                        "durability": "persistent",
                        "value_type": "i128",
                        "description": "per-depositor balance",
                    },
                    {
                        "key": "Admin",
                        "durability": "instance",
                        "value_type": "Address",
                        "description": "the address allowed to upgrade",
                    },
                ],
                "functions": [
                    {
                        "name": "initialize",
                        "description": "record the admin",
                        "requirements": [],
                    },
                    {
                        "name": "deposit",
                        "description": "move tokens in",
                        "auth": [
                            {
                                "address": "from",
                                "kind": "require_auth",
                                "description": "the depositor must authorize",
                            }
                        ],
                        "requirements": [],
                    },
                    {"name": "withdraw", "description": "move tokens out", "requirements": []},
                ],
                "components": [
                    {
                        "name": "Deposits",
                        "description": "Initializing the vault and funding it.",
                        "functions": ["initialize", "deposit"],
                        "storage_keys": ["Balance(Address)", "Admin"],
                        "interactions": [
                            {"authority": "Token", "description": "calls transfer in"}
                        ],
                        "requirements": ["The implementation must credit the depositor."],
                    },
                    {
                        "name": "Withdrawals",
                        "description": "Releasing funds to the recorded depositor.",
                        "functions": ["withdraw"],
                        "storage_keys": ["Balance(Address)"],
                        "interactions": [
                            {
                                "contract": "Vault",
                                "component": "Deposits",
                                "description": "reads the balance Deposits maintains",
                            }
                        ],
                        "requirements": ["The implementation must only pay the depositor."],
                    },
                ],
            },
            {
                "name": "Token",
                "description": "A SEP-41 token the vault holds.",
                "assumptions": ["A Stellar Asset Contract; the issuer may clawback."],
            },
        ],
    }


def _app(mutate=None) -> SorobanApplication:
    raw = _raw()
    if mutate is not None:
        mutate(raw)
    return SorobanApplication.model_validate(raw)


def _contract(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["components"][0]


def _components(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return _contract(raw)["components"]


# --- the model ---------------------------------------------------------------------


def test_components_parse_and_functions_resolve_through_the_contract():
    contract = _app().contracts[0]
    assert [c.name for c in contract.components] == ["Deposits", "Withdrawals"]
    # A component references its functions by name; the contract stays authoritative for the
    # objects, and `functions_by_name` is the one place the names resolve.
    deposits = contract.components[0]
    resolved = [contract.functions_by_name[n] for n in deposits.functions]
    assert [f.description for f in resolved] == ["record the admin", "move tokens in"]


def test_absent_auth_is_recorded_as_empty_not_missing():
    """The Soroban peer of Solana's unconstrained account: nothing else authenticates a caller,
    so `auth == []` is the finding, and it must survive parsing rather than defaulting away."""
    by_name = _app().contracts[0].functions_by_name
    assert by_name["withdraw"].auth == []
    assert [a.address for a in by_name["deposit"].auth] == ["from"]


def test_interaction_union_discriminates_by_shape():
    contract = _app().contracts[0]
    assert isinstance(contract.components[0].interactions[0], AuthorityInteraction)
    inter = contract.components[1].interactions[0]
    assert isinstance(inter, InterComponentInteraction)
    assert (inter.contract, inter.component) == ("Vault", "Deposits")


def test_unit_resolves_functions_and_durability_tagged_storage():
    """``storage_entries`` resolves names to entries because a key without its durability is not
    interpretable — the reason this ecosystem validates ``storage_keys`` at all."""
    (deposits, withdrawals) = SOROBAN.units(SorobanContractInstance(0, _app()))
    assert deposits.display_name == "Deposits" and deposits.slug == "Deposits"
    assert [f.name for f in withdrawals.functions] == ["withdraw"]
    assert [(e.key, e.durability) for e in deposits.storage_entries] == [
        ("Balance(Address)", "persistent"),
        ("Admin", "instance"),
    ]
    # `feature_json` is the ecosystem-agnostic `dict[str, object]` a backend marshals, so the
    # resolved lists come back untyped — narrow them here rather than widen the protocol.
    feature = deposits.feature_json()
    assert feature["slug"] == "Deposits"
    functions = cast(list[dict[str, Any]], feature["functions"])
    storage = cast(list[dict[str, Any]], feature["storage_entries"])
    assert [f["name"] for f in functions] == ["initialize", "deposit"]
    assert [e["durability"] for e in storage] == ["persistent", "instance"]


# --- validation: the happy path and the shared rules -------------------------------


def test_well_formed_application_validates():
    assert _soroban_validate(_app(), VAULT_ID) is None


def test_expected_main_is_required():
    problem = _soroban_validate(_app(), RustIdentifier("not_the_vault"))
    assert problem is not None and "not_the_vault" in problem


def test_duplicate_component_names_rejected():
    def dup(raw):
        _components(raw)[1]["name"] = "Deposits"

    problem = _soroban_validate(_app(dup), VAULT_ID)
    assert problem is not None and "Duplicate component names in Vault: Deposits" in problem


def test_component_slug_collision_rejected():
    # "Deposits!" and "Deposits" are distinct names that slugify to the same artifact id.
    def collide(raw):
        _components(raw)[1]["name"] = "Deposits!"

    problem = _soroban_validate(_app(collide), VAULT_ID)
    assert problem is not None and "filename slug" in problem


def test_unknown_function_reference_rejected():
    def typo(raw):
        _components(raw)[0]["functions"] = ["initialize", "depsoit"]

    problem = _soroban_validate(_app(typo), VAULT_ID)
    assert problem is not None
    assert "lists a function 'depsoit' that Vault does not declare" in problem


def test_function_belonging_to_no_component_rejected():
    def orphan(raw):
        _components(raw)[0]["functions"] = ["initialize"]  # drops `deposit`

    problem = _soroban_validate(_app(orphan), VAULT_ID)
    assert problem is not None and "'deposit'" in problem and "belong to no component" in problem


def test_a_function_may_serve_two_components():
    def overlap(raw):
        _components(raw)[1]["functions"] = ["withdraw", "initialize"]

    assert _soroban_validate(_app(overlap), VAULT_ID) is None


def test_a_contract_with_no_functions_needs_no_components():
    def empty(raw):
        _contract(raw)["functions"] = []
        _contract(raw)["components"] = []

    assert _soroban_validate(_app(empty), VAULT_ID) is None


@pytest.mark.parametrize(
    "interaction, expected",
    [
        ({"authority": "Reflector", "description": "reads a price"},
         "unknown external authority: Reflector"),
        (
            {"contract": "Pool", "component": None, "description": "x"},
            "an unknown contract: Pool",
        ),
        (
            {"contract": "Vault", "component": "Rewards", "description": "x"},
            "unknown component Rewards of contract Vault",
        ),
    ],
)
def test_unresolvable_interactions_rejected(interaction, expected):
    def point_nowhere(raw):
        _components(raw)[0]["interactions"] = [interaction]

    problem = _soroban_validate(_app(point_nowhere), VAULT_ID)
    assert problem is not None and expected in problem


# --- validation: the Soroban-specific storage rules ----------------------------------


def test_unknown_storage_key_reference_rejected():
    """Unlike Solana's bare-name ``account_types``, a component's ``storage_keys`` are references
    the unit wrapper resolves; a dangling one renders as nothing at all."""
    def typo(raw):
        _components(raw)[1]["storage_keys"] = ["Blance(Address)"]

    problem = _soroban_validate(_app(typo), VAULT_ID)
    assert problem is not None
    assert "lists a storage key 'Blance(Address)'" in problem


def test_a_key_declared_under_two_durabilities_rejected():
    """The two are distinct entries on-chain, but ``storage_by_key`` is keyed by name — so one
    would shadow the other and the prompt would attribute the wrong expiry semantics."""
    def shadow(raw):
        _contract(raw)["storage_entries"].append(
            {
                "key": "Balance(Address)",
                "durability": "temporary",
                "value_type": "i128",
                "description": "a cache of the same balance",
            }
        )

    problem = _soroban_validate(_app(shadow), VAULT_ID)
    assert problem is not None
    assert "declared twice in Vault" in problem and "separate key spaces" in problem


def test_a_component_need_not_claim_every_storage_key():
    """Totality is required of the function mapping, not of storage: an entry only one component
    touches, or that no component lists, is ordinary."""
    def drop(raw):
        _components(raw)[0]["storage_keys"] = []

    assert _soroban_validate(_app(drop), VAULT_ID) is None


# --- the registry --------------------------------------------------------------------


def test_soroban_is_registered_and_locates_its_main():
    from composer.pipeline.ecosystem import ECOSYSTEMS

    assert ECOSYSTEMS["soroban"] is SOROBAN
    assert SOROBAN.name == "soroban" and SOROBAN.language.name == "rust"
    assert not SOROBAN.supports_greenfield
