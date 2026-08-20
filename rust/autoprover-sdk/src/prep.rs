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

/// What a wheel needs to render its build's *scaffolding* once, at the one moment both halves of it
/// are known: after the shared setup spec is authored, and before the units fan out.
///
/// This exists because scaffolding for a multi-unit build depends on the **whole unit set** — a
/// Cargo manifest's feature list, a crate root's module declarations — which no per-unit callout can
/// see. Without it a wheel must re-render the scaffolding on every gated build, from the one unit it
/// happens to be holding, which means the artifact the run produces is only ever assembled for real
/// at the end (`docs/crucible.md` §4 is what that cost).
///
/// The host writes the returned files under the workdir and does not write them again, so a wheel
/// that implements [`Backend::crate_root`](crate::Backend::crate_root) can have its per-unit
/// callouts emit only that unit's own files.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct CrateRootInput {
    /// As every authoring callout receives it
    /// (see [`AuthorInput::program`](crate::authoring::AuthorInput::program)).
    pub program: String,
    /// As every authoring callout receives it.
    pub source_unit: ChainData,
    /// As every authoring callout receives it.
    pub prep_facts: ChainData,
    /// The compiled shared setup spec, for a wheel that declared a
    /// [`PhaseRole::Setup`](crate::descriptor::PhaseRole::Setup) phase.
    #[serde(deserialize_with = "crate::required::present")]
    pub setup: Option<String>,
    /// Every unit the run is about to formalize, in the order the host will fan them out — each the
    /// same [chain-shaped](ChainData) value a component callout gets as
    /// [`Authored::Component::unit`](crate::authoring::Authored::Component). This is the field the
    /// hook exists for: it is the only place a wheel sees the unit set whole.
    pub units: Vec<ChainData>,
    /// Every property the run extracted, each naming the unit that owns it — the same set the setup
    /// gate is sent as [`AuthorInput::props`](crate::authoring::AuthorInput::props).
    ///
    /// Here because a wheel whose scaffolding names something per *property* cannot render it from
    /// the unit set alone, and this hook must re-emit byte-identically what that gate produced.
    /// Crucible declares one build target per check, and a check is named after the property it
    /// carries.
    #[serde(default)]
    pub props: Vec<crate::authoring::Property>,
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
