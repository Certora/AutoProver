//! Nondet accounts for a jiminy program.
//!
//! `cvlr-solana` builds the SDK's `AccountInfo`, which a jiminy handler cannot take. These helpers
//! build jiminy's own `AccountHandle`, over the same kind of input region the Prover gives each of
//! `cvlr-solana`'s accounts, and the `Abr` every handle is read and written through.
//!
//! A jiminy `Account` is a view of the runtime's serialized account: an 88-byte header (duplicate
//! marker, flags, realloc budget, key, owner, lamports, data length), then the data, then room for
//! the data to grow. An `AccountHandle` is a reference to that header, so a region laid out the
//! same way is an account.

use core::mem::size_of;

use cvlr::prelude::*;
use jiminy_entrypoint::account::{
    deser_accounts, Abr, Account, AccountHandle, MAX_PERMITTED_DATA_INCREASE,
};

extern "C" {
    fn CVT_nondet_solana_account_space(size: usize) -> *mut u8;
}

/// The largest data length an account built here can have. The region also holds the header,
/// the data's room to grow, and the trailing rent epoch.
pub const MAX_DATA_LEN: usize = 8 * 1024 * 1024;

const REGION_SIZE: usize = size_of::<Account>() + MAX_DATA_LEN + MAX_PERMITTED_DATA_INCREASE + 8;

/// Where the header's realloc budget sits. The runtime hands every account to the program with it
/// at zero, and `Account::realloc` reads it.
const REALLOC_BUDGET_USED_OFFSET: usize = 4;

/// The account borrow registry a jiminy handler takes as `&mut Abr`.
///
/// jiminy hands out one per instruction, and its borrow rules assume there is only one. Call this
/// once per rule and pass the same `Abr` to every handler and every read.
pub fn cvlr_jiminy_abr() -> Abr {
    let mut no_accounts = 0u64;
    // SAFETY: a serialized input that holds no accounts is its leading count alone.
    let (_, accounts) = unsafe { deser_accounts::<0>(&(), (&raw mut no_accounts).cast()) };
    accounts.etp_start().0
}

/// One account with arbitrary contents, in a region of its own.
///
/// The key, owner, flags, lamports and data are unconstrained. Two accounts may have the same
/// key: assume `abr.get(a).key() != abr.get(b).key()` where the program relies on distinct
/// accounts. The data length is at most [`MAX_DATA_LEN`].
pub fn cvlr_jiminy_account() -> AccountHandle<'static> {
    // SAFETY: the region is the size of a serialized account with `MAX_DATA_LEN` bytes of data,
    // and its header is constrained to describe data that fits in it. `AccountHandle` is a
    // `#[repr(transparent)]` reference to the header, and the region is never freed.
    unsafe {
        let region = CVT_nondet_solana_account_space(REGION_SIZE);
        let account = &*region.cast::<Account>();
        cvlr_assume!(account.data_len() <= MAX_DATA_LEN);
        cvlr_assume!(*region.add(REALLOC_BUDGET_USED_OFFSET).cast::<i32>() == 0);
        core::mem::transmute::<*mut u8, AccountHandle<'static>>(region)
    }
}

/// Sixteen accounts, each a separate [`cvlr_jiminy_account`]. Pass a prefix of the array to a
/// handler that takes `&[AccountHandle]`, and build more with [`cvlr_jiminy_account`] when an
/// instruction takes more than sixteen.
pub fn cvlr_jiminy_accounts() -> [AccountHandle<'static>; 16] {
    [
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
        cvlr_jiminy_account(),
    ]
}
