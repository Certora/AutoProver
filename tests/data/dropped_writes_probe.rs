//! A hand-written probe for writes the Prover's model may drop: `realloc` under the starting
//! layer's summary, and a lamport transfer by CPI.
//!
//! Each call is probed by a pair of rules. The **canary** asserts that the call changed nothing it
//! really changes, which is false of the real program, so a VERIFIED canary means the model
//! dropped the write. Its **companion** asserts what the call really does, so it is VERIFIED only
//! when the model kept the write. Sanity checking stays on, so a pair that passes only because its
//! branch is unreachable is reported as vacuous rather than as a verdict.

use anchor_lang::prelude::*;
use anchor_lang::solana_program::program::invoke;
use anchor_lang::solana_program::system_instruction;
use cvlr::prelude::*;
use cvlr_solana::cvlr_deserialize_nondet_accounts;

/// The largest growth `realloc` permits in one instruction is 10 KiB; stay well inside it.
const GROWTH: usize = 1024;

/// Grow `info` by a nondet amount, returning the old and new lengths on success.
fn grown_by(
    info: &AccountInfo,
    grow: impl FnOnce(&AccountInfo, usize) -> std::result::Result<(), ProgramError>,
) -> Option<(usize, usize)> {
    let old = info.data_len();
    let new_len: usize = nondet();
    cvlr_assume!(new_len > old && new_len <= old + GROWTH);
    grow(info, new_len).is_ok().then_some((old, new_len))
}

fn grown(info: &AccountInfo) -> Option<(usize, usize)> {
    #[allow(deprecated)]
    grown_by(info, |info, n| info.realloc(n, false))
}

/// `resize` is `realloc(n, true)`: the same writes plus a zero-fill of the grown tail.
fn resized(info: &AccountInfo) -> Option<(usize, usize)> {
    grown_by(info, |info, n| info.resize(n))
}

#[rule]
pub fn rule_canary_resize_keeps_the_old_length() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let info = &accounts[0];
    if let Some((old, _)) = resized(info) {
        clog!(old, info.data_len());
        cvlr_assert!(info.data_len() == old);
    }
}

#[rule]
pub fn rule_resize_sets_the_new_length() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let info = &accounts[0];
    if let Some((_, new_len)) = resized(info) {
        clog!(new_len, info.data_len());
        cvlr_assert!(info.data_len() == new_len);
    }
}

#[rule]
pub fn rule_resize_can_succeed() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    cvlr_satisfy!(resized(&accounts[0]).is_some());
}

#[rule]
pub fn rule_canary_realloc_keeps_the_old_length() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let info = &accounts[0];
    if let Some((old, _)) = grown(info) {
        clog!(old, info.data_len());
        cvlr_assert!(info.data_len() == old);
    }
}

#[rule]
pub fn rule_realloc_sets_the_new_length() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let info = &accounts[0];
    if let Some((_, new_len)) = grown(info) {
        clog!(new_len, info.data_len());
        cvlr_assert!(info.data_len() == new_len);
    }
}

/// A transfer between two distinct accounts, and the payer's lamports before it.
fn transfer_setup<'a, 'info>(
    accounts: &'a [AccountInfo<'info>],
) -> (&'a AccountInfo<'info>, &'a AccountInfo<'info>, &'a AccountInfo<'info>, u64, u64) {
    let (from, to, system) = (&accounts[0], &accounts[1], &accounts[2]);
    cvlr_assume!(from.key != to.key);
    let amount: u64 = nondet();
    cvlr_assume!(amount > 0);
    let before = from.lamports();
    (from, to, system, amount, before)
}

fn invoke_transfer<'info>(
    from: &AccountInfo<'info>,
    to: &AccountInfo<'info>,
    system: &AccountInfo<'info>,
    amount: u64,
) -> bool {
    invoke(
        &system_instruction::transfer(from.key, to.key, amount),
        &[from.clone(), to.clone(), system.clone()],
    )
    .is_ok()
}

