"""The author's worked invocation example, substituted against the analyzed program.

Covers the two halves of ``composer.spec.cvlr.example``: reading a handler's construction off the
model (which types produce a constructor, which arguments split, which instruction gets picked), and
the template branch that renders it — including that the fallback still exists for a run with no
component.
"""

import pytest

from composer.spec.cvlr.anchor_surface import Param, read_surface
from composer.spec.cvlr.conf import TEMPLATE_BASE
from composer.spec.cvlr.example import (
    ExampleAccount,
    WorkedExample,
    account_constructor,
    split_arg,
    worked_example,
)
from composer.spec.solana.model import (
    AccountConstraint,
    ProgramComponent,
    SolanaApplication,
    SolanaComponentInstance,
    SolanaInstruction,
    SolanaProgram,
    SolanaProgramInstance,
)
from composer.spec.types import ProgramName, PropertyFormulation, RustIdentifier
from composer.templates.loader import env

TEMPLATE = "cvlr_property_generation_system_prompt.j2"


def _account(name: str, account_type: str) -> AccountConstraint:
    return AccountConstraint(name=name, account_type=account_type)


def _instruction(name: str, *, accounts: list[AccountConstraint], args: list[str] | None = None):
    return SolanaInstruction(
        name=name,
        description=f"The {name} handler.",
        accounts=accounts,
        args=args or [],
        requirements=[],
    )


def _component(*instructions: SolanaInstruction) -> SolanaComponentInstance:
    program = SolanaProgram(
        name=ProgramName("Lending"),
        program_identifier=RustIdentifier("lending"),
        description="Supplies and withdraws liquidity.",
        instructions=list(instructions),
        components=[
            ProgramComponent(
                name="Exchange Rate",
                description="Share pricing.",
                instructions=[i.name for i in instructions],
                account_types=["Lending"],
                interactions=[],
                requirements=[],
            )
        ],
    )
    app = SolanaApplication(
        application_type="Lending market", description="A lending market.", components=[program]
    )
    return SolanaComponentInstance(0, SolanaProgramInstance(0, app))


def _prop(title: str, description: str) -> PropertyFormulation:
    return PropertyFormulation(title=title, sort="invariant", description=description)


DEPOSIT = _instruction(
    "deposit_reserve_liquidity",
    accounts=[
        _account("lending", "Box<Account<'info, Lending>>"),
        _account("depositor", "Signer<'info>"),
    ],
    args=["amount: u64"],
)
WITHDRAW = _instruction(
    "withdraw", accounts=[_account("lending", "Account<'info, Lending>")], args=["shares: u64"]
)


# -- reading a type off the model ---------------------------------------------------------------


@pytest.mark.parametrize(
    "declared,expected",
    [
        ("Signer<'info>", "Signer"),
        ("Account<'info, Vault>", "Account"),
        ("AccountLoader<'info, Reserve>", "AccountLoader"),
        ("UncheckedAccount", "UncheckedAccount"),
        ("  Program<'info, System>  ", "Program"),
        # Anchor boxes large accounts, and `Box` is not what constructs one.
        ("Box<Account<'info, Lending>>", "Account"),
    ],
)
def test_a_declared_type_yields_its_constructor(declared: str, expected: str) -> None:
    assert account_constructor(declared) == expected


@pytest.mark.parametrize("declared", ["a PDA of some seeds", "pda", "", "the token mint"])
def test_prose_in_the_type_field_yields_no_constructor(declared: str) -> None:
    """The analysis is free to answer with prose. Inventing a wrapper from it would put a name in
    the prompt that the compiler rejects, so the renderer shows the prose instead."""
    assert account_constructor(declared) is None


@pytest.mark.parametrize(
    "declared,expected",
    [
        ("amount: u64", Param("amount", "u64")),
        ("  shares : u128 ", Param("shares", "u128")),
        ("`amount`: `u64`", Param("amount", "u64")),
        ("seeds: Vec<u8>", Param("seeds", "Vec<u8>")),
    ],
)
def test_an_argument_splits_into_name_and_type(declared: str, expected: Param) -> None:
    assert split_arg(declared) == expected


@pytest.mark.parametrize("declared", ["amount", "the deposit amount in lamports", "u64 amount"])
def test_an_argument_that_does_not_split_is_rejected(declared: str) -> None:
    assert split_arg(declared) is None


