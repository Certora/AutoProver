//! An in-process model of the SEP-41 tokens this contract calls, for verification only.
//!
//! The Certora Soroban Prover does not follow a call into another contract: the call returns an
//! arbitrary value and changes nothing. Through the real client, two reads of one balance are two
//! unrelated numbers, so no rule about what a transfer did can be proven.
//!
//! Under the `certora` feature the contract's token client is this module's `Client`. The scaffold
//! gated the import or `contractimport!` module that names the real one on `not(feature =
//! "certora")` and gave it a twin naming this module; those lines say "added by AutoProver". The
//! tokens' state lives in this contract's own persistent storage, under a key type of its own and
//! keyed by the token's address, so a contract holding several tokens keeps them apart.
//!
//! What a verdict then assumes: each token behaves like a standard SEP-41 token, and like the
//! Stellar Asset Contract for its admin functions. A transfer moves exactly `amount` from `from` to
//! `to`, needs `from`'s authorization, and panics on a negative amount or an insufficient balance.
//! There is no fee on transfer, no hook, and no call back into this contract. Whether an account is
//! authorized is recorded, and transfers do not consult it. A `try_` function that changes state
//! may also fail for no stated reason, and then changes nothing: a real token can refuse for
//! reasons the model does not track.
//!
//! A rule reads the model's state with `balance_of` and `allowance_of`, and may fix a balance
//! before the call with `set_balance`.

#![allow(dead_code)]

use cvlr::cvlr_assume;
use cvlr::nondet::nondet;
use cvlr_soroban::is_auth;
use soroban_sdk::{contracttype, Address, Env, Error, InvokeError, String, TryFromVal, Val};

// Everything else `soroban_sdk::token` has, so code that names this module in its place still
// finds `TokenInterface` and the rest. The clients below shadow the SDK's.
pub use soroban_sdk::token::*;

/// The model's storage keys. A variant is stored as its name and fields, not its type's name, so
/// the names are chosen not to collide with the contract's own: `DataKey::Balance(a, b)` and a
/// `Balance(a, b)` here would be one entry.
#[contracttype]
enum CertoraTokenKey {
    /// (token, holder) -> i128
    CertoraBalance(Address, Address),
    /// (token, from, spender) -> CertoraAllowanceValue
    CertoraAllowance(Address, Address, Address),
    /// token -> Address
    CertoraAdmin(Address),
    /// (token, holder) -> bool
    CertoraAuthorized(Address, Address),
    /// token -> u32
    CertoraDecimals(Address),
    /// token -> String
    CertoraName(Address),
    /// token -> String
    CertoraSymbol(Address),
}

#[contracttype]
struct CertoraAllowanceValue {
    amount: i128,
    expiration_ledger: u32,
}

/// The balance `holder` has on `token`, as the model holds it.
///
/// Storage starts arbitrary, so an entry could hold anything. A SEP-41 balance is never negative,
/// and every write here keeps it so; the read assumes it.
pub fn balance_of(env: &Env, token: &Address, holder: &Address) -> i128 {
    let key = CertoraTokenKey::CertoraBalance(token.clone(), holder.clone());
    let balance: i128 = env.storage().persistent().get(&key).unwrap_or(0);
    cvlr_assume!(balance >= 0);
    balance
}

/// Fixes `holder`'s balance on `token` outright, for a rule about a particular balance.
pub fn set_balance(env: &Env, token: &Address, holder: &Address, balance: i128) {
    let key = CertoraTokenKey::CertoraBalance(token.clone(), holder.clone());
    env.storage().persistent().set(&key, &balance);
}

/// What `spender` may still move of `from`'s balance on `token`: nothing once the allowance has
/// expired.
pub fn allowance_of(env: &Env, token: &Address, from: &Address, spender: &Address) -> i128 {
    let key = CertoraTokenKey::CertoraAllowance(token.clone(), from.clone(), spender.clone());
    let stored: Option<CertoraAllowanceValue> = env.storage().persistent().get(&key);
    match stored {
        Some(a) if a.expiration_ledger >= env.ledger().sequence() => {
            cvlr_assume!(a.amount >= 0);
            a.amount
        }
        _ => 0,
    }
}

