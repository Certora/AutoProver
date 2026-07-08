//! A minimal Anchor "lamports vault" program.
//!
//! A user creates a vault (a PDA) with themselves as the authority, deposits lamports into it,
//! and later withdraws. Only the vault's authority may withdraw. Illustrative — not audited.

#![allow(unexpected_cfgs)]
use anchor_lang::prelude::*;
use anchor_lang::prelude::program::invoke;
use anchor_lang::solana_program::system_instruction;

declare_id!("BdmwBcVB95UpLzXFwqRnbeJBsrMDLKB4sgJb123oxUoj");

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
        let vault = &mut ctx.accounts.vault;
        vault.balance = vault.balance.checked_add(amount).ok_or(VaultError::Overflow)?;
        Ok(())
    }

    /// Withdraw `amount` lamports from the vault to the authority. Only the authority may call.
    pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
        let vault = &mut ctx.accounts.vault;
        require!(amount <= vault.balance, VaultError::InsufficientFunds);

        **vault.to_account_info().try_borrow_mut_lamports()? -= amount;
        **ctx.accounts.authority.to_account_info().try_borrow_mut_lamports()? += amount;
        vault.balance = vault.balance.checked_sub(amount).ok_or(VaultError::Overflow)?;
        Ok(())
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
}

#[error_code]
pub enum VaultError {
    #[msg("arithmetic overflow")]
    Overflow,
    #[msg("insufficient funds in vault")]
    InsufficientFunds,
}
