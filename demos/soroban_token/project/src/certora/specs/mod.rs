//! The rules. One module per unit, declared here by the CVLR backend.
//!
//! Each module is gated on its own cargo feature, so a build selects exactly one
//! unit's rules. That is what lets every unit share one working tree: a module
//! behind a disabled `cfg` is never compiled and never enters rustc's dep-info, so
//! one unit's draft cannot break or dirty another's build.
//!
//! Written once for the whole run rather than appended to per unit.

#[cfg(feature = "unit_admin_setup")]
pub mod admin_setup;