# -- choosing the handler -----------------------------------------------------------------------


def test_the_handler_the_properties_name_is_the_one_rendered() -> None:
    component = _component(DEPOSIT, WITHDRAW)
    props = [_prop("shares_on_withdraw", "Withdrawing burns shares at the current rate.")]

    example = worked_example(component, props)

    assert example is not None
    assert example.handler == "withdraw"


def test_a_handler_named_with_spaces_still_counts_as_mentioned() -> None:
    """Properties are prose: they say "deposit reserve liquidity", not the snake_case symbol."""
    component = _component(WITHDRAW, DEPOSIT)
    props = [_prop("supply", "A deposit reserve liquidity call credits the depositor's position.")]

    example = worked_example(component, props)

    assert example is not None
    assert example.handler == "deposit_reserve_liquidity"


def test_no_mention_falls_back_to_declared_order() -> None:
    component = _component(WITHDRAW, DEPOSIT)

    example = worked_example(component, [_prop("solvency", "Assets cover liabilities.")])

    assert example is not None
    assert example.handler == "withdraw"


def test_an_unrenderable_handler_is_skipped_rather_than_failing_the_substitution() -> None:
    """A handler whose recorded arguments do not split cannot be written as a call, but the
    component's other handlers still teach the mechanics."""
    unparseable = _instruction(
        "redeem", accounts=[_account("lending", "Account<'info, Lending>")], args=["the amount"]
    )
    component = _component(unparseable, DEPOSIT)

    example = worked_example(component, [_prop("redeem_rate", "Redeem burns at the rate.")])

    assert example is not None
    assert example.handler == "deposit_reserve_liquidity"


def test_a_component_with_nothing_renderable_yields_no_example() -> None:
    component = _component(_instruction("initialize", accounts=[]))

    assert worked_example(component, []) is None


def test_the_struct_name_follows_anchors_convention() -> None:
    example = worked_example(_component(DEPOSIT), [])

    assert example is not None
    assert example.accounts_struct == "DepositReserveLiquidity"
    assert example.program_module == "lending"


# -- rendering ----------------------------------------------------------------------------------


def _render(example: WorkedExample | None, conf: dict | None = None) -> str:
    return env.get_template(TEMPLATE).render(
        module="exchange_rate",
        cvlr_versions="cvlr 0.6.1",
        example=example,
        conf=dict(TEMPLATE_BASE) if conf is None else conf,
    )


def test_the_rendered_example_names_this_program() -> None:
    rendered = _render(worked_example(_component(DEPOSIT), []))

    assert "crate::DepositReserveLiquidity {" in rendered
    assert "lending: Account::try_from(&accounts[0]).unwrap()," in rendered
    assert "depositor: Signer::try_from(&accounts[1]).unwrap()," in rendered
    assert "let amount: u64 = nondet();" in rendered
    assert "crate::lending::deposit_reserve_liquidity(ctx, amount).unwrap();" in rendered
    # The stand-in program is gone, not merely appended to.
    assert "vault_program" not in rendered


def test_an_account_with_no_constructor_renders_as_the_note_it_is() -> None:
    component = _component(
        _instruction(
            "settle",
            accounts=[_account("position", "a PDA of (b\"position\", user)")],
            args=[],
        )
    )

    rendered = _render(worked_example(component, []))

    assert 'position: /* recorded as "a PDA of (b"position", user)", not a type' in rendered


def test_a_declared_type_cannot_close_the_comment_it_is_rendered_in() -> None:
    """`*/` in the analysis's free text would otherwise end the comment and spill prose into the
    example as code."""
    component = _component(
        _instruction("settle", accounts=[_account("position", "unknown */ leak")], args=[])
    )

    example = worked_example(component, [])

    assert example is not None
    assert "*/" not in example.accounts[0].declared
    assert "leak" not in _render(example).split("```rust")[1].split("*/")[0]


def test_no_component_keeps_the_stand_in_example() -> None:
    """A run with no analyzed component still needs a worked example, and the prompt's own is it."""
    rendered = _render(None)

    assert "crate::vault_program::deposit(ctx, amount).unwrap();" in rendered
    assert "The names in that example are the example's" in rendered


