//! Harness for Admin_Setup of the `Token` contract.
//!
//! Every rule drives the contract's own entry points (`crate::Token::initialize`,
//! `crate::Token::mint`, `crate::Token::balance`) and reads the persistent `"ADMIN"`
//! entry with exactly the key spelling the contract uses (`&"ADMIN"`).
//!
//! Property -> rule mapping (property titles as they appear in the batch):
//!   * `unauthenticated_admin_overwrite_enables_unlimited_mint`
//!         -> rule `unauthenticated_admin_overwrite_enables_unlimited_mint`
//!   * `initialize_is_one_shot`
//!         -> rule `initialize_is_one_shot`
//!   * `admin_rotation_requires_current_admin_auth`
//!         -> rule `admin_rotation_requires_current_admin_auth`
//!   * `initialize_persists_admin_readable_by_mint`
//!         -> rules `initialize_persists_admin` (the write lands under the key and
//!            durability `mint` reads, holding exactly `a`) and
//!            `initialize_then_mint_uses_stored_admin` (the subsequent `mint` succeeds,
//!            requires `a`'s auth and credits `to`). One property, two rules, so a
//!            failure names which half broke.
//!
//! Expected verdicts (observed in a prover run, not predictions):
//!   * `unauthenticated_admin_overwrite_enables_unlimited_mint` — VIOLATED (real defect)
//!   * `initialize_is_one_shot`                                 — VIOLATED (real defect)
//!   * `admin_rotation_requires_current_admin_auth`             — VIOLATED (real defect)
//!   * `initialize_persists_admin`                              — VERIFIED
//!   * `initialize_then_mint_uses_stored_admin`                 — VERIFIED
//!
//! The three violations are declared expected-to-fail: they are findings, not rules the
//! author could not get to pass. Their single root cause is that `Token::initialize` is
//! `e.storage().persistent().set(&"ADMIN", &admin);` — no `require_auth` on anybody
//! (neither the stored admin nor the incoming one) and no `has(&"ADMIN")`
//! re-initialization guard. The fix is both guards, plus a separate, incumbent-authorized
//! `set_admin` entry point if rotation is wanted at all.
//!
//! NOTE ON THE CONF: `rule_sanity` must stay enabled. Part of the force of
//! `initialize_then_mint_uses_stored_admin` comes from the path after `mint` being
//! *reachable*; without the vacuity check a rule whose calls all trap would pass for the
//! wrong reason.

#![allow(unused_imports)]

use cvlr::prelude::*;
use cvlr_soroban::is_auth;
// The Soroban rule attribute -- registers the rule where the prover looks for it.
use cvlr_soroban_derive::rule;
use soroban_sdk::{Address, Env};

use crate::Token;

/// An `Address` is a host handle, not a value, so a counterexample can only show its
/// payload. This is what we `clog!` whenever an address matters.
///
/// (`cvlr_soroban::Addr` would be the off-the-shelf wrapper, but it implements the
/// `CvlrLog` trait of crates.io `cvlr-log` 0.6.1 while `clog!` in this build resolves to
/// the git `cvlr-log`; the two traits are distinct types in this dependency graph and the
/// wrapper does not typecheck here.)
fn addr_id(a: &Address) -> u64 {
    a.to_val().get_payload()
}

/// Read the persistent `"ADMIN"` entry, exactly as `mint` does.
fn admin_opt(e: &Env) -> Option<Address> {
    e.storage().persistent().get::<_, Address>(&"ADMIN")
}

/// Does the persistent `"ADMIN"` entry exist?
fn admin_is_set(e: &Env) -> bool {
    e.storage().persistent().has(&"ADMIN")
}

