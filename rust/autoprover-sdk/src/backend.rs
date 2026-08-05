//! The trait an application implements.

use std::collections::BTreeMap;

use crate::args::AppArgs;
use crate::authoring::{AuthorInput, Failure, Prompt};
use crate::descriptor::AppDescriptor;
use crate::finalize::FinalizeInput;
use crate::outcome::{CompileResult, Unit, ValidateOutcome};
use crate::prep::{SandboxGrants, WorkspacePrep};
use crate::sandbox::Sandbox;

/// A Rust AutoProver backend — a **passive service** the Python pipeline drives. One instance
/// per wheel; construct it in [`export_app!`](crate::export_app). Metadata/authoring callouts are
/// pure; `compile` and `validate` run the toolchain (via [`run_confined`](crate::run_confined)) and
/// BLOCK — the host calls them off the event loop (`asyncio.to_thread`) while the wheel releases
/// the GIL.
pub trait Backend: Send + Sync + 'static {
    /// The declaration the Python host reads at load time.
    fn descriptor(&self) -> AppDescriptor;

    /// Validate application-specific preconditions before any service opens. `Err(msg)` aborts.
    fn validate_preconditions(&self, _args: &AppArgs) -> Result<(), String> {
        Ok(())
    }

    /// The units this input formalizes — one per property — each a property title and its
    /// unit name (Crucible: `c_<slug>`). Pure and pre-authoring: the prompt requires exactly
    /// these fn names, the host validates each, and it is the report's property→unit map.
    fn units(&self, input: &AuthorInput) -> Vec<Unit>;

    /// The instruction (+ optional system prompt) to author `input.kind`'s spec, covering all
    /// its units. `failure = Some(..)` on a re-author after a compile failure / judge rejection.
    fn author_prompt(&self, input: &AuthorInput, failure: Option<&Failure>) -> Prompt;

    /// Optional LLM review of a compiled spec, before validation. `None` (the default) skips
    /// judging — the compiler + checker are the judges.
    fn judge_prompt(&self, _input: &AuthorInput, _spec: &str) -> Option<Prompt> {
        None
    }

    /// Compile/typecheck the whole spec once (all units share one build). BLOCKING.
    ///
    /// Also the preflight gate: for `input.kind == "preflight"` the `spec` is empty and the wheel
    /// supplies its own skeleton, so one implementation covers "does the authored artifact build"
    /// and "could *any* artifact build here" (see [`PreflightSpec`](crate::PreflightSpec)).
    fn compile(
        &self,
        input: &AuthorInput,
        spec: &str,
        workdir: &std::path::Path,
        sandbox: &Sandbox,
    ) -> CompileResult;

    /// Build + check ONE unit against the spec (the fused build gate — no separate compile for
    /// components). Returns [`ValidateOutcome::BuildFailed`] to trigger a re-author of the whole
    /// spec (the build is shared across units), or a per-unit [`Verdict`](crate::Verdict). Per-unit
    /// so the host owns enumeration/scheduling. BLOCKING.
    fn validate(
        &self,
        input: &AuthorInput,
        spec: &str,
        unit: &str,
        workdir: &std::path::Path,
        sandbox: &Sandbox,
    ) -> ValidateOutcome;

    /// Extra sandbox grants to union into the host's policy (see [`SandboxGrants`]). Pure; called
    /// once before any confined step. Default: no extra grants.
    fn sandbox_grants(&self, _args: &AppArgs) -> SandboxGrants {
        SandboxGrants::default()
    }

    /// Declare the pre-formalization workspace prep (see [`WorkspacePrep`]). Pure — the host
    /// executes the returned plan with the shared warm/build helpers, so the network posture
    /// stays Python-owned. Default: nothing to prepare.
    fn workspace_prep(&self, _input: &AuthorInput) -> WorkspacePrep {
        WorkspacePrep::default()
    }

    /// Optional run-level artifacts from the full outcome set, as `{relpath: contents}`.
    ///
    /// Under [`DeliverableMode::Callout`](crate::DeliverableMode::Callout) this renders the whole
    /// source deliverable (Crucible's one crate) — which is why the outcome set carries each
    /// component's authored spec and targets alongside the setup artifact and the program's crate.
    fn finalize(&self, _outcomes: &FinalizeInput) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}
