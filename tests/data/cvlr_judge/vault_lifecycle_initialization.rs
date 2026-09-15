//! Harness for Vault_Lifecycle_Initialization. Written by the CVLR author.
//!
//! ## What these rules drive
//!
//! Every rule drives the program's own code: a handler in `crate::vault_program`, or the
//! accounting core in `crate::vault_accounting` that the handlers wrap. Account structs are
//! built field by field from unconstrained `AccountInfo`s with Anchor's
//! `Account::try_from_unchecked` / `Signer::try_from` / `Program::try_from` constructors,
//! and post-state is read back through the very struct the `Context` borrowed.
//!
//! ## What this batch does and does not establish
//!
//! Only Properties 1 and 2 are fully discharged. Four of the nine properties are skipped
//! outright, with reasons recorded against them. Three more are **partially** discharged:
//!
//! * **Property 4 (`init_stores_canonical_bump`)** is proved *up to* the canonicity of
//!   `ctx.bumps.vault`. `rule_init_stores_validated_bump` shows the handler records exactly
//!   the bump account validation derived, for every bump it could have derived — not zero,
//!   not a constant, not the value already in the account, not some other field. That the
//!   derived bump is canonical for `["vault", vault.authority]` is delegated to Anchor's
//!   `bump` constraint and is **not proved** (fact 1).
//!
//! * **Property 7 (`vault_authority_and_pda_binding_invariant`)** — its **inductive step is
//!   proved, its base case is not**. The vault's address is `*infos[0].key`, which no
//!   handler can write; so once
//!   `address == canonical_pda(["vault", vault.authority], vault.bump)` holds, it is
//!   *preserved* by any handler that leaves `authority` and `bump` alone, which is what
//!   `rule_authority_unchanged_deposit` and `rule_authority_unchanged_withdraw` prove.
//!   With `rule_init_sets_authority_to_signer` (only `initialize` writes `authority`, and it
//!   writes the key of the account bound to the `authority` field) that discharges the whole
//!   authority-immutability clause and the preservation half of the PDA-binding clause. The
//!   **base case is not proved**: that the address `initialize` accepts really is the
//!   canonical PDA of those seeds rests solely on Anchor's `seeds`/`bump` constraint, which
//!   is where the havoced derivation of fact 1 bites.
//!
//! * **Property 8 (`recorded_balance_never_exceeds_vault_lamports`)** has three legs and
//!   only one of them has both sides observed:
//!   - `withdraw` — **fully proved**. `rule_balance_le_lamports_withdraw` reads the vault
//!     account's real lamports cell on both sides of the real handler; there is no CPI in
//!     the way.
//!   - `initialize` — **partial**. `balance` is a `u64`, so `balance <= lamports` reduces to
//!     `0 <= lamports` and carries nothing on its own; the rule is carried by the
//!     lamport-preservation assertion beside it (`initialize` moves none of the vault's
//!     lamports). The "on an account funded for rent" half of the property's init clause is
//!     **not checked** — rent exemption is established by the havoced `init` CPI.
//!   - `deposit` — **partial: accounting half only, lamport side not observed**.
//!     `rule_deposit_core_credits_exact_amount` proves over exact integers that
//!     `apply_deposit` credits *exactly* `amount` (no wrap, no truncation, no double
//!     credit), which is the program's half of the invariant. Nothing in this unit observes
//!     the vault's lamports across a deposit, because that is only observable at the
//!     handler and the handler is unreachable here — see "On the `deposit` leg" below.
//!
//! ## Three facts about this build that shaped the harness
//!
//! 1. `solana_pubkey::Pubkey::find_program_address` and `::create_program_address` carry only
//!    points-to annotations in `cvlr_summaries_core.txt` and fall under
//!    `#[inline(never)] ^solana_[a-z0-9_]*::.*$`: the Prover returns an unconstrained value
//!    from each call rather than modelling PDA derivation as a function of its arguments. A
//!    first prover run confirmed it empirically — a rule that re-derived the vault PDA got a
//!    "bump" outside `0..=255` back. So no rule can relate the derivation Anchor performs
//!    inside its `seeds`/`bump` constraint to one a rule performs. This makes the canonicity
//!    clauses above undischargeable, and `init_vault_is_canonical_pda` is skipped for it.
//!
//! 2. This build's inlining policy is `#[inline(never)] ^.*anchor_lang.*$` with a short
//!    exception list containing `Account<T>::try_from`, `Account<T>::try_from_unchecked`,
//!    `Signer::try_from` and `Program::try_from` but *not* the per-field extractor that
//!    `#[derive(Accounts)]`-generated code calls,
//!    `<anchor_lang::accounts::signer::Signer as anchor_lang::Accounts<B>>::try_accounts`.
//!    That extractor is replaced by an unconstrained stand-in: a run-1 counterexample showed
//!    `Initialize::try_accounts` succeeding with `is_signer == false`, and a run-2
//!    counterexample showed `Withdraw::try_accounts`'s `has_one = authority` comparing two
//!    havoced keys. Contexts are therefore built with the `*::try_from` constructors, which
//!    is the idiom this scaffold's exception list is wired for.
//!    `init_requires_authority_signature` is recorded as **unverified in this unit** for
//!    this reason: the rule that would state it is writable and was run, the fix is one
//!    `#[inline]` line in the package-owned layer `cvlr_inlining_package.txt`, and no tool in
//!    this run — including the code editor, which refused and named the missing kind — can
//!    write it. That directive should be raised with a human: it is the single change that
//!    would turn this attack vector from unverified into checkable, and it would also make
//!    `Withdraw`'s `has_one = authority` drivable.
//!
//! 3. Cross-program invocations are replaced by unconstrained stand-ins
//!    (`solana_cpi::invoke_signed_unchecked` is summarized). That is why
//!    `init_not_twice_for_same_authority` and `init_reachable_for_fresh_authority` — both of
//!    which turn on what the System Program does — are skipped. `withdraw` performs no CPI,
//!    so its rules drive the real handler end to end.
//!
//! ### On the `deposit` leg, and a contradiction with the sibling units
//!
//! The `deposit` rules here are taken at `crate::vault_accounting::apply_deposit` rather
//! than at `crate::vault_program::deposit`, because the handler is not reachable in this
//! unit. That is measured, three times:
//!
//! 1. At the project's `loop_iter = 2`: VIOLATED on "Unwinding condition in a loop", outer
//!    loop live at iteration 3.
//! 2. At `loop_iter = 4`: unwinding again — inner loop terminating at 3, **outer** loop
//!    still live at iteration 5. The bound was returned to 2.
//! 3. Back at `loop_iter = 2`, with the account construction rebuilt to match the sibling
//!    `certora/specs/deposits.rs` harness's `setup_deposit!` exactly —
//!    `Account::try_from_unchecked` for the vault, explicit `owner == crate::ID`,
//!    `data_len() == 8 + VaultState::SIZE` and `is_writable` on it, explicit `is_signer` and
//!    `is_writable` on the depositor, explicit `key == system_program::ID` and `executable`
//!    on slot 2: unwinding again, outer live at 3. This tested and **falsified** the
//!    hypothesis that an unconstrained system-program slot was the cause.
//!
//! The shape is consistent across all three: `solana_program::program::invoke` and
//! `invoke_signed` are `#[inline]` in this build, so `invoke_signed`'s borrow-consistency
//! check is analysed — an outer walk of `instruction.accounts` containing an inner walk of
//! `account_infos`. The inner walk is over the three-element slice the handler passes and is
//! genuinely bounded (it closes at 1 or 3 depending on where the matching key is found). The
//! outer walk is over the `Vec<AccountMeta>` returned by `system_instruction::transfer`,
//! which falls under `#[inline(never)] ^solana_[a-z0-9_]*::.*$` with no summary, so its
//! length is unconstrained and **no `loop_iter` value closes it**. Summarizing the
//! marshalling path is not available either: `invoke`/`invoke_signed` are on the inlining
//! *exception* list, and a points-to summary does not apply to a symbol the policy inlines.
//!
//! **Unresolved between units, and a reader of the three reports should know it.** The
//! sibling harnesses `certora/specs/deposits.rs` and
//! `certora/specs/withdrawals_fee_sharing.rs` declare rules that drive
//! `crate::vault_program::deposit` under the same conf. I could not reproduce a drivable
//! handler here even after adopting their construction verbatim, and I have no visibility
//! into their verdicts — so this unit and those units make opposite claims about
//! reachability. Treat it as unresolved rather than settled; it is worth a human comparing
//! the three runs.
//!
//! One further point, attributed rather than measured: the program's own documentation on
//! `crate::vault_accounting` says the accounting core is "what makes the balance properties
//! reachable when a CPI in the enclosing handler leaves the deserialized account
//! unconstrained". That is the program author's rationale for the same narrowing, not
//! something this harness demonstrated. The unwinding evidence above stands on its own.
//!
//! Finally, the consequence of the narrowing: that `apply_deposit` is the handler's only
//! writer of `VaultState` — `deposit` is `invoke(transfer(..))?` followed by that one call —
//! rests on reading the four-line handler, not on a prover verdict.