// ---------------------------------------------------------------------------
// Property 1: unauthenticated_admin_overwrite_enables_unlimited_mint
//
// Pre-state: an admin `a` is already installed. An arbitrary caller (`attacker`,
// different from `a`) calls `initialize(attacker)` and then `mint(to, amount)`.
// If the token can only be inflated with the standing admin's blessing, then any
// execution that increases `to`'s balance must have had `a`'s authorization.
//
// EXPECTED VERDICT: VIOLATED (declared expected-to-fail in the module header) — this is
// the finding. The prover returned a complete attack execution: `initialize` runs with
// nothing but a key conversion and `put_contract_data` in its frame (no `require_auth`
// host call at all), `mint` then reads the overwritten "ADMIN", satisfies `require_auth`
// with the attacker's own signature, and credits `to`. In the counterexample
// `standing_admin_authorized == false`, `attacker_authorized == true`, and
// `bal_after == bal_before + amount` with `amount > 0`. Cause: `initialize` has neither
// a `require_auth` nor a re-initialization guard.
//
// The no-overflow assumption below is there because the release profile sets
// `overflow-checks = true`, so `receive_balance`'s `balance + amount` traps on overflow;
// the rule is therefore about non-overflowing mints (a trapping path is rolled back by
// the host and cannot exhibit the attack anyway).
// ---------------------------------------------------------------------------
#[rule]
fn unauthenticated_admin_overwrite_enables_unlimited_mint(
    e: Env,
    attacker: Address,
    to: Address,
    amount: i64,
) {
    // The contract is already initialized with some admin `a`.
    cvlr_assume!(admin_is_set(&e));
    let a: Address = admin_opt(&e).unwrap();
    // The attacker is somebody else.
    cvlr_assume!(a != attacker);

    // Authorization of the standing admin for this invocation.
    let standing_admin_authorized = is_auth(a.clone());
    let attacker_authorized = is_auth(attacker.clone());

    let bal_before = Token::balance(&e, to.clone());
    cvlr_assume!(amount > 0);
    cvlr_assume!(bal_before.checked_add(amount).is_some());

    // The attack: overwrite the admin, then mint to an address of the attacker's choice.
    Token::initialize(e.clone(), attacker.clone());
    Token::mint(&e, to.clone(), amount);

    let bal_after = Token::balance(&e, to.clone());

    clog!(standing_admin_authorized, attacker_authorized);
    clog!(bal_before, bal_after, amount);
    clog!(addr_id(&a) => "standing_admin");
    clog!(addr_id(&attacker) => "attacker");
    clog!(addr_id(&to) => "mint_recipient");

    // New supply was created for `to`; that must have required the standing admin's auth.
    cvlr_assert!(bal_after == bal_before || standing_admin_authorized);
}

// ---------------------------------------------------------------------------
// Property 2: initialize_is_one_shot
//
// If the persistent "ADMIN" entry already exists, a further `initialize(b)` must
// not change it (a one-shot initializer would have rejected the call outright,
// and a rejected call leaves storage untouched).
//
// EXPECTED VERDICT: VIOLATED (declared expected-to-fail in the module header). The
// counterexample starts from a state satisfying `has("ADMIN")`, runs `initialize(b)` to
// completion (frame contains only the key conversion and `put_contract_data` — no guard,
// no auth) and the read-back comparison gives `unchanged == false`: the entry now holds
// `b`. Cause: `initialize` never tests `has(&"ADMIN")` before writing.
//
// Note on strength: `still_set` is trivially true of any implementation that writes at
// all, so it carries no load; the property is carried entirely by `unchanged`. It is kept
// only as a sanity read of the post-state.
//
// Key/durability modelling is sound here: `initialize_persists_admin` VERIFIES using the
// same `admin_is_set` / `admin_opt` read-back helpers, so a false `unchanged` is about
// the contract, not about the key being a freshly converted host object.
// ---------------------------------------------------------------------------
#[rule]
fn initialize_is_one_shot(e: Env, b: Address) {
    cvlr_assume!(admin_is_set(&e));
    let before: Address = admin_opt(&e).unwrap();

    Token::initialize(e.clone(), b.clone());

    let still_set = admin_is_set(&e);
    let after_opt = admin_opt(&e);
    let unchanged = after_opt.as_ref() == Some(&before);

    clog!(still_set, unchanged);
    clog!(addr_id(&before) => "admin_before");
    clog!(addr_id(&b) => "initialize_arg");

    cvlr_assert!(still_set);
    // The substantive claim: the incumbent admin survives the second initialize.
    cvlr_assert!(unchanged);
}

