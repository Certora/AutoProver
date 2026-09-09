"""Reading the target's Anchor-generated surface off its source.

``docs/cvlr-backend-plan.md`` §7.5.5: a draft named `crate::__client_accounts_withdraw::WithdrawBumps`
because nothing put the generated names in front of it. These cover the four naming rules, the
scanning that finds the declarations they apply to, and the end-to-end read of the repo's own Anchor
scenario — which is the check that says the scanner survives real source rather than fixtures shaped
to suit it.
"""

from pathlib import Path

import pytest

from composer.spec.cvlr.anchor_surface import (
    AccountsStruct,
    Param,
    context_accounts_type,
    read_surface,
    signature_params,
    source_files,
)
from composer.spec.cvlr.example import worked_example
from composer.spec.solana.model import (
    AccountConstraint,
    ProgramComponent,
    SolanaApplication,
    SolanaComponentInstance,
    SolanaInstruction,
    SolanaProgram,
    SolanaProgramInstance,
)
from composer.spec.types import ProgramName, RustIdentifier
from composer.templates.loader import env

#: The repo's own Anchor scenario, used as the realistic end of these tests.
VAULT = Path(__file__).parent.parent / "test_scenarios" / "solana_vault_idl" / "programs" / "vault"

PROGRAM = '''
use anchor_lang::prelude::*;

#[program]
pub mod lending_program {
    use super::*;

    /// Not a `struct Decoy` — this is a doc comment.
    pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
        Ok(())
    }

    pub fn settle<'info>(
        ctx: Context<'_, '_, '_, 'info, Settle<'info>>,
        shares: u64,
        limit: Option<u64>,
    ) -> Result<()> {
        Ok(())
    }

    pub fn refresh(ctx: Context<Settle>) -> Result<()> { Ok(()) }
}

#[derive(Accounts)]
#[instruction(amount: u64)]
pub struct Withdraw<'info> {
    #[account(mut, has_one = authority)]
    pub lending: Box<Account<'info, Lending>>,

    /// CHECK: verified in the CPI
    #[account(mut)]
    pub authority: Signer<'info>,

    pub fee_collector: Option<UncheckedAccount<'info>>,
}

#[derive(Accounts, Debug)]
pub struct Settle<'info> {
    pub position: AccountLoader<'info, Position>,
}

#[derive(Clone)]
pub struct NotAContext {
    pub ignored: u8,
}
'''


def _write(tmp_path: Path, source: str = PROGRAM) -> Path:
    package = tmp_path / "programs" / "lending"
    (package / "src").mkdir(parents=True)
    (package / "src" / "lib.rs").write_text(source)
    return package


# -- the four naming rules ------------------------------------------------------------------------


def test_the_generated_names_follow_anchors_rules() -> None:
    """`anchor-syn` 0.31.1: `{Ident}Bumps` beside the struct, `__client_accounts_{snake}` and
    `__cpi_client_accounts_{snake}` for the client structs."""
    struct = AccountsStruct(name="DepositReserveLiquidity", declared_in="src/lib.rs", fields=())

    assert struct.bumps_type == "DepositReserveLiquidityBumps"
    assert struct.client_accounts_module == "__client_accounts_deposit_reserve_liquidity"
    assert struct.cpi_client_accounts_module == "__cpi_client_accounts_deposit_reserve_liquidity"


def test_the_discriminant_is_the_handlers_name_in_pascal_case(tmp_path: Path) -> None:
    surface = read_surface(_write(tmp_path))
    handler = surface.handler("withdraw")

    assert handler is not None
    assert handler.discriminant_path == "instruction::Withdraw::DISCRIMINATOR"


# -- finding the declarations the rules apply to --------------------------------------------------


def test_an_accounts_struct_is_read_with_its_fields_and_declared_types(tmp_path: Path) -> None:
    surface = read_surface(_write(tmp_path))
    struct = surface.struct("Withdraw")

    assert struct is not None
    assert [f.name for f in struct.fields] == ["lending", "authority", "fee_collector"]
    # Multi-line `#[account(..)]`, a `/// CHECK:` comment, and a comma inside nested generics.
    assert struct.fields[0].rust_type == "Box<Account<'info, Lending>>"
    assert struct.fields[2].rust_type == "Option<UncheckedAccount<'info>>"