def test_the_substituted_branch_states_what_the_model_could_not_know() -> None:
    """The struct name is Anchor's convention rather than a fact, and a green example that hid that
    would send the author at a name the compiler rejects with nothing saying why."""
    rendered = _render(worked_example(_component(DEPOSIT), []))

    assert "Anchor's" in rendered and "PascalCase" in rendered
    assert "The names in that example are the example's" not in rendered


def test_both_branches_keep_the_shared_mechanics() -> None:
    for example in (worked_example(_component(DEPOSIT), []), None):
        rendered = _render(example)
        assert "The context borrows the\naccounts struct" in rendered
        assert "A zero-copy `AccountLoader` field behaves differently" in rendered
        # Anchor's naming rules belong to Anchor, not to whether we could read this program.
        assert "`<AccountsStruct>Bumps`" in rendered


# -- the arithmetic guidance ---------------------------------------------------------------------


def test_the_prompt_connects_the_timeout_symptom_to_its_two_causes() -> None:
    """The run this was written for died twice on nonlinear arithmetic with nothing in the prompt
    naming the symptom, so the author had the remedy and no reason to reach for it."""
    rendered = _render(None)

    assert "nonlinear" in rendered.lower()
    # The symptom as the job reports it.
    assert "HALTs" in rendered or "HALT" in rendered
    # The remedy for each of the two sources.
    assert "redirect_module" in rendered
    assert "cvlr_lemma!" in rendered and ".apply(&ctx)" in rendered


def test_the_prompt_does_not_claim_native_int_removes_nonlinearity() -> None:
    """`NativeInt` removes the bit width, not the multiplication — an author told otherwise would
    convert the rule and resubmit into the same timeout."""
    rendered = _render(None)

    assert "`NativeInt` does not make a product linear" in rendered


def test_bounding_is_taught_as_soundness_rather_than_speed() -> None:
    """An author who reads it as a performance tip skips it under time pressure, and the run that
    did got a counterexample at ~2.7e62 that it could not tell from a real defect."""
    rendered = _render(None)

    assert "soundness step, not a speed one" in rendered
    # And the trap is named where the widening is introduced, not only where it bites.
    assert "one move, not two" in rendered


def test_the_judge_is_told_to_recognise_the_lemma_shape_not_the_macro() -> None:
    """The author who first used the discipline hand-rolled it — assert the hypotheses, assume the
    conclusion — and never wrote `cvlr_lemma!`. A judge keyed on the macro sees no lemma at all."""
    rendered = env.get_template("cvlr_property_judge_system_prompt.j2").render(
        cvlr_versions="cvlr 0.6.1"
    )

    assert "Judge the shape, not the macro" in rendered
    assert "cvlr_assert!" in rendered and "cvlr_assume!" in rendered


# -- the conf the author works under --------------------------------------------------------------


def test_the_author_is_shown_the_conf_rather_than_told_about_it() -> None:
    """Three of its settings used to be paraphrased in hand-written prose, which could drift from
    the dict and did not say what the loop bound *is* — while the same section asked the author to
    report when the bound was too low."""
    rendered = _render(None, {"loop_iter": "7", "prover_args": ["-solanaTACMathInt true"]})

    assert '"loop_iter": "7"' in rendered
    assert "-solanaTACMathInt true" in rendered
    # And the old paraphrase, which asserted a bound the conf may not carry, is gone.
    assert "unrolls loops a fixed number of times" not in rendered


def test_a_conf_without_rule_sanity_is_not_described_as_having_it() -> None:
    """The paraphrase said "keep `rule_sanity` on (the conf does)" unconditionally. A project conf
    that omits it gets no vacuity report, and an author told otherwise trusts a green rule."""
    rendered = _render(None, {"loop_iter": "2"})

    assert "(the conf does)" not in rendered
    assert "check yours below" in rendered


def test_the_judge_is_told_a_verified_lemma_is_not_an_over_assumption() -> None:
    """`.apply()` ends by assuming its conclusion, which reads like the thing the judge exists to
    reject; unflagged, the mechanism is unusable."""
    rendered = env.get_template("cvlr_property_judge_system_prompt.j2").render(
        cvlr_versions="cvlr 0.6.1"
    )

    assert "cvlr_lemma!" in rendered
    assert ".verify()" in rendered