// ---------------------------------------------------------------------------
// Property 3: admin_rotation_requires_current_admin_auth
//
// If `initialize(b)` succeeds while "ADMIN" holds some other address `a` — i.e. if the
// stored admin actually got rotated to `b` — then `a` must have authorized the
// invocation. Stated conditionally on the rotation having happened, so that a fixed
// contract which turned re-initialization into a silent no-op would satisfy it.
//
// EXPECTED VERDICT: VIOLATED (declared expected-to-fail in the module header). Both
// assumptions are discharged in the counterexample (an admin `a` is stored, `b != a`),
// `initialize(b)` returns, the post-read shows `rotated == true`, and
// `current_admin_authorized == false`. Cause: `initialize` never reads the stored admin
// and calls `require_auth` on nobody, so the mint privilege can be seized by any party.
// ---------------------------------------------------------------------------
#[rule]
fn admin_rotation_requires_current_admin_auth(e: Env, b: Address) {
    cvlr_assume!(admin_is_set(&e));
    let a: Address = admin_opt(&e).unwrap();
    // A genuine rotation: the new admin differs from the stored one.
    cvlr_assume!(a != b);

    let current_admin_authorized = is_auth(a.clone());

    Token::initialize(e.clone(), b.clone());

    // The rotation went through (the call returned and the entry now holds `b`).
    let rotated = admin_opt(&e).as_ref() == Some(&b);

    clog!(current_admin_authorized, rotated);
    clog!(addr_id(&a) => "admin_before");
    clog!(addr_id(&b) => "new_admin");

    cvlr_assert!(!rotated || current_admin_authorized);
}

// ---------------------------------------------------------------------------
// Property 4 (first half): initialize_persists_admin_readable_by_mint
//
// Starting from a state where no admin is set, `initialize(a)` must leave the persistent
// "ADMIN" entry present and holding exactly `a`, read back under the same key and the
// same durability `mint` uses.
//
// EXPECTED VERDICT: VERIFIED.
// ---------------------------------------------------------------------------
#[rule]
fn initialize_persists_admin(e: Env, a: Address) {
    cvlr_assume!(!admin_is_set(&e));

    Token::initialize(e.clone(), a.clone());

    let set_after = admin_is_set(&e);
    let stored_is_a = admin_opt(&e).as_ref() == Some(&a);

    clog!(set_after, stored_is_a);
    clog!(addr_id(&a) => "new_admin");

    cvlr_assert!(set_after);
    cvlr_assert!(stored_is_a);
}

// ---------------------------------------------------------------------------
// Property 4 (second half): initialize_persists_admin_readable_by_mint
//
// The two-call sequence `initialize(a); mint(to, amount)` from a state with no admin set:
// the mint must succeed, must have required `a`'s authorization, and must credit `to` by
// exactly `amount`.
//
// EXPECTED VERDICT: VERIFIED.
//
// The "wrong key / wrong durability bricks mint" half of the property is stated twice
// over: positively, by the `cvlr_assert!` between the two calls that the entry `mint`
// will read holds `a`; and by reachability, since had `initialize` written where `mint`
// does not read, `mint`'s `.unwrap()` would trap, the path would die, and the trailing
// asserts would hold vacuously. The conf's `rule_sanity` vacuity check is what makes the
// latter visible, so it is load-bearing here and must not be turned off.
//
// The no-overflow assumption is there because the release profile sets
// `overflow-checks = true`: `receive_balance`'s `balance + amount` traps on overflow, so
// this rule speaks about non-overflowing mints.
// ---------------------------------------------------------------------------
#[rule]
fn initialize_then_mint_uses_stored_admin(e: Env, a: Address, to: Address, amount: i64) {
    cvlr_assume!(!admin_is_set(&e));

    Token::initialize(e.clone(), a.clone());

    // Stated positively on this path: the entry `mint` is about to read holds `a`.
    cvlr_assert!(admin_opt(&e).as_ref() == Some(&a));

    let a_authorized = is_auth(a.clone());
    let bal_before = Token::balance(&e, to.clone());
    cvlr_assume!(amount >= 0);
    cvlr_assume!(bal_before.checked_add(amount).is_some());

    Token::mint(&e, to.clone(), amount);

    let bal_after = Token::balance(&e, to.clone());

    clog!(a_authorized);
    clog!(bal_before, bal_after, amount);
    clog!(addr_id(&a) => "new_admin");
    clog!(addr_id(&to) => "mint_recipient");

    // The mint returned, so the address it required auth from is the one we installed.
    cvlr_assert!(a_authorized);
    cvlr_assert!(bal_after == bal_before + amount);
}
