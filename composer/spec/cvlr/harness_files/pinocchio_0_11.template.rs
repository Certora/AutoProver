//! Nondet accounts for a Pinocchio program (Pinocchio 0.11 onwards).
//!
//! `cvlr-solana` builds the SDK's `AccountInfo`, which a Pinocchio handler cannot take. These
//! helpers build Pinocchio's own `AccountView`, over the same kind of input region the Prover gives
//! each of `cvlr-solana`'s accounts.
//!
//! An `AccountView` is a pointer to the runtime's serialized account: an 88-byte header (borrow
//! state, flags, four bytes the entrypoint may use, address, owner, lamports, data length), then
//! the data, then room for the data to grow. A region laid out the same way is an account.

use core::mem::size_of;

use cvlr::prelude::*;
use pinocchio::account::{AccountView, RuntimeAccount, MAX_PERMITTED_DATA_INCREASE, NOT_BORROWED};

extern "C" {
    fn CVT_nondet_solana_account_space(size: usize) -> *mut u8;
}

/// The largest data length an account built here can have. The region also holds the header,
/// the data's room to grow, and the trailing rent epoch.
pub const MAX_DATA_LEN: usize = 8 * 1024 * 1024;

const REGION_SIZE: usize =
    size_of::<RuntimeAccount>() + MAX_DATA_LEN + MAX_PERMITTED_DATA_INCREASE + 8;

/// One account with arbitrary contents, in a region of its own.
///
/// The address, owner, flags, lamports and data are unconstrained, apart from what the runtime
/// guarantees: the account is not borrowed, each flag is 0 or 1, the original data length is
/// recorded where the entrypoint keeps it, and the data fits the region.
/// Two accounts may have the same address: assume `a.address() != b.address()` where the program
/// relies on distinct accounts.
pub fn cvlr_pinocchio_account() -> AccountView {
    // SAFETY: the region is the size of a serialized account with `MAX_DATA_LEN` bytes of data,
    // and its header is constrained to describe data that fits in it. The region is never freed.
    unsafe {
        let raw = CVT_nondet_solana_account_space(REGION_SIZE).cast::<RuntimeAccount>();
        cvlr_assume!((*raw).borrow_state == NOT_BORROWED);
        cvlr_assume!((*raw).is_signer <= 1);
        cvlr_assume!((*raw).is_writable <= 1);
        cvlr_assume!((*raw).executable <= 1);
        cvlr_assume!((*raw).data_len <= MAX_DATA_LEN as u64);
        // What the entrypoint stores under `account-resize`, the original data length. Without
        // that feature nothing reads these bytes.
        cvlr_assume!(u32::from_le_bytes((*raw).padding) as u64 == (*raw).data_len);
        AccountView::new_unchecked(raw)
    }
}

/// Sixteen accounts, each a separate [`cvlr_pinocchio_account`]. Pass a prefix of the array to a
/// handler that takes `&[AccountView]`, and build more with [`cvlr_pinocchio_account`] when an
/// instruction takes more than sixteen.
pub fn cvlr_pinocchio_accounts() -> [AccountView; 16] {
    [
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
        cvlr_pinocchio_account(),
    ]
}
