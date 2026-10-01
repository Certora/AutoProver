//! Certora verification harness.
//!
//! Compiled only under the `certora` feature, which `lib.rs` gates this module on.

mod log;
pub mod mocks;
mod nondet;
pub mod specs;