// Each operation below returns false, changing nothing, where a real token would panic.

fn move_balance(env: &Env, token: &Address, from: &Address, to: &Address, amount: i128) -> bool {
    if amount < 0 {
        return false;
    }
    let from_balance = balance_of(env, token, from);
    if from_balance < amount {
        return false;
    }
    if balance_of(env, token, to).checked_add(amount).is_none() {
        return false;
    }
    set_balance(env, token, from, from_balance - amount);
    // Read again after the write, so a transfer to oneself leaves the balance as it was. No
    // overflow: this read is at most the one checked above.
    let to_balance = balance_of(env, token, to);
    set_balance(env, token, to, to_balance + amount);
    true
}

fn mint_balance(env: &Env, token: &Address, to: &Address, amount: i128) -> bool {
    if amount < 0 {
        return false;
    }
    match balance_of(env, token, to).checked_add(amount) {
        Some(balance) => {
            set_balance(env, token, to, balance);
            true
        }
        None => false,
    }
}

fn burn_balance(env: &Env, token: &Address, from: &Address, amount: i128) -> bool {
    if amount < 0 {
        return false;
    }
    let balance = balance_of(env, token, from);
    if balance < amount {
        return false;
    }
    set_balance(env, token, from, balance - amount);
    true
}

fn approve_allowance(
    env: &Env,
    token: &Address,
    from: &Address,
    spender: &Address,
    amount: i128,
    expiration_ledger: u32,
) -> bool {
    if amount < 0 || (amount > 0 && expiration_ledger < env.ledger().sequence()) {
        return false;
    }
    let key = CertoraTokenKey::CertoraAllowance(token.clone(), from.clone(), spender.clone());
    env.storage().persistent().set(&key, &CertoraAllowanceValue { amount, expiration_ledger });
    true
}

/// `amount` out of `spender`'s allowance on `from`'s balance, then `spend` itself, which is
/// `transfer_from`'s or `burn_from`'s move.
fn spend_allowance(
    env: &Env,
    token: &Address,
    from: &Address,
    spender: &Address,
    amount: i128,
    spend: impl FnOnce() -> bool,
) -> bool {
    if allowance_of(env, token, from, spender) < amount || !spend() {
        return false;
    }
    let key = CertoraTokenKey::CertoraAllowance(token.clone(), from.clone(), spender.clone());
    let stored: Option<CertoraAllowanceValue> = env.storage().persistent().get(&key);
    if let Some(a) = stored {
        let left = CertoraAllowanceValue { amount: a.amount - amount, expiration_ledger: a.expiration_ledger };
        env.storage().persistent().set(&key, &left);
    }
    true
}

/// What a client's `try_` function returns, spelled as the SDK's generated clients spell it.
pub type Attempt<T> =
    Result<Result<T, <T as TryFromVal<Env, Val>>::Error>, Result<Error, InvokeError>>;

fn succeeded<T: TryFromVal<Env, Val>>(value: T) -> Attempt<T> {
    Ok(Ok(value))
}

fn failed<T: TryFromVal<Env, Val>>() -> Attempt<T> {
    Err(Err(InvokeError::Abort))
}

fn outcome(ok: bool) -> Attempt<()> {
    if ok {
        succeeded(())
    } else {
        failed()
    }
}

/// What a transfer's `to` may be: an `Address`, and from soroban-sdk 23 a `MuxedAddress`.
pub trait Recipient {
    fn recipient(self) -> Address;
}

impl Recipient for Address {
    fn recipient(self) -> Address {
        self
    }
}

impl Recipient for &Address {
    fn recipient(self) -> Address {
        self.clone()
    }
}

/// The client the contract calls a token through, in the shape of the SDK's `TokenClient` and
/// `StellarAssetClient` together.
pub struct Client<'a> {
    pub env: Env,
    pub address: Address,
    _phantom: core::marker::PhantomData<&'a ()>,
}

pub type TokenClient<'a> = Client<'a>;
pub type StellarAssetClient<'a> = Client<'a>;

