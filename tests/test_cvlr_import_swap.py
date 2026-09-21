"""The ninth munge kind: an imported name pointed at a stand-in.

``docs/cvlr-todo.md`` U10 is why it exists. Every other kind edits the item it is about, so the item
has to belong to the program — and a CPI's does not: ``invoke`` is ``solana_program``'s, the call is
an expression rather than an item, and the optimizer leaves no symbol to summarize. The ``use`` line
is the program's, which is the seam this kind works on, and the corpus writes the same edit by hand
(``solana-program-stake-pool-audit`` swaps ``solana_program::msg`` for its own).

These tests are about what that makes true: the deployed build imports what the developer wrote,
only the named binding moves, and a declaration this reader cannot take one leaf out of is refused
rather than approximated.
"""

import pytest

from composer.spec.cvlr.munge import (
    AlreadyMunged,
    FunctionAmbiguous,
    ImportNotFound,
    ImportNotIsolable,
    ImportSwap,
    Munged,
    apply_import_swap,
    munge_history,
)
from composer.spec.cvlr.tree import replay

FEATURE = "unit_vault_deposits"
LIB = "programs/vault/src/lib.rs"
STAND_IN = "crate::certora::specs::unit_vault_deposits::invoke_transfer"

VAULT = """\
#![allow(unexpected_cfgs)]
use anchor_lang::prelude::*;
use anchor_lang::solana_program::program::invoke;
use anchor_lang::solana_program::system_instruction;

#[program]
pub mod vault_program {
    use super::*;

    pub fn deposit(ctx: Context<Deposit>, amount: u64) -> Result<()> {
        invoke(&system_instruction::transfer(&a, &b, amount), &[])?;
        vault_accounting::apply_deposit(&mut ctx.accounts.vault, amount)
    }
}
"""


def _swap(name: str = "invoke", **kw) -> ImportSwap:
    return ImportSwap(
        path=kw.pop("path", LIB),
        name=name,
        stand_in=kw.pop("stand_in", STAND_IN),
        why=kw.pop(
            "why",
            "the CPI havocs the caller's deserialized Vault; the stand-in moves no lamports, so "
            "no property about account balances holds under it",
        ),
        feature=kw.pop("feature", FEATURE),
    )


def _applied(source: str = VAULT, **kw) -> str:
    outcome = apply_import_swap(source, _swap(**kw))
    assert isinstance(outcome, Munged), outcome
    return outcome.source


# ---------------------------------------------------------------------------------------------
# the deployed build


def test_the_developers_declaration_survives_verbatim():
    assert "use anchor_lang::solana_program::program::invoke;" in _applied()


def test_the_original_is_compiled_only_with_the_feature_off():
    body = _applied()
    gate = f'#[cfg(not(feature = "{FEATURE}"))]\nuse anchor_lang::solana_program::program::invoke;'
    assert gate in body


def test_the_stand_in_is_bound_under_the_name_the_call_sites_use():
    assert f'#[cfg(feature = "{FEATURE}")]\nuse {STAND_IN} as invoke;' in _applied()


def test_the_call_site_is_not_touched():
    """The point of working on the import: no expression in the program is rewritten."""
    assert "invoke(&system_instruction::transfer(&a, &b, amount), &[])?;" in _applied()


def test_the_unrelated_import_is_left_alone():
    body = _applied()
    assert body.count("use anchor_lang::solana_program::system_instruction;") == 1
    assert 'system_instruction;\n#[cfg' not in body


def test_the_gate_names_the_recording_unit():
    body = _applied(feature="unit_other")
    assert '#[cfg(feature = "unit_other")]' in body
    assert f'feature = "{FEATURE}"' not in body


def test_it_creates_no_file():
    """Unlike a module redirect: the stand-in is an item in the harness, reviewed with it."""
    assert _swap().created == {}


# ---------------------------------------------------------------------------------------------
# taking one name out of a group


GROUPED = """\
use solana_program::program::{invoke, invoke_signed};
"""


def test_a_grouped_import_keeps_the_names_it_did_not_swap():
    body = _applied(GROUPED)
    assert f'#[cfg(feature = "{FEATURE}")]\nuse solana_program::program::{{invoke_signed}};' in body


def test_a_grouped_imports_original_is_still_whole_for_the_deployed_build():
    assert "use solana_program::program::{invoke, invoke_signed};" in _applied(GROUPED)


def test_a_group_reduced_to_nothing_emits_no_remainder():
    body = _applied("use solana_program::program::{invoke};\n")
    assert "{}" not in body
    assert body.count("#[cfg(feature") == 1


def test_a_multi_line_group_is_read_as_one_declaration():
    source = "use solana_program::program::{\n    invoke,\n    invoke_signed,\n};\n"
    body = _applied(source)
    assert "use solana_program::program::{invoke_signed};" in body
    assert body.startswith(f'#[cfg(not(feature = "{FEATURE}"))]\nuse solana_program::program::{{\n')


def test_a_visibility_is_carried_to_both_halves():
    body = _applied("pub(crate) use solana_program::program::{invoke, invoke_signed};\n")
    assert "pub(crate) use solana_program::program::{invoke_signed};" in body
    assert f"pub(crate) use {STAND_IN} as invoke;" in body