def test_a_struct_without_the_accounts_derive_is_not_one(tmp_path: Path) -> None:
    surface = read_surface(_write(tmp_path))

    assert surface.struct("NotAContext") is None
    assert {s.name for s in surface.structs} == {"Withdraw", "Settle"}


def test_the_program_module_is_the_one_the_attribute_marks(tmp_path: Path) -> None:
    """It is not the crate name: the scenario's crate is `vault` and its `#[program]` module is
    `vault_program`, which is the half a `crate::…::handler` path needs."""
    surface = read_surface(_write(tmp_path))

    assert surface.program_module == "lending_program"


def test_a_handler_resolves_to_the_struct_its_context_names(tmp_path: Path) -> None:
    surface = read_surface(_write(tmp_path))

    assert surface.accounts_for("withdraw") is not None
    assert surface.accounts_for("withdraw").name == "Withdraw"  # type: ignore[union-attr]
    # Two handlers may share one accounts struct.
    assert surface.accounts_for("refresh").name == "Settle"  # type: ignore[union-attr]


def test_a_handler_naming_a_struct_this_package_does_not_declare_resolves_to_nothing(
    tmp_path: Path,
) -> None:
    """A half-answer is worse than none: the caller asks precisely so it can stop guessing."""
    source = PROGRAM.replace("pub fn withdraw(ctx: Context<Withdraw>", "pub fn withdraw(ctx: Context<Elsewhere>")
    surface = read_surface(_write(tmp_path, source))

    assert surface.handler("withdraw") is not None
    assert surface.accounts_for("withdraw") is None


@pytest.mark.parametrize(
    "signature,expected",
    [
        ("(ctx: Context<Withdraw>, amount: u64)", "Withdraw"),
        ("(ctx: Context<'_, '_, '_, 'info, Settle<'info>>)", "Settle"),
        ("(ctx: Context < Deposit >, amount: u64)", "Deposit"),
        ("(amount: u64)", None),
    ],
)
def test_the_accounts_type_is_the_contexts_last_type_argument(
    signature: str, expected: str | None
) -> None:
    """Anchor spells the parameter with or without its lifetimes, and the accounts type is last in
    both — taken positionally rather than by counting lifetimes."""
    assert context_accounts_type(signature) == expected


def test_a_handlers_arguments_come_from_its_own_declaration() -> None:
    signature = "(ctx: Context<Settle>, shares: u64, limit: Option<u64>)"

    assert signature_params(signature) == (Param("shares", "u64"), Param("limit", "Option<u64>"))


def test_a_handler_taking_only_a_context_has_no_arguments() -> None:
    assert signature_params("(ctx: Context<Settle>)") == ()


def test_the_harness_this_run_writes_is_not_the_programs_surface(tmp_path: Path) -> None:
    """`certora/` holds this run's specs and any mocks the editor authored. A mock's accounts struct
    is not something the author should be told the program declares."""
    package = _write(tmp_path)
    mocks = package / "src" / "certora" / "mocks"
    mocks.mkdir(parents=True)
    (mocks / "context.rs").write_text(
        "#[derive(Accounts)]\npub struct Mocked<'info> { pub a: Signer<'info> }\n"
    )

    surface = read_surface(package)

    assert surface.struct("Mocked") is None
    assert not any("certora" in p.parts for p in source_files(package))


def _component(instruction: SolanaInstruction) -> SolanaComponentInstance:
    program = SolanaProgram(
        name=ProgramName("Lending"),
        program_identifier=RustIdentifier("lending"),
        description="A lending program.",
        instructions=[instruction],
        components=[
            ProgramComponent(
                name="Withdrawals",
                description="Taking liquidity out.",
                instructions=[instruction.name],
                account_types=["Lending"],
                interactions=[],
                requirements=[],
            )
        ],
    )
    return SolanaComponentInstance(
        0,
        SolanaProgramInstance(
            0,
            SolanaApplication(
                application_type="Lending", description="A market.", components=[program]
            ),
        ),
    )


