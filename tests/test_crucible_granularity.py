"""Solana unit granularity: one unit per `ProgramComponent` — docs/crucible.md §2.

Covers the pieces of the granularity change that need no toolchain or LLM:
- the component unit wrapper (`SolanaComponentInstance`) and what it exposes to a backend,
- the SOLANA ecosystem's per-component `units` enumeration.

History: Solana was per-instruction, then briefly a single whole-program unit.
Both are gone — an instruction is a syntactic artifact, not a unit of behavior,
and one extraction agent for a whole program is a hard cap on depth. `Main`
(`SolanaProgramInstance`) is deliberately no longer a `FeatureUnit`; main and
unit are different axes, as on EVM. That line is held by the type checker —
`FeatureUnit` is not `@runtime_checkable`, and a structural isinstance could not
hold it anyway.
"""

from composer.spec.solana.model import (
    SolanaApplication,
    SolanaComponentInstance,
    SolanaProgramInstance,
)


def _app() -> SolanaApplication:
    return SolanaApplication.model_validate(
        {
            "application_type": "defi",
            "description": "a vault program",
            "components": [
                {
                    "name": "vault",
                    "description": "the vault program",
                    "program_identifier": "vault",
                    "account_types": ["Vault"],
                    "instructions": [
                        {"name": "deposit", "description": "d", "requirements": []},
                        {"name": "withdraw", "description": "w", "requirements": []},
                    ],
                    "components": [
                        {
                            "name": "Deposits",
                            "description": "taking deposits",
                            "instructions": ["deposit"],
                            "account_types": ["Vault"],
                            "interactions": [],
                            "requirements": ["must credit the depositor"],
                        },
                        {
                            "name": "Withdrawals",
                            "description": "releasing funds",
                            "instructions": ["withdraw"],
                            "account_types": ["Vault"],
                            "interactions": [],
                            "requirements": [],
                        },
                    ],
                }
            ],
        }
    )


def test_component_instructions_resolve_to_the_real_objects():
    # The component holds names; the program stays authoritative for accounts/CPIs/signers.
    unit = SolanaComponentInstance(1, SolanaProgramInstance(0, _app()))
    assert [i.name for i in unit.instructions] == ["withdraw"]
    assert [i.description for i in unit.instructions] == ["w"]


def test_sibling_components_are_context_not_content():
    unit = SolanaComponentInstance(0, SolanaProgramInstance(0, _app()))
    assert [c.name for c in unit.sibling_components] == ["Withdrawals"]


def test_feature_json_carries_the_component_and_only_the_component():
    # Mirrors EVM (`ContractComponentInstance.feature_json` is the component alone). The
    # whole-program surface reaches a backend by its own route — for Crucible, the shared fixture.
    unit = SolanaComponentInstance(0, SolanaProgramInstance(0, _app()))
    js = unit.feature_json()
    assert js["name"] == "Deposits"
    assert js["slug"] == unit.slug
    assert js["requirements"] == ["must credit the depositor"]
    # `instructions` is resolved from names to full objects, and scoped to THIS component.
    assert [i["name"] for i in js["instructions"]] == ["deposit"]
    assert "withdraw" not in str(js["instructions"])
    # No whole-program payload rides along.
    assert "all_instructions" not in js


def test_distinct_components_cache_separately():
    main = SolanaProgramInstance(0, _app())
    a, b = SolanaComponentInstance(0, main), SolanaComponentInstance(1, main)
    assert a.cache_material() != b.cache_material()
    assert a.context_tag() != b.context_tag()


def test_solana_units_are_one_per_component_of_the_main_program():
    from composer.pipeline.ecosystem import SOLANA

    main = SolanaProgramInstance(0, _app())
    units = SOLANA.units(main)
    assert [u.display_name for u in units] == ["Deposits", "Withdrawals"]
    assert [u.unit_index for u in units] == [0, 1]