fn anchor_transfer<'info>(
    from: &AccountInfo<'info>,
    to: &AccountInfo<'info>,
    system: &AccountInfo<'info>,
    amount: u64,
) -> bool {
    let cpi = CpiContext::new(
        system.clone(),
        anchor_lang::system_program::Transfer { from: from.clone(), to: to.clone() },
    );
    anchor_lang::system_program::transfer(cpi, amount).is_ok()
}

#[rule]
pub fn rule_canary_invoke_transfer_moves_nothing() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let (from, to, system, amount, before) = transfer_setup(&accounts[..]);
    if invoke_transfer(from, to, system, amount) {
        clog!(amount, before, from.lamports());
        cvlr_assert!(from.lamports() == before);
    }
}

#[rule]
pub fn rule_invoke_transfer_debits_the_payer() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let (from, to, system, amount, before) = transfer_setup(&accounts[..]);
    if invoke_transfer(from, to, system, amount) {
        clog!(amount, before, from.lamports());
        cvlr_assert!(from.lamports() == before.wrapping_sub(amount));
    }
}

#[rule]
pub fn rule_canary_anchor_transfer_moves_nothing() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let (from, to, system, amount, before) = transfer_setup(&accounts[..]);
    if anchor_transfer(from, to, system, amount) {
        clog!(amount, before, from.lamports());
        cvlr_assert!(from.lamports() == before);
    }
}

#[rule]
pub fn rule_anchor_transfer_debits_the_payer() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let (from, to, system, amount, before) = transfer_setup(&accounts[..]);
    if anchor_transfer(from, to, system, amount) {
        clog!(amount, before, from.lamports());
        cvlr_assert!(from.lamports() == before.wrapping_sub(amount));
    }
}

// Reachability of each success branch. A canary and its companion cannot both be VERIFIED unless
// the branch they assert in is unreachable, and sanity checking does not catch that: the rule's
// end is still reached through the failure branch. These say which it is.

#[rule]
pub fn rule_realloc_can_succeed() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    cvlr_satisfy!(grown(&accounts[0]).is_some());
}

#[rule]
pub fn rule_invoke_transfer_can_succeed() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let (from, to, system, amount, _) = transfer_setup(&accounts[..]);
    cvlr_satisfy!(invoke_transfer(from, to, system, amount));
}

#[rule]
pub fn rule_anchor_transfer_can_succeed() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let (from, to, system, amount, _) = transfer_setup(&accounts[..]);
    cvlr_satisfy!(anchor_transfer(from, to, system, amount));
}

// Does a CPI disturb the caller's deserialized `Account<T>`? It cannot in the real program: the
// deserialized copy is the caller's own memory, and a CPI reaches only the account bytes. The
// author's prompt said the Prover's CPI stand-in havocs it. Through the `deposit` handler, where
// that was first observed, this program cannot be analyzed at all ([3308] in its `#[error_code]`
// formatting, inlined where no summary reaches), so the question is asked directly.

#[rule]
pub fn rule_account_field_survives_an_invoke() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let vault: Account<crate::VaultState> = Account::try_from(&accounts[0]).unwrap();
    let before = vault.balance;
    let (from, to, system) = (&accounts[1], &accounts[0], &accounts[2]);
    cvlr_assume!(from.key != to.key);
    let amount: u64 = nondet();
    cvlr_assume!(amount > 0);
    if invoke_transfer(from, to, system, amount) {
        clog!(before, vault.balance);
        cvlr_assert!(vault.balance == before);
    }
}

#[rule]
pub fn rule_account_field_across_an_invoke_is_reachable() {
    let accounts = Box::new(cvlr_deserialize_nondet_accounts());
    let _vault: Account<crate::VaultState> = Account::try_from(&accounts[0]).unwrap();
    let (from, to, system) = (&accounts[1], &accounts[0], &accounts[2]);
    cvlr_assume!(from.key != to.key);
    let amount: u64 = nondet();
    cvlr_assume!(amount > 0);
    cvlr_satisfy!(invoke_transfer(from, to, system, amount));
}
