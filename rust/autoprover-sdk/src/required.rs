//! The one piece of machinery the protocol's "every field is present" rule needs.
//!
//! Both halves of this seam ship together, so a payload missing a field is never version skew — it
//! is a mirror that drifted. Every wire type therefore requires every field and rejects unknown
//! ones (`#[serde(deny_unknown_fields)]`), which turns a one-sided field into an error at the first
//! callout, naming it, instead of a default that reads as data forever.
//!
//! Plain fields get that for free: without `#[serde(default)]`, serde already fails on a missing
//! key. `Option<T>` does not — serde deserializes a missing field of that type as `None` whether or
//! not the attribute is there, because its missing-field handling deserializes from a unit-like
//! deserializer that `Option` accepts. [`present`] is how such a field opts back in: it routes
//! through `deserialize_with`, which serde cannot satisfy from a missing key, so absence is an
//! error and an empty value must be spelled `null`.

use serde::{Deserialize, Deserializer};

/// An `Option<T>` field that must still be **present** on the wire — `null` when it holds nothing.
///
/// Used as `#[serde(deserialize_with = "crate::required::present")]`. Pair it with *no*
/// `skip_serializing_if`, so the key is always written and the two sides never have to agree on
/// whether an absent key and a null one mean the same thing.
pub fn present<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}
