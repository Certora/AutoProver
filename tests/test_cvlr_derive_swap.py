"""The seventh munge kind: derives moved behind a unit's feature.

``docs/the-state-behind-the-bytes.md`` is why it exists — a derived trait impl has no function to
name, so none of the other six reaches it. These tests are about the two things that make it safe to
replay: the deployed build must see exactly what it saw before, and a cascade must land whole or not
at all.
"""

import pytest

from composer.spec.cvlr.munge import (
    AlreadyMunged,
    DeriveAttributeUnreadable,
    DeriveNotFound,
    DeriveSwap,
    FunctionAmbiguous,
    Munged,
    SourceDrifted,
    SwappedDerive,
    apply_derive_swap,
    munge_history,
)
from composer.spec.cvlr.tree import replay

FEATURE = "unit_admin_fee_configuration"

STATE = """\
/// Initialized program details.
#[repr(C)]
#[derive(Clone, Debug, Default, PartialEq, BorshDeserialize, BorshSerialize, BorshSchema)]
pub struct StakePool {
    pub account_type: AccountType,
}

#[derive(Clone, Debug, Default, PartialEq, BorshDeserialize, BorshSerialize, BorshSchema)]
pub enum AccountType {
    Uninitialized,
}
"""

POOL = SwappedDerive(
    "StakePool",
    "#[derive(Clone, Debug, Default, PartialEq, BorshDeserialize, BorshSerialize, BorshSchema)]\n"
    "pub struct StakePool {",
    ("BorshDeserialize", "BorshSerialize"),
    ("Copy",),
)
ACCOUNT_TYPE = SwappedDerive(
    "AccountType",
    "#[derive(Clone, Debug, Default, PartialEq, BorshDeserialize, BorshSerialize, BorshSchema)]\n"
    "pub enum AccountType {",
    (),
    ("Copy",),
)


def _swap(*swaps: SwappedDerive, feature: str = FEATURE) -> DeriveSwap:
    return DeriveSwap(
        path="program/src/state.rs",
        swaps=swaps or (POOL,),
        why="the harness supplies borsh over a havoc'd global",
        feature=feature,
    )


def _applied(*swaps: SwappedDerive, source: str = STATE, **kw) -> str:
    result = apply_derive_swap(source, _swap(*swaps, **kw))
    assert isinstance(result, Munged), result
    return result.source


# ---------------------------------------------------------------------------------------------
# the deployed build


def test_the_removed_derives_survive_behind_a_not_gate() -> None:
    """The whole safety story: with the feature off the type derives exactly what it derived."""
    out = _applied()

    assert (
        f'#[cfg_attr(not(feature = "{FEATURE}"), derive(BorshDeserialize, BorshSerialize))]' in out
    )


def test_derives_the_swap_does_not_name_stay_ungated() -> None:
    """`BorshSchema` and friends were never in question, so they must not move — a gated `Clone`
    would change the deployed build for a type nobody asked about."""
    out = _applied()

    assert "#[derive(Clone, Debug, Default, PartialEq, BorshSchema)]" in out


def test_attributes_above_the_derive_are_left_alone() -> None:
    """`#[repr(C)]` decides the type's layout. Moving or dropping it would change the deployed
    build in the one way this kind must never change it."""
    out = _applied()

    assert "#[repr(C)]\n#[cfg_attr(not(feature" in out
    assert "/// Initialized program details." in out


def test_nothing_removed_emits_no_negative_gate() -> None:
    """`AccountType` only *gains* `Copy`. An empty `derive()` is not valid Rust, and emitting one
    would fail the build with a message pointing at the type rather than at the munge."""
    out = _applied(POOL, ACCOUNT_TYPE)

    assert "derive())" not in out
    assert (
        f'#[cfg_attr(feature = "{FEATURE}", derive(Copy))]\n'
        "#[derive(Clone, Debug, Default, PartialEq, BorshDeserialize, BorshSerialize, "
        "BorshSchema)]\npub enum AccountType {" in out
    ), "the gate sits directly above an otherwise untouched derive list"


# ---------------------------------------------------------------------------------------------
# replay


def test_replaying_a_landed_swap_is_a_no_op() -> None:
    """The tree is rebuilt from pristine and replayed on every build, so this runs constantly. A
    second application would stack a second pair of gates and change the crate's bytes."""
    once = _applied(POOL, ACCOUNT_TYPE)

    assert isinstance(apply_derive_swap(once, _swap(POOL, ACCOUNT_TYPE)), AlreadyMunged)


def test_a_cascade_lands_whole() -> None:
    """`derive(Copy)` on a struct needs every field type to be `Copy`, so the two swaps stand or
    fall together — half a cascade is a program that does not compile."""
    out = _applied(POOL, ACCOUNT_TYPE)

    assert out.count(f'#[cfg_attr(feature = "{FEATURE}", derive(Copy))]') == 2


