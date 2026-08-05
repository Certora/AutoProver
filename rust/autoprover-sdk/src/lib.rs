//! # autoprover-sdk
//!
//! The library a Rust-based AutoProver application imports. It defines the seam
//! between a Rust backend and the generic Python pipeline
//! (`composer/pipeline/core.py`), realized over a **synchronous, JSON** FFI
//! boundary — the service-shaped design in `docs/rust-applications.md`.
//!
//! The backend is a **passive service**, not a driver: the Python pipeline owns the
//! author→compile→judge→validate loop and every LLM turn, and calls the backend's
//! callouts. Most are pure ([`Backend::descriptor`], [`Backend::units`],
//! [`Backend::author_prompt`], [`Backend::judge_prompt`], [`Backend::finalize`]). The
//! two gating callouts ([`Backend::compile`], [`Backend::validate`]) run the toolchain
//! directly — each spawns the `run-confined` launcher via [`run_confined`] — and BLOCK;
//! the host calls them off the event loop (`asyncio.to_thread`) while the wheel releases
//! the GIL. There is no `async`/`pyo3-async` bridge and no `Command`/`Observation` resume
//! protocol on the Rust side.
//!
//! An application implements [`Backend`] and calls [`export_app!`] to emit the PyO3
//! module the Python host loads.
//!
//! Every item below is re-exported at the crate root; the modules only group them:
//!
//!  * [`descriptor`] — the declaration the host reads at load time.
//!  * [`args`] — the run's resolved inputs (project root, program, declared flags).
//!  * [`authoring`] — what the host sends into a callout, and the prompt it gets back.
//!  * [`outcome`] — the compile verdict, the property→unit map, the per-unit verdicts.
//!  * [`finalize`] — the full outcome set the run-level deliverable is rendered from.
//!  * [`prep`] — pure plans the host executes for the wheel (workspace prep, sandbox grants).
//!  * [`sandbox`] — the confinement wrapper and the launcher helper.
//!  * [`backend`] — the [`Backend`] trait itself.
//!  * [`ffi`] — the JSON-string boundary [`export_app!`] wraps.

pub mod args;
pub mod authoring;
pub mod backend;
pub mod descriptor;
pub mod export;
pub mod ffi;
pub mod finalize;
pub mod outcome;
pub mod prep;
pub mod sandbox;

pub use args::{AppArgs, DeclaredArgs};
pub use authoring::{
    anchor_compat_key, AuthorInput, Authored, Failure, FailureKind, ProgramCrate, Prompt, Property,
};
pub use backend::Backend;
pub use descriptor::{
    AppDescriptor, ArgDefault, ArgSpec, ArtifactLayout, CoreSlot, DeliverableMode, EventKind,
    PhaseSpec, PreflightSpec, SetupSpec,
};
pub use finalize::{ComponentOutcome, Delivered, FinalizeComponent, FinalizeInput};
pub use ffi::{
    ffi_author_prompt, ffi_compile, ffi_descriptor, ffi_finalize, ffi_judge_prompt,
    ffi_sandbox_grants, ffi_units, ffi_validate, ffi_validate_preconditions, ffi_workspace_prep,
};
pub use outcome::{CompileResult, Outcome, Unit, ValidateOutcome, Verdict};
pub use prep::{SandboxGrants, WorkspacePrep};
pub use sandbox::{run_confined, CommandOutput, Sandbox};

/// Re-exported so [`export_app!`] can reference `$crate::pyo3::…`; an app crate
/// still depends on pyo3 directly to enable `extension-module` / `abi3-py312`.
pub use pyo3;
