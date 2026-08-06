//! The trait an application implements.

use std::collections::BTreeMap;

use crate::args::AppArgs;
use crate::authoring::{AuthorInput, Prompt};
use crate::descriptor::AppDescriptor;
use crate::finalize::FinalizeInput;
use crate::outcome::{Check, CompileResult, Target, ValidateOutcome};
use crate::prep::{SandboxGrants, WorkspacePrep};
use crate::sandbox::Workspace;

/// A Rust AutoProver backend — a **passive service** the Python pipeline drives. One instance
/// per wheel; construct it in [`export_app!`](crate::export_app). Metadata/authoring callouts are
/// pure; `compile` and `validate` run the toolchain (via [`Workspace::run`]) and BLOCK — the host
/// calls them off the event loop (`asyncio.to_thread`) while the wheel releases the GIL.
pub trait Backend: Send + Sync + 'static {
    /// The declaration the Python host reads at load time.
    fn descriptor(&self) -> AppDescriptor;

    /// Validate application-specific preconditions before any service opens. `Err(msg)` aborts.
    fn validate_preconditions(&self, _args: &AppArgs) -> Result<(), String> {
        Ok(())
    }

    /// The checks this input formalizes — one or more per property — each a property title and the
    /// name of the check that carries it. Pure and pre-authoring: the author is required to produce
    /// exactly these names, the publish gate validates its mapping against them, and they are the
    /// report's property→check map.
    fn checks(&self, input: &AuthorInput) -> Vec<Check>;

    /// The instruction (+ optional system prompt) to author `input.kind`'s spec, covering all its
    /// units.
    ///
    /// Asked once per authoring session, not once per attempt: the host runs a stateful agent that
    /// keeps its buffer and its history across revisions, so build errors and review feedback reach
    /// the author as tool results rather than as a re-rendered prompt. `system` is the *domain*
    /// half of the system prompt — the host prepends the session protocol (the tools, the publish
    /// gate, the citation rules) so no wheel restates it.
    fn author_prompt(&self, input: &AuthorInput) -> Prompt;

    /// Reject a spec at write time, cheaply and purely — `Some(complaint)` refuses the write and
    /// the buffer keeps its previous contents. Default: accept anything, and let `validate` be the
    /// only judge.
    ///
    /// This is for what can be decided without a toolchain (a parser, a required declaration). It
    /// is not a build: it runs on every put and edit, so it must be fast, and it must not spawn
    /// anything.
    fn check_syntax(&self, _input: &AuthorInput, _spec: &str) -> Option<String> {
        None
    }

    /// Optional LLM review of a compiled spec, before validation. `None` (the default) skips
    /// judging — the compiler + checker are the judges.
    fn judge_prompt(&self, _input: &AuthorInput, _spec: &str) -> Option<Prompt> {
        None
    }

    /// Compile/typecheck the whole spec once (every check in it shares one build). BLOCKING.
    ///
    /// Also the preflight gate: for [`Authored::Preflight`](crate::authoring::Authored::Preflight)
    /// the `spec` is empty and the wheel supplies its own skeleton, so one implementation covers
    /// "does the authored artifact build" and "could *any* artifact build here" (see
    /// [`PhaseRole::Preflight`](crate::descriptor::PhaseRole::Preflight)).
    fn compile(&self, input: &AuthorInput, spec: &str, ws: &Workspace) -> CompileResult;

    /// Build + check ONE target against the spec (the fused build gate — no separate compile for
    /// components). Returns [`ValidateOutcome::BuildFailed`] to trigger a revision of the whole
    /// spec (the build is shared across targets), or a [`Verdict`](crate::outcome::Verdict) for
    /// each check the target covers — [`Target::checks`], which the host already grouped.
    /// Per-target so the host owns enumeration and scheduling. BLOCKING.
    fn validate(
        &self,
        input: &AuthorInput,
        spec: &str,
        target: &Target,
        ws: &Workspace,
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
    /// Under [`DeliverableMode::Callout`](crate::descriptor::DeliverableMode::Callout) this renders
    /// the whole source deliverable (Crucible's one crate) — which is why the outcome set carries
    /// each component's authored spec and targets alongside the setup spec and the crate.
    fn finalize(&self, _outcomes: &FinalizeInput) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}