def test_a_moved_type_reports_drift_rather_than_rewriting() -> None:
    """Content addressing, for the same reason an extraction has it: a rewrite has to know the
    region it replaces is the region it was recorded against."""
    moved = STATE.replace("pub struct StakePool {", "pub struct StakePoolV2 {")

    assert isinstance(apply_derive_swap(moved, _swap()), SourceDrifted)


def test_two_identical_captures_are_refused_rather_than_guessed() -> None:
    doubled = STATE + STATE

    assert isinstance(apply_derive_swap(doubled, _swap()), FunctionAmbiguous)


def test_replay_applies_it_through_the_shared_driver() -> None:
    out, drifted = replay(STATE, (_swap(POOL, ACCOUNT_TYPE),))

    assert drifted == ()
    assert "#[derive(Clone, Debug, Default, PartialEq, BorshSchema)]" in out


# ---------------------------------------------------------------------------------------------
# refusals


def test_removing_a_derive_the_type_does_not_have_is_refused() -> None:
    """A swap that removed nothing is a silent no-op: the harness supplies an impl the type still
    derives, and the build fails on a conflict rather than on the munge."""
    result = apply_derive_swap(
        STATE,
        _swap(
            SwappedDerive("StakePool", POOL.original, ("AnchorSerialize",), ("Copy",)),
        ),
    )

    assert isinstance(result, DeriveNotFound)
    assert result.missing == ("AnchorSerialize",)
    assert "BorshSerialize" in result.present, "the refusal says what the type does derive"


def test_capturing_text_with_no_single_derive_list_is_refused() -> None:
    """The swap has to know exactly which derives survive; a guess drops one silently."""
    result = apply_derive_swap(
        STATE, _swap(SwappedDerive("StakePool", "pub struct StakePool {", ("BorshSerialize",)))
    )

    assert isinstance(result, DeriveAttributeUnreadable)


def test_two_derive_attributes_on_one_item_are_refused() -> None:
    result = apply_derive_swap(
        STATE,
        _swap(
            SwappedDerive(
                "StakePool",
                "#[derive(Clone)]\n#[derive(BorshSerialize)]\npub struct StakePool {",
                ("BorshSerialize",),
            )
        ),
    )

    assert isinstance(result, DeriveAttributeUnreadable)


# ---------------------------------------------------------------------------------------------
# identity


def test_the_edit_id_moves_with_what_the_compiler_sees() -> None:
    """A re-recorded swap must not inherit the previous one's review approval or prover stamp."""
    base = _swap(POOL).edit_id
    other = _swap(SwappedDerive("StakePool", POOL.original, ("BorshSerialize",), ("Copy",))).edit_id

    assert base != other


def test_the_edit_id_is_stable_across_rewordings() -> None:
    """`why` is prose. Correcting it must not cost a prover submission."""
    a = DeriveSwap("program/src/state.rs", (POOL,), "one reason", FEATURE)
    b = DeriveSwap("program/src/state.rs", (POOL,), "another reason entirely", FEATURE)

    assert a.edit_id == b.edit_id


def test_it_reaches_version_history_like_every_other_kind() -> None:
    (token,) = munge_history((_swap(),))

    assert token.startswith("munge:") and "derives[StakePool" in token


def test_two_units_swapping_the_same_type_do_not_collide() -> None:
    """Each unit's swap is gated on its own feature, so they are two lines rather than a conflict —
    the same property the attribute kinds have."""
    mine, theirs = _swap().edit_id, _swap(feature="unit_other").edit_id

    assert mine != theirs


def test_describe_reads_as_a_sentence_for_both_shapes() -> None:
    assert "stops deriving BorshDeserialize, BorshSerialize and derives Copy" in _swap(POOL).describe()
    assert "`AccountType` derives Copy" in _swap(ACCOUNT_TYPE).describe()


@pytest.mark.parametrize("feature", ["unit_a", "unit_b"])
def test_the_gate_names_the_recording_unit(feature: str) -> None:
    out = _applied(feature=feature)

    assert f'feature = "{feature}"' in out


# ---------------------------------------------------------------------------------------------
# two edits in one step


def test_two_edits_in_one_step_do_not_kill_the_component() -> None:
    """Run 9 died here after 21 minutes.

    Every edit tool writes `reviewed_digest`, and the model may call two in one step — which is
    legitimate, and which `proposed` already merges. Without a reducer LangGraph rejects the step
    with `InvalidUpdateError` and the component is lost. The bug was latent before there was a
    third edit tool; adding one made parallel calls likely enough to hit it.
    """
    from composer.spec.cvlr.editor import _latest_review

    assert _latest_review("approved-digest", None) is None, "an edit voids a standing approval"
    assert _latest_review(None, None) is None, "two edits in one step fold to no approval"
    assert _latest_review(None, "fresh") == "fresh", "a later review can still set one"
