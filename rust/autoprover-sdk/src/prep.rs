//! Pure declarations the *host* executes on the wheel's behalf — the workspace plan and the
//! sandbox grants unioned into the Python-authored policy. Nothing here runs a command line.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

use crate::chain::ChainData;

/// A pure plan for preparing the workspace before formalization (Crucible: place the harness
/// manifest, warm its deps, build the program). The wheel *declares* the plan; the **host executes
/// it**, so the standard network posture holds without the wheel touching a command line:
/// dependency fetches run *unconfined* (network, no untrusted code), and anything that compiles runs
/// *confined + offline* (`docs/command-sandbox.md` §5). This keeps warming out of confinement — the
/// codebase never gives a confined process network — while still letting a pure-Rust app own its
/// layout.
///
/// Two halves, split by who can execute them: writing files is the same everywhere, while preparing
/// a *project* means driving a build system the host does not understand, which only that chain's
/// registered `ProjectToolchain` can do.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct WorkspacePrep {
    /// Files to write under the workdir (path-confined) before anything else — e.g. the harness
    /// manifest, whose contents only the wheel knows. Contents only; no command line.
    pub files: BTreeMap<String, String>,
    /// What the chain's `ProjectToolchain` should do beyond writing [`WorkspacePrep::files`]: warm a
    /// dependency cache, build the program, derive a client from it. Empty asks for nothing, and a
    /// plan that only places files is complete once they are written.
    ///
    /// [Chain-shaped](ChainData) — build one with [`ChainData::of`] from the request type your
    /// chain's support crate defines (Solana: `{warm_dirs, build_program, idl_dest}`). The host
    /// forwards it without a schema and only asks whether it is empty, which is what keeps a new
    /// ecosystem a registration rather than a field here. Whatever the toolchain establishes comes
    /// back on every later callout as
    /// [`AuthorInput::prep_facts`](crate::authoring::AuthorInput::prep_facts).
    ///
    /// A request that cannot be carried out is a hard error, not a silent skip: a wheel asks only
    /// when it cannot proceed without the result.
    pub toolchain_request: ChainData,
}

/// Extra sandbox grants a wheel needs unioned into the host-authored policy (Crucible: the
/// crucible checkout + the `crucible` binary dir as read-only). Pure data — the wheel declares
/// grants, Python decides the policy; the wheel never invents confinement.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct SandboxGrants {
    /// Extra read-only paths.
    pub extra_ro: Vec<String>,
    /// Extra env variable *names* to pass through confinement — the host unions these into its
    /// passthrough list, so a value is inherited from the ambient environment, never supplied here.
    pub extra_env: Vec<String>,
}
