//! A hand-written probe for prover error [3006], graded by how much of Anchor it goes through.
//!
//! Not written by the authoring loop on purpose: an error the author cannot act on consumes its
//! whole budget, and three runs have shown that measures the loop rather than the hypothesis.
//! Every rule here *reaches the program* — that is the point, and it is what the loop's own output
//! never did.

use anchor_lang::prelude::*;
use cvlr::prelude::*;
use cvlr_solana::{cvlr_deserialize_nondet_accounts, cvlr_nondet_pubkey};

use crate::{vault_program, VaultState};

/// Tier 3: the whole Anchor dispatch path — `entry`, `try_accounts`, `Account::try_from`.
///
/// The most aggressive probe and the one the reported error was attributed to. Asserts nothing
/// about the result: if this reports [3006] then the error is in dispatch, and no property written
/// on top of it can be reached.
#[rule]
pub fn rule_dispatch_is_reachable() {
    let accounts = cvlr_deserialize_nondet_accounts();
    let program_id: Pubkey = cvlr_nondet_pubkey();
    // Heap rather than `[u8; 16]`: the prover does not fully support stack arrays, and the
    // instruction data is the one buffer here whose contents are unconstrained.
    let data: Vec<u8> = (0..16).map(|_| nondet::<u8>()).collect();
    let _ = crate::entry(&program_id, &accounts, &data);
    cvlr_assert!(true);
}

/// Tier 2: the handler, with the accounts struct built by hand so `try_accounts` is skipped.
///
/// Deposit's post-state property: the recorded balance grows by exactly `amount`. This is the
/// shape of property the loop reported as unreachable, and the one that matters commercially.
///
/// Post-state is read **through the handle the context borrowed**, not by re-deserializing the
/// account. That is how the reference project's integrity rules observe a handler's effect, and the
/// distinction is load-bearing: Anchor's `Account<T>` is a deserialized copy, and a handler's
/// mutation of it reaches the account's bytes only in `exit` — which nothing calls when a rule
/// invokes the handler directly. Re-deserializing therefore reads the *pre-state* and the property
/// fails against a correct program. Measured: it did.
#[rule]
pub fn rule_deposit_credits_exactly_the_amount() {
    // Heaped, as the reference project heaps its own account array: the prover does not fully
    // support stack-allocated arrays, and this one is large.
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

/// Tier 1: no Anchor dispatch and no CPI — read the account, do the arithmetic the handler does.
///
/// The control. If this fails too, the problem is not Anchor's; if it is the only one that passes,
/// the boundary is exactly where Anchor begins.
#[rule]
pub fn rule_vault_state_deserializes() {
    let accounts = cvlr_deserialize_nondet_accounts();
    let vault: Account<VaultState> = Account::try_from(&accounts[0]).unwrap();
    let amount: u64 = nondet();
    cvlr_assume!(vault.balance.checked_add(amount).is_some());
    cvlr_assert!(vault.balance + amount >= vault.balance);
}
