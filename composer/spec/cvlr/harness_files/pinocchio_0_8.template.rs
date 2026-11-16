//! Nondet accounts for a Pinocchio program (Pinocchio 0.8).
//!
//! `cvlr-solana` builds the SDK's `AccountInfo`, which a Pinocchio handler cannot take. These
//! helpers build Pinocchio's own `AccountInfo`, over the same kind of input region the Prover gives
//! each of `cvlr-solana`'s accounts.
//!
//! Pinocchio's `AccountInfo` is a pointer to the runtime's serialized account: an 88-byte header,
//! then the data, then room for the data to grow. The header's type is private to Pinocchio, so
//! its fields are reached here by offset:
//!
//! | offset | field                                                      |
//! |--------|------------------------------------------------------------|
//! | 0      | borrow state                                               |
//! | 1..4   | `is_signer`, `is_writable`, `executable`                   |
//! | 4      | original data length, set on first use                     |
//! | 8      | key, owner, lamports, data length                          |

use cvlr::prelude::*;
use pinocchio::account_info::{AccountInfo, MAX_PERMITTED_DATA_INCREASE};

extern "C" {
    fn CVT_nondet_solana_account_space(size: usize) -> *mut u8;
}

/// The largest data length an account built here can have. The region also holds the header,
/// the data's room to grow, and the trailing rent epoch.
pub const MAX_DATA_LEN: usize = 8 * 1024 * 1024;

const HEADER_SIZE: usize = 88;

const REGION_SIZE: usize = HEADER_SIZE + MAX_DATA_LEN + MAX_PERMITTED_DATA_INCREASE + 8;

/// What the 0.8 entrypoint stores over the runtime's non-duplicate marker: any bit set reads as a
/// borrow.
const NOT_BORROWED: u8 = 0;

/// One account with arbitrary contents, in a region of its own.
///
/// The key, owner, flags, lamports and data are unconstrained, apart from what the runtime
/// guarantees: the account is not borrowed, each flag is 0 or 1, the four bytes after the flags
/// are zero, and the data fits the region. Two accounts may have the same key: assume
/// `a.key() != b.key()` where the program relies on distinct accounts.
pub fn cvlr_pinocchio_account() -> AccountInfo {
    // SAFETY: the region is the size of a serialized account with `MAX_DATA_LEN` bytes of data,
    // and its header is constrained to describe data that fits in it. `AccountInfo` is a
    // `#[repr(C)]` struct holding only a pointer to the header, and the region is never freed.
    unsafe {
        let region = CVT_nondet_solana_account_space(REGION_SIZE);
        cvlr_assume!(*region == NOT_BORROWED);
        cvlr_assume!(*region.add(1) <= 1);
        cvlr_assume!(*region.add(2) <= 1);
        cvlr_assume!(*region.add(3) <= 1);
        cvlr_assume!(*region.add(4).cast::<u32>() == 0);
        let account = core::mem::transmute::<*mut u8, AccountInfo>(region);
        cvlr_assume!(account.data_len() <= MAX_DATA_LEN);
        account
    }
}

/// Sixteen accounts, each a separate [`cvlr_pinocchio_account`]. Pass a prefix of the array to a
/// handler that takes `&[AccountInfo]`, and build more with [`cvlr_pinocchio_account`] when an
/// instruction takes more than sixteen.
pub fn cvlr_pinocchio_accounts() -> [AccountInfo; 16] {
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
