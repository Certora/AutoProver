//! The trait an application implements.

use std::collections::BTreeMap;

use crate::args::AppArgs;
use crate::authoring::{AuthorInput, Judge, Prompt};
use crate::descriptor::AppDescriptor;
use crate::finalize::FinalizeInput;
use crate::outcome::{CompileResult, Target, ValidateOutcome};
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

    /// Which invocation of the checker a declared check runs under — `None` (the default) makes it
    /// its own. Pure; asked once per check name the author declared.
    ///
    /// This is the whole of what a wheel says about the check set: the *names* are the author's
    /// (they are names of things in the artifact, so only the author can know them), while how they
    /// are grouped into runs is a backend convention. Crucible answers with the component's harness
    /// fn — one campaign for the whole property set; a CVL wheel would answer with the component's
    /// conf.
    fn target_for(&self, _input: &AuthorInput, _check: &str) -> Option<String> {
        None
    }

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

    /// Who reviews this input's drafts, before validation — `None` (the default) skips judging, and
    /// the compiler + checker are the judges.
    ///
    /// Asked once, when the authoring session is built: whether this kind of input is reviewed at
    /// all and who reviews it are both fixed for the session, so neither is given a draft — none
    /// exists yet. A wheel can answer per kind (review components, not the shared setup spec).
    fn judge(&self, _input: &AuthorInput) -> Option<Judge> {
        None
    }

    /// What to ask the reviewer about this draft — asked once per review round, and only for an
    /// input [`Backend::judge`] claimed. The default asks for a plain review, which is what a wheel
    /// with nothing input-specific to say wants.
    fn judge_instruction(&self, _input: &AuthorInput, _spec: &str) -> String {
        "Review the proposed specification and decide whether it is acceptable as it stands.".into()
    }

    /// Compile/typecheck the whole spec once (every check in it shares one build). BLOCKING.
    ///
    /// Also the preflight gate: for [`Authored::Preflight`](crate::authoring::Authored::Preflight)
    /// the `spec` is `None` — nothing is authored, and the wheel supplies its own skeleton — so one
    /// implementation covers "does the authored artifact build" and "could *any* artifact build
    /// here" (see [`PhaseRole::Preflight`](crate::descriptor::PhaseRole::Preflight)). A wheel whose
    /// toolchain would take an empty spec file for a real one can tell the two apart.
    fn compile(&self, input: &AuthorInput, spec: Option<&str>, ws: &Workspace) -> CompileResult;

    /// Build + check ONE target against the spec (the fused build gate — no separate compile for
    /// components). Returns [`ValidateOutcome::BuildFailed`] to trigger a revision of the whole
    /// spec (the build is shared across targets), or a [`Verdict`](crate::outcome::Verdict) for
    /// each check the target covers — [`Target::checks`], which the host already grouped.
    /// Per-target so the host owns scheduling. BLOCKING.
    ///
    /// # The verdict contract
    ///
    /// The check names come from the author, so this is also where a name is held to the artifact.
    /// A backend must not answer [`Outcome::Good`](crate::outcome::Outcome::Good) for a check it
    /// found no evidence ran:
    ///
    /// | Situation | Verdict |
    /// |---|---|
    /// | The checker has no such check | `Error`, with a detail naming it |
    /// | It exists but the run never exercised it | `Unknown`, with a detail saying so |
    /// | It ran and held | `Good` |
    /// | It ran and was refuted | `Bad` + the counterexample |
    ///
    /// Both non-`Good` cases block the publish gate, which is the point: a declared check with
    /// nothing behind it must not stamp a property as verified. What corroborates a check is the
    /// backend's own business — a per-rule result from the Prover, a runtime tally of which tagged
    /// assertions a campaign evaluated — but *some* evidence is required, and a source-text scan is
    /// not it (a tag in a comment or in dead code reads exactly like a check).
    ///
    /// The residue no mechanism reaches — whether a check that demonstrably ran genuinely verifies
    /// the property it claims — is the judge's ([`Backend::judge`]).
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