def test_a_stand_in_already_named_for_the_binding_is_not_aliased_to_itself():
    """`use x::y as y` compiles and reads as a rename of something that was not renamed."""
    body = _applied(stand_in="crate::certora::specs::unit_vault_deposits::invoke")
    assert "use crate::certora::specs::unit_vault_deposits::invoke;" in body
    assert " as invoke;" not in body


def test_replaying_an_unaliased_swap_is_still_idempotent():
    swap = _swap(stand_in="crate::certora::specs::unit_vault_deposits::invoke")
    landed = apply_import_swap(VAULT, swap)
    assert isinstance(landed, Munged), landed
    assert isinstance(apply_import_swap(landed.source, swap), AlreadyMunged)


def test_an_aliased_import_is_named_by_what_it_binds():
    body = _applied("use solana_program::program::invoke as cpi;\n", name="cpi")
    assert f"use {STAND_IN} as cpi;" in body


def test_an_indented_declaration_keeps_its_indentation():
    body = _applied("mod m {\n    use solana_program::program::invoke;\n}\n")
    assert f'    #[cfg(not(feature = "{FEATURE}"))]\n    use solana_program' in body
    assert f"    use {STAND_IN} as invoke;" in body


# ---------------------------------------------------------------------------------------------
# refusals


def test_a_name_the_file_never_imports_is_reported_with_what_it_does():
    outcome = apply_import_swap(VAULT, _swap(name="invoke_signed"))
    assert isinstance(outcome, ImportNotFound)
    assert outcome.name == "invoke_signed"
    assert set(outcome.nearby) == {"invoke", "system_instruction"}


def test_a_glob_leaves_no_declaration_to_rewrite():
    """It binds the name without spelling it, so there is nothing to gate — and nothing to guess."""
    outcome = apply_import_swap("use solana_program::program::*;\n", _swap())
    assert isinstance(outcome, ImportNotFound)
    assert outcome.nearby == ()


def test_a_nested_tree_is_refused_rather_than_split():
    outcome = apply_import_swap("use solana_program::{program::{invoke}, pubkey::Pubkey};\n", _swap())
    assert isinstance(outcome, ImportNotIsolable)
    assert "invoke" in outcome.declaration


def test_two_declarations_binding_the_name_are_refused():
    source = "use a::invoke;\n\nmod m {\n    use b::invoke;\n}\n"
    outcome = apply_import_swap(source, _swap())
    assert isinstance(outcome, FunctionAmbiguous)
    assert outcome.lines == (1, 4)


def test_a_swap_already_in_the_developers_source_is_not_doubled():
    outcome = apply_import_swap(_applied(), _swap())
    assert isinstance(outcome, AlreadyMunged)


# ---------------------------------------------------------------------------------------------
# replay and identity


def test_replaying_a_landed_swap_is_a_no_op():
    """The tree is rebuilt from pristine and replayed on every build, so this runs constantly."""
    assert isinstance(apply_import_swap(_applied(), _swap()), AlreadyMunged)


def test_a_second_units_swap_lands_beside_the_first_rather_than_seeing_two_bindings():
    """The shared tree replays the union onto pristine, so one swap meets the other's output."""
    body = _applied()
    other = _swap(feature="unit_other", stand_in="crate::certora::specs::unit_other::invoke_noop")
    outcome = apply_import_swap(body, other)
    assert isinstance(outcome, Munged), outcome
    assert "use crate::certora::specs::unit_other::invoke_noop as invoke;" in outcome.source
    assert outcome.source.count("use crate::certora::specs") == 2
    assert f'#[cfg(not(feature = "{FEATURE}"))]\n#[cfg(not(feature = "unit_other"))]' in outcome.source


def test_replay_applies_it_through_the_shared_driver():
    body, drifted = replay(VAULT, (_swap(),))
    assert drifted == ()
    assert f"use {STAND_IN} as invoke;" in body


def test_a_declaration_that_gained_a_name_swaps_rather_than_drifting():
    """No captured text, so growth in the developer's import is absorbed rather than reported.

    The feature-on half is derived from whatever the ``use`` says at replay time, which is the
    property that lets this kind skip the drift detector an extraction needs.
    """
    body, drifted = replay(GROUPED.replace("invoke_signed", "invoke_signed, invoke_unchecked"), (_swap(),))
    assert drifted == ()
    assert "use solana_program::program::{invoke_signed, invoke_unchecked};" in body


def test_the_edit_id_moves_with_the_stand_in():
    assert _swap().edit_id != _swap(stand_in="crate::certora::specs::other::invoke_noop").edit_id


def test_the_edit_id_is_stable_across_rewordings():
    assert _swap().edit_id == _swap(why="a better sentence").edit_id


def test_two_units_swapping_the_same_import_do_not_collide():
    assert _swap().edit_id != _swap(feature="unit_other").edit_id


def test_it_reaches_version_history_like_every_other_kind():
    assert munge_history((_swap(),)) == (f"munge:{_swap().edit_id}",)


def test_describe_names_the_scope_it_has():
    assert "throughout this file" in _swap().describe()


@pytest.mark.parametrize(
    "decl",
    [
        "use a::b::invoke;",
        "pub use a::b::invoke;",
        "pub(crate) use a::b::invoke;",
        "pub(super) use a::b::invoke;",
    ],
)
def test_every_visibility_a_declaration_can_carry_is_matched(decl: str):
    outcome = apply_import_swap(decl + "\n", _swap())
    assert isinstance(outcome, Munged), outcome
