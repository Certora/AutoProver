//! A minimal Anchor "lamports vault" program, on an Anchor major older than the harness stack's.
//!
//! Deliberately *not* the same Anchor as `crucible-app` links (see this scenario's `Cargo.toml`), so
//! the harness cannot depend on this crate and must generate its types from the IDL instead — the
//! path every real target on a pre-1.0 Anchor takes.
//!
//! `withdraw` carries an **optional account** for the same reason: an absent optional is the one
//! place the two paths disagree, since Anchor's own client codegen transmits it as the program's id
//! while `crucible-idl-gen` drops it. Illustrative — not audited.

#![allow(unexpected_cfgs)]
use anchor_lang::prelude::*;
use anchor_lang::solana_program::program::invoke;
use anchor_lang::solana_program::system_instruction;

declare_id!("Vau1t1DL9gWKUmvBrGnPLp2MJNRnJ8mE1qYCkkTgkA5");

/// Basis points of a withdrawal diverted to the fee collector, when one is supplied.
const FEE_BPS: u64 = 100;

#[program]
pub mod vault_program {
    use super::*;

    /// Create a vault PDA owned by `authority`. Fails if the vault already exists.
    pub fn initialize(ctx: Context<Initialize>) -> Result<()> {
        let vault = &mut ctx.accounts.vault;
        vault.authority = ctx.accounts.authority.key();
        vault.balance = 0;
        vault.bump = ctx.bumps.vault;
        Ok(())
    }

    /// Deposit `amount` lamports from the depositor into the vault PDA.
    pub fn deposit(ctx: Context<Deposit>, amount: u64) -> Result<()> {
        invoke(
            &system_instruction::transfer(
                &ctx.accounts.depositor.key(),
                &ctx.accounts.vault.key(),
                amount,
            ),
            &[
                ctx.accounts.depositor.to_account_info(),
                ctx.accounts.vault.to_account_info(),
                ctx.accounts.system_program.to_account_info(),
            ],
        )?;
        vault_accounting::apply_deposit(&mut ctx.accounts.vault, amount)
    }

    /// Withdraw `amount` lamports from the vault. Only the authority may call.
    ///
    /// When a `fee_collector` is supplied, `FEE_BPS` of the withdrawal goes to it and the rest to
    /// the authority; with no collector the authority receives all of it. Either way the vault
    /// gives up exactly `amount`.
    pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
        let with_collector = ctx.accounts.fee_collector.is_some();
        let split = vault_accounting::apply_withdrawal(
            &mut ctx.accounts.vault,
            amount,
            with_collector,
        )?;

        let vault_info = ctx.accounts.vault.to_account_info();
        **vault_info.try_borrow_mut_lamports()? -= amount;
        **ctx.accounts.authority.to_account_info().try_borrow_mut_lamports()? +=
            split.to_authority;
        if let Some(collector) = &ctx.accounts.fee_collector {
            **collector.to_account_info().try_borrow_mut_lamports()? += split.fee;
        }
        Ok(())
    }
}

/// The vault's accounting core — the state transitions the handlers wrap.
///
/// Deliberately free of `Context` and `AccountInfo`: everything these functions read arrives as a
/// parameter and everything they produce leaves as a return value, so the accounting can be
/// exercised without materializing accounts. Real Anchor programs of any size are organized this
/// way for their own reasons, and it is also what makes the balance properties reachable when a CPI
/// in the enclosing handler leaves the deserialized account unconstrained.
pub mod vault_accounting {
    use super::{VaultError, VaultState, FEE_BPS};
    use anchor_lang::prelude::*;

    /// How a withdrawal of `amount` divides between the authority and the fee collector.
    pub struct Split {
        pub to_authority: u64,
        pub fee: u64,
    }

    /// Credit `amount` to the vault's recorded balance.
    pub fn apply_deposit(state: &mut VaultState, amount: u64) -> Result<()> {
        state.balance = state.balance.checked_add(amount).ok_or(VaultError::Overflow)?;
        Ok(())
    }

    /// Debit `amount` from the recorded balance and say how it divides.
    ///
    /// `with_collector` is whether the caller supplied a fee sink; the fee is `FEE_BPS` of the
    /// withdrawal when it did and zero when it did not. The vault gives up `amount` either way.
    pub fn apply_withdrawal(
        state: &mut VaultState,
        amount: u64,
        with_collector: bool,
    ) -> Result<Split> {
        require!(amount <= state.balance, VaultError::InsufficientFunds);
        let fee = if with_collector { amount * FEE_BPS / 10_000 } else { 0 };
        let to_authority = amount.checked_sub(fee).ok_or(VaultError::Overflow)?;
        state.balance = state.balance.checked_sub(amount).ok_or(VaultError::Overflow)?;
        Ok(Split { to_authority, fee })
    }
}

#[account]
pub struct VaultState {
    /// The only key allowed to withdraw.
    pub authority: Pubkey,
    /// Lamports recorded as deposited (mirrors the PDA's spendable lamports).
    pub balance: u64,
    pub bump: u8,
}

impl VaultState {
    pub const SIZE: usize = 32 + 8 + 1;
}

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(
        init,
        payer = authority,
        space = 8 + VaultState::SIZE,
        seeds = [b"vault", authority.key().as_ref()],
        bump,
    )]
    pub vault: Account<'info, VaultState>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Deposit<'info> {
    #[account(mut, seeds = [b"vault", vault.authority.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, VaultState>,
    #[account(mut)]
    pub depositor: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Withdraw<'info> {
    #[account(
        mut,
        seeds = [b"vault", authority.key().as_ref()],
        bump = vault.bump,
        has_one = authority,
    )]
    pub vault: Account<'info, VaultState>,
    #[account(mut)]
    pub authority: Signer<'info>,
    /// CHECK: an optional fee sink. Only ever credited lamports, so any account is acceptable.
    #[account(mut)]
    pub fee_collector: Option<UncheckedAccount<'info>>,
}

#[error_code]
pub enum VaultError {
    #[msg("arithmetic overflow")]
    Overflow,
    #[msg("insufficient funds in vault")]
    InsufficientFunds,
}