#![allow(unused_imports)]
#![allow(unused_variables)]

use anchor_lang::prelude::*;
use anchor_lang::solana_program::account_info::AccountInfo;
use cvlr::mathint::NativeInt;
use cvlr::prelude::*;
use cvlr_solana::cvlr_deserialize_nondet_accounts;
use cvlr_solana::cvlr_nondet_pubkey;
use cvlr_solana::pubkey::Pk;

/// The size Anchor's `init` constraint allocates for the vault account.
const VAULT_ACCOUNT_SPACE: usize = 8 + crate::VaultState::SIZE;

fn system_program_id() -> Pubkey {
    anchor_lang::solana_program::system_program::ID
}

/// 16 unconstrained `AccountInfo`s, on the heap (the Prover does not fully support large
/// stack arrays) and leaked so the borrow can carry the `'info` lifetime Anchor's account
/// types require.
fn nondet_account_infos() -> &'static [AccountInfo<'static>] {
    let boxed: Box<[AccountInfo<'static>; 16]> = Box::new(cvlr_deserialize_nondet_accounts());
    &Box::leak(boxed)[..]
}

/// A `VaultState` with every field unconstrained.
fn nondet_vault_state() -> crate::VaultState {
    crate::VaultState {
        authority: cvlr_nondet_pubkey(),
        balance: nondet(),
        bump: nondet(),
    }
}

/// `Pk` is the supported way to render a key on this backend; decomposing a symbolic
/// `Pubkey` into `u64` words instead provokes `Imprecision detected` in the Prover's bitwise
/// domain, which cost a rule a spurious violation in run 2.
fn clog_vault(state: &crate::VaultState) {
    clog!(Pk(&state.authority) => "vault.authority");
    clog!(state.balance => "vault.balance");
    clog!(state.bump => "vault.bump");
}

/// Identity of the three accounts a rule works with. Only immutable fields, so this may be
/// called at any point. Slot 2 is the system program for `initialize` and the fee collector
/// (when supplied) for `withdraw`.
fn clog_account_identities(infos: &[AccountInfo<'static>]) {
    clog!(Pk(infos[0].key) => "acct0_vault_key");
    clog!(Pk(infos[1].key) => "acct1_authority_key");
    clog!(Pk(infos[2].key) => "acct2_sysprog_or_collector_key");
    clog!(infos[1].is_signer => "acct1_is_signer");
}

/// Shape assumptions for a program-owned `VaultState` account: what the deserializer needs
/// in order to be analyzable, and what the account genuinely is on chain. The data length is
/// exactly what Anchor's `space = 8 + VaultState::SIZE` constraint allocates.
fn assume_vault_account(info: &AccountInfo<'static>) {
    cvlr_assume!(*info.owner == crate::ID);
    cvlr_assume!(info.data_len() == VAULT_ACCOUNT_SPACE);
    cvlr_assume!(info.is_writable);
}

fn assume_system_program(info: &AccountInfo<'static>) {
    cvlr_assume!(*info.key == system_program_id());
    cvlr_assume!(info.executable);
}

/// Build the program's `Initialize` accounts struct from unconstrained accounts.
///
/// `try_from_unchecked` rather than `try_from` on purpose: the account a real `initialize`
/// writes to has just been created and zeroed by the System Program, so its 8-byte
/// discriminator is zero. `try_from` would require a matching discriminator and would model
/// only *already-initialized* vaults — a pre-state disjoint from the reachable one.
/// `try_from_unchecked` is what Anchor's own `init` path uses; it still pins
/// `owner == crate::ID` (the post-CPI state) but leaves the discriminator free, so the
/// modeled pre-state is a superset containing both the fresh account and the
/// already-initialized one. Nothing else is constrained: the vault's pre-existing
/// `authority`, `balance` and `bump`, the account keys and lamports are all free.
fn initialize_accounts() -> (crate::Initialize<'static>, &'static [AccountInfo<'static>]) {
    let infos = nondet_account_infos();
    assume_vault_account(&infos[0]);
    cvlr_assume!(infos[1].is_signer);
    cvlr_assume!(infos[1].is_writable);
    assume_system_program(&infos[2]);
    let ix = crate::Initialize {
        vault: Account::try_from_unchecked(&infos[0]).unwrap(),
        authority: Signer::try_from(&infos[1]).unwrap(),
        system_program: Program::try_from(&infos[2]).unwrap(),
    };
    (ix, infos)
}

/// Build the program's `Withdraw` accounts struct. The fee collector is present or absent
/// nondeterministically, so both branches of the handler are covered.
///
/// Modelling note: the fee collector is drawn from a distinct `AccountInfo`, which the layout
/// helper gives its own lamport cell, so this builder does not cover a caller passing the
/// vault itself as the fee sink. For `rule_balance_le_lamports_withdraw` the gap is in the
/// safe direction — with aliasing the vault's real post-state lamports would be *larger* by
/// the fee, so the asserted inequality only gets easier — but a lamport *conservation*
/// property would need a key-distinctness assumption or an aliasing case split.
fn withdraw_accounts() -> (crate::Withdraw<'static>, &'static [AccountInfo<'static>]) {
    let infos = nondet_account_infos();
    assume_vault_account(&infos[0]);
    cvlr_assume!(infos[1].is_signer);
    cvlr_assume!(infos[1].is_writable);
    let ix = crate::Withdraw {
        vault: Account::try_from_unchecked(&infos[0]).unwrap(),
        authority: Signer::try_from(&infos[1]).unwrap(),
        fee_collector: if nondet::<bool>() {
            Some(UncheckedAccount::try_from(&infos[2]))
        } else {
            None
        },
    };
    (ix, infos)
}

// ---------------------------------------------------------------------------------------
// Property 1: init_sets_authority_to_signer
//
// Also the `initialize` leg of Property 7's authority-immutability clause: `authority` is
// written by `initialize`, to the key of the account bound to the `authority` field — the
// account whose signature the declared `Signer<'info>` type requires — rather than to the
// vault's own key, the system program's, or whatever the account happened to hold before.
// ---------------------------------------------------------------------------------------
#[rule]
fn rule_init_sets_authority_to_signer() {
    let (mut ix, infos) = initialize_accounts();
    let authority_key = ix.authority.key();
    let vault_authority_before = ix.vault.authority;

    let ctx = Context::new(
        &crate::ID,
        &mut ix,
        &[],
        crate::InitializeBumps { vault: nondet() },
    );
    crate::vault_program::initialize(ctx).unwrap();

    clog_account_identities(infos);
    clog!(Pk(&authority_key) => "signing_authority_key");
    clog!(Pk(&vault_authority_before) => "vault.authority_before");
    clog_vault(&ix.vault);
    cvlr_assert!(ix.vault.authority == authority_key);
}

// ---------------------------------------------------------------------------------------
// Property 2: init_zeroes_balance
// ---------------------------------------------------------------------------------------
#[rule]
fn rule_init_zeroes_balance() {
    let (mut ix, infos) = initialize_accounts();
    let balance_before = ix.vault.balance;

    let ctx = Context::new(
        &crate::ID,
        &mut ix,
        &[],
        crate::InitializeBumps { vault: nondet() },
    );
    crate::vault_program::initialize(ctx).unwrap();

    clog_account_identities(infos);
    clog!(balance_before => "vault.balance_before");
    clog_vault(&ix.vault);
    cvlr_assert!(ix.vault.balance == 0);
}

// ---------------------------------------------------------------------------------------
// Property 4: init_stores_canonical_bump — PARTIAL, see the module header.
// ---------------------------------------------------------------------------------------
#[rule]
fn rule_init_stores_validated_bump() {
    let (mut ix, infos) = initialize_accounts();
    let validated_bump: u8 = nondet();
    let bump_before = ix.vault.bump;

    let ctx = Context::new(
        &crate::ID,
        &mut ix,
        &[],
        crate::InitializeBumps {
            vault: validated_bump,
        },
    );
    crate::vault_program::initialize(ctx).unwrap();

    clog_account_identities(infos);
    clog!(validated_bump => "bump_derived_by_validation");
    clog!(bump_before => "vault.bump_before");
    clog_vault(&ix.vault);
    cvlr_assert!(ix.vault.bump == validated_bump);
}

// ---------------------------------------------------------------------------------------
// Property 7 — deposit / withdraw legs: the invariant's inductive step. The vault's address
// is not writable by any handler, so leaving `authority` and `bump` untouched preserves the
// binding. The base case at `initialize` is not proved (module header).
// ---------------------------------------------------------------------------------------

/// The deposit state transition leaves `authority` and `bump` alone, so a deposit can
/// neither re-point a vault at another owner nor break the seed/bump binding that
/// `deposit`'s own `seeds = [b"vault", vault.authority], bump = vault.bump` constraint will
/// validate on the next instruction.
///
/// Taken at `vault_accounting::apply_deposit`; see "On the `deposit` leg" in the module
/// header for the three measurements showing the handler is unreachable, and for what the
/// narrowing costs.
#[rule]
fn rule_authority_unchanged_deposit() {
    let mut state = nondet_vault_state();
    let amount: u64 = nondet();
    let before_authority = state.authority;
    let before_bump = state.bump;
    let before_balance = state.balance;

    crate::vault_accounting::apply_deposit(&mut state, amount).unwrap();

    clog!(Pk(&before_authority) => "authority_before");
    clog!(before_bump => "bump_before");
    clog!(before_balance => "balance_before");
    clog!(amount => "amount");
    clog_vault(&state);
    cvlr_assert!(state.authority == before_authority);
    cvlr_assert!(state.bump == before_bump);
}

/// `withdraw` — the real handler, CPI-free, over both fee branches — leaves `authority` and
/// `bump` alone.
///
/// Declared cost: `Withdraw`'s account validation (`seeds`/`bump` and `has_one = authority`)
/// is not executed; run 2 established that driving `Withdraw::try_accounts` instead compares
/// values the build havocs (header fact 2), so nothing is gained by it.
///
/// Domain note: `withdraw` debits the vault's lamports with a raw `u64` subtraction and this
/// workspace builds with `overflow-checks = true`, so executions with `amount` above the
/// vault's lamports abort and are pruned regardless. The solvency assumption below states
/// that narrowing explicitly rather than leaving it implicit in the pruning.
#[rule]
fn rule_authority_unchanged_withdraw() {
    let (mut ix, infos) = withdraw_accounts();
    let vault_lamports_before = infos[0].lamports();
    cvlr_assume!(ix.vault.balance <= vault_lamports_before);
    let before_authority = ix.vault.authority;
    let before_bump = ix.vault.bump;
    let before_balance = ix.vault.balance;
    let authority_key = ix.authority.key();
    let has_collector = ix.fee_collector.is_some();
    let authority_lamports_before = infos[1].lamports();
    let collector_lamports_before = infos[2].lamports();
    let amount: u64 = nondet();

    let ctx = Context::new(&crate::ID, &mut ix, &[], crate::WithdrawBumps::default());
    crate::vault_program::withdraw(ctx, amount).unwrap();

    clog_account_identities(infos);
    clog!(Pk(&authority_key) => "withdraw_authority_key");
    clog!(has_collector => "fee_collector_present");
    clog!(authority_lamports_before => "authority_lamports_before");
    clog!(infos[1].lamports() => "authority_lamports_after");
    clog!(collector_lamports_before => "collector_lamports_before");
    clog!(infos[2].lamports() => "collector_lamports_after");
    clog!(Pk(&before_authority) => "vault.authority_before");
    clog!(before_bump => "vault.bump_before");
    clog!(before_balance => "vault.balance_before");
    clog!(vault_lamports_before => "vault_lamports_before");
    clog!(infos[0].lamports() => "vault_lamports_after");
    clog!(amount => "amount");
    clog_vault(&ix.vault);
    cvlr_assert!(ix.vault.authority == before_authority);
    cvlr_assert!(ix.vault.bump == before_bump);
}

// ---------------------------------------------------------------------------------------
// Property 8: recorded_balance_never_exceeds_vault_lamports.
// `withdraw` fully proved; `initialize` and `deposit` partial — see the module header.
// ---------------------------------------------------------------------------------------

/// `initialize` establishes the invariant: the recorded balance it leaves is 0, and it moves
/// none of the vault account's lamports, so whatever the account was funded with is still
/// there.
///
/// Declared cost: `balance` is a `u64`, so `balance <= lamports` reduces to `0 <= lamports`
/// and carries nothing on its own; the lamport-preservation assertion beside it is what makes
/// this rule say something `rule_init_zeroes_balance` does not. The "on an account funded for
/// rent" half of the property's init clause is not checked — rent exemption is established by
/// the havoced `init` CPI (header fact 3).
#[rule]
fn rule_balance_le_lamports_initialize() {
    let (mut ix, infos) = initialize_accounts();
    let lamports_before = infos[0].lamports();

    let ctx = Context::new(
        &crate::ID,
        &mut ix,
        &[],
        crate::InitializeBumps { vault: nondet() },
    );
    crate::vault_program::initialize(ctx).unwrap();

    let lamports_after = infos[0].lamports();
    clog_account_identities(infos);
    clog!(lamports_before => "vault_lamports_before");
    clog!(lamports_after => "vault_lamports_after");
    clog_vault(&ix.vault);
    cvlr_assert!(ix.vault.balance <= lamports_after);
    cvlr_assert!(lamports_after == lamports_before);
}

/// The accounting half of Property 8's `deposit` leg, and all of it that is reachable here.
///
/// The credit is *exactly* `amount`, stated over exact integers so neither wrapping nor
/// truncation can hide in it: a core that credited more than the transfer moved, or credited
/// twice, fails this rule. Combined with the System Program moving `amount` into the vault,
/// that is what preserves `balance <= lamports` — but the lamport side is **not observed
/// here**, because there is no account in this rule: it is only observable at the handler,
/// and the handler is unreachable (module header). Property 8's deposit leg is therefore
/// partial, not green.
#[rule]
fn rule_deposit_core_credits_exact_amount() {
    let mut state = nondet_vault_state();
    let amount: u64 = nondet();
    let balance_before = state.balance;

    crate::vault_accounting::apply_deposit(&mut state, amount).unwrap();

    clog!(balance_before => "vault.balance_before");
    clog!(amount => "amount");
    clog_vault(&state);
    cvlr_assert!(
        NativeInt::from(state.balance) == NativeInt::from(balance_before) + NativeInt::from(amount)
    );
}

/// `withdraw` preserves the invariant — the one leg with both sides observed. Driven as the
/// real handler over both fee branches: it debits the vault account's lamports directly and
/// debits the recorded balance through `vault_accounting::apply_withdrawal`, with no CPI in
/// the way, and the rule reads the account's real lamports cell before and after.
/// (On the fee collector's aliasing, see `withdraw_accounts`.)
#[rule]
fn rule_balance_le_lamports_withdraw() {
    let (mut ix, infos) = withdraw_accounts();
    let lamports_before = infos[0].lamports();
    let balance_before = ix.vault.balance;
    cvlr_assume!(balance_before <= lamports_before);
    let authority_key = ix.authority.key();
    let authority_lamports_before = infos[1].lamports();
    let has_collector = ix.fee_collector.is_some();
    let collector_lamports_before = infos[2].lamports();
    let amount: u64 = nondet();

    let ctx = Context::new(&crate::ID, &mut ix, &[], crate::WithdrawBumps::default());
    crate::vault_program::withdraw(ctx, amount).unwrap();

    let lamports_after = infos[0].lamports();
    clog_account_identities(infos);
    clog!(Pk(&authority_key) => "withdraw_authority_key");
    clog!(has_collector => "fee_collector_present");
    clog!(authority_lamports_before => "authority_lamports_before");
    clog!(infos[1].lamports() => "authority_lamports_after");
    clog!(collector_lamports_before => "collector_lamports_before");
    clog!(infos[2].lamports() => "collector_lamports_after");
    clog!(lamports_before => "vault_lamports_before");
    clog!(lamports_after => "vault_lamports_after");
    clog!(balance_before => "vault.balance_before");
    clog!(amount => "amount");
    clog_vault(&ix.vault);
    cvlr_assert!(ix.vault.balance <= lamports_after);
}
