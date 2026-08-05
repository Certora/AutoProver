//! Pure declarations the *host* executes on the wheel's behalf — the workspace plan and the
//! sandbox grants unioned into the Python-authored policy. Nothing here runs a command line.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A pure plan for preparing the workspace before formalization (Crucible: place the harness
/// `Cargo.toml`, warm its deps, build the program `.so`). The wheel *declares* the plan; the
/// **host executes it with the shared toolchain helpers**, so the standard network posture holds
/// without the wheel touching a command line: dependency fetches run *unconfined* (network, no
/// untrusted code), and the code-executing build runs *confined + offline*
/// (`docs/command-sandbox.md` §5). This keeps warming out of confinement — the codebase never
/// gives a confined process network — while still letting a pure-Rust app own its layout.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WorkspacePrep {
    /// Files to write under the workdir (path-confined) before warming — e.g. the harness
    /// `Cargo.toml` (whose deps only the wheel knows). Contents only; no command line.
    #[serde(default)]
    pub files: BTreeMap<String, String>,
    /// Project-relative manifest dirs to `cargo fetch` (unconfined, network) so a later
    /// confined + offline build finds every dep warm in the private `CARGO_HOME`.
    #[serde(default)]
    pub warm_dirs: Vec<String>,
    /// If set, build the workspace and expect this artifact, via the host's shared build
    /// capability (Solana: `cargo-build-sbf` → `target/deploy/<name>.so`). It names the *build
    /// artifact*, so for Cargo it is the crate's lib target
    /// ([`ProgramCrate::lib`](crate::authoring::ProgramCrate::lib)) — not the analysis identifier,
    /// which need not match it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub build_program: Option<String>,
    /// If set, the wheel needs the program's **IDL** and wants it at this workdir-relative path.
    /// The host obtains it (a user-supplied file, else the build capability's IDL build), writes it
    /// there, and echoes the path back as [`AuthorInput::idl`](crate::authoring::AuthorInput::idl)
    /// on every later callout — so a set `idl` means "the file is in place". A hard error if it
    /// can't be produced: the wheel only asks when it cannot proceed without one.
    ///
    /// This is what lets a harness target a program whose toolchain it can't link against
    /// ([`ProgramCrate::anchor`](crate::authoring::ProgramCrate::anchor)): types generated from the
    /// IDL belong to the *wheel's* stack, so the program's own dependency graph never enters the
    /// harness build.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub idl_dest: Option<String>,
}

/// Extra sandbox grants a wheel needs unioned into the host-authored policy (Crucible: the
/// crucible checkout + the `crucible` binary dir as read-only). Pure data — the wheel declares
/// grants, Python decides the policy; the wheel never invents confinement.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SandboxGrants {
    /// Extra read-only paths.
    #[serde(default)]
    pub extra_ro: Vec<String>,
    /// Extra `NAME=VALUE` env entries to pass through confinement.
    #[serde(default)]
    pub extra_env: Vec<String>,
}