def _model_withdraw() -> SolanaInstruction:
    """The analysis's reading of `withdraw`: one account under a name the source does not use, the
    other two missed, and the crate name where the `#[program]` module belongs."""
    return SolanaInstruction(
        name="withdraw",
        description="Withdraws.",
        accounts=[AccountConstraint(name="vault", account_type="Account<Vault>")],
        args=["amount: u64"],
        requirements=[],
    )


# -- what the author is told ----------------------------------------------------------------------


def _render(example) -> str:
    return env.get_template("cvlr_property_generation_system_prompt.j2").render(
        module="withdrawals", cvlr_versions="cvlr 0.6.1", example=example
    )


def test_the_prompt_rules_out_the_module_the_failing_draft_reached_for(tmp_path: Path) -> None:
    """The draft on record wrote `crate::__client_accounts_withdraw::WithdrawBumps`. Both halves of
    that mistake are named where the author will be reading."""
    surface = read_surface(_write(tmp_path))
    rendered = _render(worked_example(_component(_model_withdraw()), [], surface))

    assert "`WithdrawBumps`" in rendered
    assert "__client_accounts_withdraw" in rendered
    assert "Nothing a rule builds" in rendered


def test_the_prompt_states_the_relationship_rather_than_a_derived_path(tmp_path: Path) -> None:
    """A module path derived from the file would be a fresh guess — a private `mod` with a
    `pub use` would make it wrong. The relationship holds however the struct is reached."""
    surface = read_surface(_write(tmp_path))
    rendered = _render(worked_example(_component(_model_withdraw()), [], surface))

    assert "whatever path reaches the struct reaches it" in rendered
    assert "always valid" in rendered


def test_the_generated_names_are_stated_even_with_no_example() -> None:
    """The rules are Anchor's, not this program's: a run with no analyzed component still needs
    them, since the trap is the same one."""
    rendered = _render(None)

    assert "__client_accounts_<snake>" in rendered
    assert "instruction::<Handler in PascalCase>::DISCRIMINATOR" in rendered


# -- the worked example prefers the source ---------------------------------------------------------


def test_the_example_prefers_the_source_over_the_models_reading(tmp_path: Path) -> None:
    """The model is asked for the same facts and gets some of them wrong. Here it records one
    account under a different name and misses another entirely, and the analysis's `program_
    identifier` is the crate rather than the `#[program]` module."""
    surface = read_surface(_write(tmp_path))

    example = worked_example(_component(_model_withdraw()), [], surface)

    assert example is not None
    assert example.provenance == "source"
    assert example.accounts_struct == "Withdraw"
    assert example.program_module == "lending_program"
    assert [a.name for a in example.accounts] == ["lending", "authority", "fee_collector"]
    assert example.args == (Param("amount", "u64"),)


def test_a_handler_the_scanner_cannot_follow_falls_back_to_the_model(tmp_path: Path) -> None:
    """Falling back for *that handler* rather than moving to the next one: the batch's properties
    picked it, and the model's reading of it still teaches the mechanics."""
    component = _component(
        SolanaInstruction(
            name="not_in_the_program",
            description="Absent from the source.",
            accounts=[AccountConstraint(name="vault", account_type="Account<Vault>")],
            args=["amount: u64"],
            requirements=[],
        )
    )
    surface = read_surface(_write(tmp_path))

    example = worked_example(component, [], surface)

    assert example is not None
    assert example.provenance == "model"
    assert example.accounts_struct == "NotInTheProgram"


# -- against real source --------------------------------------------------------------------------


@pytest.mark.skipif(not VAULT.exists(), reason="the Anchor scenario is not checked out")
def test_the_repos_own_anchor_scenario_reads_completely() -> None:
    """The end that matters: real source, with every handler resolving to a declared struct. A
    scanner that only survives its own fixtures is not one this can rely on."""
    surface = read_surface(VAULT)

    assert surface.program_module == "vault_program"
    assert {h.name for h in surface.handlers} == {"initialize", "deposit", "withdraw"}
    assert all(surface.accounts_for(h.name) is not None for h in surface.handlers)

    withdraw = surface.accounts_for("withdraw")
    assert withdraw is not None
    assert withdraw.bumps_type == "WithdrawBumps"
    assert [f.name for f in withdraw.fields]
    assert all("<" not in f.name for f in withdraw.fields)