impl<'a> Client<'a> {
    pub fn new(env: &Env, address: &Address) -> Self {
        Self { env: env.clone(), address: address.clone(), _phantom: core::marker::PhantomData }
    }

    /// Whether a `try_` call that changes state, authorized by `signer`, goes ahead.
    fn admitted(&self, signer: &Address) -> bool {
        !nondet::<bool>() && is_auth(signer.clone())
    }

    fn stored_admin(&self) -> Option<Address> {
        self.env.storage().persistent().get(&CertoraTokenKey::CertoraAdmin(self.address.clone()))
    }

    // SEP-41

    pub fn allowance(&self, from: &Address, spender: &Address) -> i128 {
        allowance_of(&self.env, &self.address, from, spender)
    }

    pub fn try_allowance(&self, from: &Address, spender: &Address) -> Attempt<i128> {
        succeeded(self.allowance(from, spender))
    }

    pub fn approve(&self, from: &Address, spender: &Address, amount: &i128, expiration_ledger: &u32) {
        from.require_auth();
        if !approve_allowance(&self.env, &self.address, from, spender, *amount, *expiration_ledger) {
            panic!("token approve failed");
        }
    }

    pub fn try_approve(
        &self,
        from: &Address,
        spender: &Address,
        amount: &i128,
        expiration_ledger: &u32,
    ) -> Attempt<()> {
        outcome(
            self.admitted(from)
                && approve_allowance(&self.env, &self.address, from, spender, *amount, *expiration_ledger),
        )
    }

    pub fn balance(&self, id: &Address) -> i128 {
        balance_of(&self.env, &self.address, id)
    }

    pub fn try_balance(&self, id: &Address) -> Attempt<i128> {
        succeeded(self.balance(id))
    }

    pub fn transfer(&self, from: &Address, to: impl Recipient, amount: &i128) {
        from.require_auth();
        if !move_balance(&self.env, &self.address, from, &to.recipient(), *amount) {
            panic!("token transfer failed");
        }
    }

    pub fn try_transfer(&self, from: &Address, to: impl Recipient, amount: &i128) -> Attempt<()> {
        outcome(
            self.admitted(from)
                && move_balance(&self.env, &self.address, from, &to.recipient(), *amount),
        )
    }

    fn move_allowed(&self, spender: &Address, from: &Address, to: &Address, amount: i128) -> bool {
        spend_allowance(&self.env, &self.address, from, spender, amount, || {
            move_balance(&self.env, &self.address, from, to, amount)
        })
    }

    pub fn transfer_from(&self, spender: &Address, from: &Address, to: &Address, amount: &i128) {
        spender.require_auth();
        if !self.move_allowed(spender, from, to, *amount) {
            panic!("token transfer_from failed");
        }
    }

    pub fn try_transfer_from(
        &self,
        spender: &Address,
        from: &Address,
        to: &Address,
        amount: &i128,
    ) -> Attempt<()> {
        outcome(self.admitted(spender) && self.move_allowed(spender, from, to, *amount))
    }

    pub fn burn(&self, from: &Address, amount: &i128) {
        from.require_auth();
        if !burn_balance(&self.env, &self.address, from, *amount) {
            panic!("token burn failed");
        }
    }

    pub fn try_burn(&self, from: &Address, amount: &i128) -> Attempt<()> {
        outcome(self.admitted(from) && burn_balance(&self.env, &self.address, from, *amount))
    }

    fn burn_allowed(&self, spender: &Address, from: &Address, amount: i128) -> bool {
        spend_allowance(&self.env, &self.address, from, spender, amount, || {
            burn_balance(&self.env, &self.address, from, amount)
        })
    }

    pub fn burn_from(&self, spender: &Address, from: &Address, amount: &i128) {
        spender.require_auth();
        if !self.burn_allowed(spender, from, *amount) {
            panic!("token burn_from failed");
        }
    }

    pub fn try_burn_from(&self, spender: &Address, from: &Address, amount: &i128) -> Attempt<()> {
        outcome(self.admitted(spender) && self.burn_allowed(spender, from, *amount))
    }

