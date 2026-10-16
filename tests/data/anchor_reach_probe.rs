//! Probe for prover error [3006], graded by how much of Anchor each rule goes through.
//!
//! Hand-written. Each rule calls the program under verification.

use anchor_lang::prelude::*;
use cvlr::prelude::*;
use cvlr_solana::{cvlr_deserialize_nondet_accounts, cvlr_nondet_pubkey};

use crate::{vault_program, VaultState};

/// Tier 3: Anchor dispatch through `entry`, `try_accounts`, and `Account::try_from`.
///
/// Asserts nothing about the result. A [3006] here is in dispatch, and a
/// property written on top of this path cannot be reached.
#[rule]
pub fn rule_dispatch_is_reachable() {
    let accounts = cvlr_deserialize_nondet_accounts();
    let program_id: Pubkey = cvlr_nondet_pubkey();
    // Heap allocation. The prover does not fully support stack arrays, and this
    // instruction data is unconstrained.
    let data: Vec<u8> = (0..16).map(|_| nondet::<u8>()).collect();
    let _ = crate::entry(&program_id, &accounts, &data);
    cvlr_assert!(true);
}

/// Tier 2: the handler, with the accounts struct built by hand so `try_accounts` is skipped.
///
/// Deposit's post-state: the recorded balance grows by `amount`.
///
/// Read the post-state through the handle the context borrowed. Anchor's
/// `Account<T>` is a deserialized copy. A handler's mutation of it reaches the
/// account bytes only in `exit`, and a rule that calls the handler directly
/// never calls `exit`. Re-deserializing after the call reads the pre-state,
/// and the property fails on a correct program.
#[rule]
pub fn rule_deposit_credits_exactly_the_amount() {
    // Heap allocation. The prover does not fully support stack arrays, and this one is large.
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let vault_info = &accounts[0];
    let depositor_info = &accounts[1];
    let system_info = &accounts[2];

    let mut deposit = crate::Deposit {
        vault: Account::try_from(vault_info).unwrap(),
        depositor: Signer::try_from(depositor_info).unwrap(),
        system_program: Program::try_from(system_info).unwrap(),
    };

    let before = deposit.vault.balance;
    let amount: u64 = nondet();
    cvlr_assume!(before.checked_add(amount).is_some());

    let ctx = Context::new(&crate::ID, &mut deposit, &[], Default::default());
    if vault_program::deposit(ctx, amount).is_ok() {
        cvlr_assert!(deposit.vault.balance == before + amount);
    }
}

/// Tier 1: no Anchor dispatch and no CPI. Read the account and do the handler's arithmetic.
///
/// The control. If this fails, the problem is not Anchor's. If it is the only
/// one that passes, the boundary is where Anchor begins.
#[rule]
pub fn rule_vault_state_deserializes() {
    let accounts = cvlr_deserialize_nondet_accounts();
    let vault: Account<VaultState> = Account::try_from(&accounts[0]).unwrap();
    let amount: u64 = nondet();
    cvlr_assume!(vault.balance.checked_add(amount).is_some());
    cvlr_assert!(vault.balance + amount >= vault.balance);
}