    pub fn decimals(&self) -> u32 {
        // 7 is a Stellar asset's.
        let key = CertoraTokenKey::CertoraDecimals(self.address.clone());
        self.env.storage().persistent().get(&key).unwrap_or(7)
    }

    pub fn try_decimals(&self) -> Attempt<u32> {
        succeeded(self.decimals())
    }

    pub fn name(&self) -> String {
        let key = CertoraTokenKey::CertoraName(self.address.clone());
        self.env.storage().persistent().get(&key).unwrap_or_else(|| String::from_str(&self.env, ""))
    }

    pub fn try_name(&self) -> Attempt<String> {
        succeeded(self.name())
    }

    pub fn symbol(&self) -> String {
        let key = CertoraTokenKey::CertoraSymbol(self.address.clone());
        self.env.storage().persistent().get(&key).unwrap_or_else(|| String::from_str(&self.env, ""))
    }

    pub fn try_symbol(&self) -> Attempt<String> {
        succeeded(self.symbol())
    }

    // The Stellar Asset Contract's admin functions. A token with no admin recorded has none: its
    // admin functions panic, and their `try_` forms fail.

    pub fn admin(&self) -> Address {
        match self.stored_admin() {
            Some(admin) => admin,
            None => panic!("token has no admin"),
        }
    }

    pub fn try_admin(&self) -> Attempt<Address> {
        match self.stored_admin() {
            Some(admin) => succeeded(admin),
            None => failed(),
        }
    }

    pub fn set_admin(&self, new_admin: &Address) {
        self.admin().require_auth();
        let key = CertoraTokenKey::CertoraAdmin(self.address.clone());
        self.env.storage().persistent().set(&key, new_admin);
    }

    pub fn try_set_admin(&self, new_admin: &Address) -> Attempt<()> {
        match self.stored_admin() {
            Some(admin) if self.admitted(&admin) => {
                let key = CertoraTokenKey::CertoraAdmin(self.address.clone());
                self.env.storage().persistent().set(&key, new_admin);
                succeeded(())
            }
            _ => failed(),
        }
    }

    pub fn authorized(&self, id: &Address) -> bool {
        let key = CertoraTokenKey::CertoraAuthorized(self.address.clone(), id.clone());
        self.env.storage().persistent().get(&key).unwrap_or(true)
    }

    pub fn try_authorized(&self, id: &Address) -> Attempt<bool> {
        succeeded(self.authorized(id))
    }

    pub fn set_authorized(&self, id: &Address, authorize: &bool) {
        self.admin().require_auth();
        let key = CertoraTokenKey::CertoraAuthorized(self.address.clone(), id.clone());
        self.env.storage().persistent().set(&key, authorize);
    }

    pub fn try_set_authorized(&self, id: &Address, authorize: &bool) -> Attempt<()> {
        match self.stored_admin() {
            Some(admin) if self.admitted(&admin) => {
                let key = CertoraTokenKey::CertoraAuthorized(self.address.clone(), id.clone());
                self.env.storage().persistent().set(&key, authorize);
                succeeded(())
            }
            _ => failed(),
        }
    }

    pub fn mint(&self, to: &Address, amount: &i128) {
        self.admin().require_auth();
        if !mint_balance(&self.env, &self.address, to, *amount) {
            panic!("token mint failed");
        }
    }

    pub fn try_mint(&self, to: &Address, amount: &i128) -> Attempt<()> {
        match self.stored_admin() {
            Some(admin) => {
                outcome(self.admitted(&admin) && mint_balance(&self.env, &self.address, to, *amount))
            }
            None => failed(),
        }
    }

    pub fn clawback(&self, from: &Address, amount: &i128) {
        self.admin().require_auth();
        if !burn_balance(&self.env, &self.address, from, *amount) {
            panic!("token clawback failed");
        }
    }

    pub fn try_clawback(&self, from: &Address, amount: &i128) -> Attempt<()> {
        match self.stored_admin() {
            Some(admin) => {
                outcome(self.admitted(&admin) && burn_balance(&self.env, &self.address, from, *amount))
            }
            None => failed(),
        }
    }
}
