//! The sync, JSON-string boundary. [`export_app!`](crate::export_app) wraps these in
//! `#[pyfunction]`s (compile/validate release the GIL); also unit-testable without Python.

use crate::args::AppArgs;
use crate::authoring::{AuthorInput, Failure, Prompt};
use crate::backend::Backend;
use crate::finalize::FinalizeInput;
use crate::outcome::{CompileResult, Outcome, ValidateOutcome, Verdict};
use crate::sandbox::Sandbox;

fn parse<T: serde::de::DeserializeOwned>(json: &str, what: &str) -> Result<T, String> {
    serde_json::from_str(json).map_err(|e| format!("invalid {what} JSON: {e}"))
}

/// Parse an [`AuthorInput`] with its `program_crate` normalized, so the partly-empty shape a host
/// that resolved nothing may send never reaches a backend. This is the one place
/// [`ProgramCrate::resolved`](crate::ProgramCrate::resolved) has to be remembered.
fn parse_input(json: &str) -> Result<AuthorInput, String> {
    let mut input: AuthorInput = parse(json, "AuthorInput")?;
    input.program_crate = input.program_crate.resolved(&input.program);
    Ok(input)
}

/// Parse [`AppArgs`], normalizing its `program_crate` for the same reason as [`parse_input`].
fn parse_args(json: &str) -> Result<AppArgs, String> {
    let mut args: AppArgs = parse(json, "AppArgs")?;
    args.program_crate = args.program_crate.resolved(&args.program);
    Ok(args)
}

/// `descriptor() -> str` (JSON).
pub fn ffi_descriptor(b: &dyn Backend) -> String {
    serde_json::to_string(&b.descriptor())
        .unwrap_or_else(|e| format!("{{\"error\":\"descriptor serialize: {e}\"}}"))
}

/// `validate_preconditions(args_json) -> str | None` (None = ok). A payload this can't parse is a
/// host bug, and it is reported as a failed precondition — the one callout with a channel to say so.
pub fn ffi_validate_preconditions(b: &dyn Backend, args_json: &str) -> Option<String> {
    match parse_args(args_json) {
        Ok(args) => b.validate_preconditions(&args).err(),
        Err(e) => Some(e),
    }
}

/// `units(input_json) -> str` (JSON `[Unit]`).
pub fn ffi_units(b: &dyn Backend, input_json: &str) -> String {
    match parse_input(input_json) {
        Ok(input) => serde_json::to_string(&b.units(&input)).unwrap_or_else(|_| "[]".into()),
        Err(_) => "[]".into(),
    }
}

/// `author_prompt(input_json, failure_json | None) -> str` (JSON `Prompt`).
pub fn ffi_author_prompt(b: &dyn Backend, input_json: &str, failure_json: Option<&str>) -> String {
    let input = match parse_input(input_json) {
        Ok(v) => v,
        Err(e) => {
            return serde_json::to_string(&Prompt { system: None, instruction: format!("ERROR: {e}") })
                .unwrap_or_default()
        }
    };
    let failure: Option<Failure> = failure_json.and_then(|s| serde_json::from_str(s).ok());
    let prompt = b.author_prompt(&input, failure.as_ref());
    serde_json::to_string(&prompt).unwrap_or_default()
}

/// `judge_prompt(input_json, spec) -> str | None` (None = skip judging).
pub fn ffi_judge_prompt(b: &dyn Backend, input_json: &str, spec: &str) -> Option<String> {
    let input = parse_input(input_json).ok()?;
    b.judge_prompt(&input, spec)
        .map(|p| serde_json::to_string(&p).unwrap_or_default())
}

/// `compile(input_json, spec, workdir, sandbox_json) -> str` (JSON `CompileResult`). BLOCKING.
pub fn ffi_compile(
    b: &dyn Backend,
    input_json: &str,
    spec: &str,
    workdir: &str,
    sandbox_json: &str,
) -> String {
    let input = match parse_input(input_json) {
        Ok(v) => v,
        Err(e) => return serde_json::to_string(&CompileResult::Failed { errors: e }).unwrap_or_default(),
    };
    let sandbox: Sandbox = parse(sandbox_json, "Sandbox").unwrap_or_default();
    let r = b.compile(&input, spec, std::path::Path::new(workdir), &sandbox);
    serde_json::to_string(&r).unwrap_or_else(|e| {
        serde_json::to_string(&CompileResult::Failed { errors: e.to_string() }).unwrap_or_default()
    })
}

/// `validate(input_json, spec, unit, workdir, sandbox_json) -> str` (JSON `ValidateOutcome`). BLOCKING.
pub fn ffi_validate(
    b: &dyn Backend,
    input_json: &str,
    spec: &str,
    unit: &str,
    workdir: &str,
    sandbox_json: &str,
) -> String {
    let sandbox: Sandbox = parse(sandbox_json, "Sandbox").unwrap_or_default();
    let outcome = match parse_input(input_json) {
        Ok(input) => b.validate(&input, spec, unit, std::path::Path::new(workdir), &sandbox),
        Err(e) => ValidateOutcome::Verdicts {
            verdicts: vec![(unit.to_string(), Verdict::detailed(Outcome::Error, e))],
        },
    };
    serde_json::to_string(&outcome).unwrap_or_default()
}

/// `sandbox_grants(args_json) -> str` (JSON `SandboxGrants`).
pub fn ffi_sandbox_grants(b: &dyn Backend, args_json: &str) -> String {
    let args = parse_args(args_json).unwrap_or_default();
    serde_json::to_string(&b.sandbox_grants(&args)).unwrap_or_else(|_| "{}".into())
}

/// `workspace_prep(input_json) -> str` (JSON `WorkspacePrep`). Pure.
pub fn ffi_workspace_prep(b: &dyn Backend, input_json: &str) -> String {
    match parse_input(input_json) {
        Ok(input) => serde_json::to_string(&b.workspace_prep(&input)).unwrap_or_else(|_| "{}".into()),
        Err(_) => "{}".into(),
    }
}

/// `finalize(outcomes_json) -> str | None` (JSON `{relpath: contents}`, or None).
pub fn ffi_finalize(b: &dyn Backend, outcomes_json: &str) -> Option<String> {
    let mut outcomes: FinalizeInput = parse(outcomes_json, "FinalizeInput").ok()?;
    outcomes.program_crate = outcomes.program_crate.resolved(&outcomes.program);
    let files = b.finalize(&outcomes);
    if files.is_empty() {
        None
    } else {
        serde_json::to_string(&files).ok()
    }
}

#[cfg(test)]
mod tests {
    //! The boundary's own guarantees: what a backend receives is normalized, whatever the host
    //! managed to resolve.
    use super::*;
    use crate::authoring::ProgramCrate;
    use crate::descriptor::AppDescriptor;
    use crate::finalize::ComponentOutcome;
    use crate::outcome::Unit;
    use crate::prep::WorkspacePrep;
    use std::sync::Mutex;

    /// Records the crate each callout was handed, so a test can assert on what crossed the seam.
    #[derive(Default)]
    struct Spy {
        seen: Mutex<Vec<ProgramCrate>>,
    }

    impl Backend for Spy {
        fn descriptor(&self) -> AppDescriptor {
            unimplemented!("not exercised")
        }
        fn units(&self, input: &AuthorInput) -> Vec<Unit> {
            self.seen.lock().unwrap().push(input.program_crate.clone());
            Vec::new()
        }
        fn author_prompt(&self, _input: &AuthorInput, _failure: Option<&Failure>) -> Prompt {
            unimplemented!("not exercised")
        }
        fn compile(
            &self,
            _input: &AuthorInput,
            _spec: &str,
            _workdir: &std::path::Path,
            _sandbox: &Sandbox,
        ) -> CompileResult {
            unimplemented!("not exercised")
        }
        fn validate(
            &self,
            _input: &AuthorInput,
            _spec: &str,
            _unit: &str,
            _workdir: &std::path::Path,
            _sandbox: &Sandbox,
        ) -> ValidateOutcome {
            unimplemented!("not exercised")
        }
        fn workspace_prep(&self, input: &AuthorInput) -> WorkspacePrep {
            self.seen.lock().unwrap().push(input.program_crate.clone());
            WorkspacePrep::default()
        }
        fn validate_preconditions(&self, args: &AppArgs) -> Result<(), String> {
            self.seen.lock().unwrap().push(args.program_crate.clone());
            Ok(())
        }
    }

    #[test]
    fn a_backend_never_sees_an_unresolved_program_crate() {
        // The host resolved nothing (no manifest, or a language with no compilation unit). Every
        // callout still gets the `programs/<program>` fallback filled in — the whole point of
        // normalizing here rather than trusting each wheel to remember `resolved()`.
        let spy = Spy::default();
        ffi_units(&spy, r#"{"kind":"component","program":"vault"}"#);
        ffi_workspace_prep(&spy, r#"{"kind":"preflight","program":"vault"}"#);
        ffi_validate_preconditions(&spy, r#"{"project_root":"/p","program":"vault"}"#);
        for cr in spy.seen.lock().unwrap().iter() {
            assert_eq!(cr.dir, "programs/vault");
            assert_eq!(cr.package, "vault");
            assert_eq!(cr.lib, "vault");
        }
    }

    #[test]
    fn a_resolved_crate_crosses_the_boundary_untouched() {
        // The lend shape: directory, package and lib all differ from the analysis identifier, and
        // the convention would have got every one of them wrong.
        let spy = Spy::default();
        ffi_units(
            &spy,
            r#"{"kind":"component","program":"vault","program_crate":
                {"dir":"programs/lend","package":"example-lending","lib":"example_lending",
                 "anchor":"0.29.0"}}"#,
        );
        let seen = spy.seen.lock().unwrap();
        let cr = seen.first().expect("units was called");
        assert_eq!((cr.dir.as_str(), cr.package.as_str(), cr.lib.as_str()),
                   ("programs/lend", "example-lending", "example_lending"));
        assert_eq!(cr.anchor_compat(), Some((0, 29)));
    }

    #[test]
    fn an_input_carries_only_the_payload_its_kind_has() {
        // The host's wire shape is flat: `kind` selects the variant, and the field beside it
        // belongs to that variant alone. A component turn has no analyzed model to read, and a
        // preflight has neither — it runs before anything is analyzed.
        let comp: AuthorInput = serde_json::from_str(
            r#"{"kind":"component","program":"vault","unit":{"slug":"farms"},
                "setup":"struct Fixture {}","idl":"fuzz/vault/idls/vault.json",
                "args":{"fuzz_timeout":900}}"#,
        )
        .expect("parse");
        assert_eq!(comp.unit().and_then(|u| u.get("slug")).and_then(|v| v.as_str()), Some("farms"));
        assert!(comp.model().is_none());
        assert_eq!(comp.setup.as_deref(), Some("struct Fixture {}"));
        assert_eq!(comp.idl.as_deref(), Some("fuzz/vault/idls/vault.json"));
        assert_eq!(comp.args.get::<u64>("fuzz_timeout"), Some(900));
        // An absent flag and one left at a null default are the same answer.
        assert_eq!(comp.args.get::<u64>("nope"), None);

        let setup: AuthorInput =
            serde_json::from_str(r#"{"kind":"setup","program":"vault","model":{"components":[]}}"#)
                .expect("parse");
        assert!(setup.model().is_some() && setup.unit().is_none());
        assert_eq!(setup.idl, None, "no IDL placed is not the same as one at the empty path");

        let pre: AuthorInput =
            serde_json::from_str(r#"{"kind":"preflight","program":"vault"}"#).expect("parse");
        assert!(pre.unit().is_none() && pre.model().is_none() && pre.props.is_empty());

        // …and it round-trips flat, which is what the host parses back.
        let json = serde_json::to_value(&pre).expect("serialize");
        assert_eq!(json.get("kind").and_then(|v| v.as_str()), Some("preflight"));
    }

    #[test]
    fn a_malformed_args_payload_is_reported_as_a_failed_precondition() {
        // The only callout with a channel to say the host sent nonsense; the alternative is a
        // silent default that reads as "no preconditions to check".
        let err = ffi_validate_preconditions(&Spy::default(), "not json").expect("an error");
        assert!(err.contains("AppArgs"), "{err}");
    }

    #[test]
    fn the_outcome_set_parses_the_hosts_payload() {
        let input: FinalizeInput = serde_json::from_str(
            r#"{"program":"lending","program_crate":{"dir":"p","package":"l","lib":"l"},
                "idl":null,"setup":"struct Fixture {}","components":[
                  {"name":"Farms","outcome":{"status":"delivered","artifact_text":"fn c_farms(){}",
                   "targets":["c_farms"],"property_units":[["fifo",["c_fifo"]]]}},
                  {"name":"Referrals","outcome":{"status":"gave_up"}}]}"#,
        )
        .expect("parse");
        assert_eq!(input.idl, None);
        assert_eq!(input.setup.as_deref(), Some("struct Fixture {}"));
        // A component that gave up carries nothing to read, and `delivered` skips it.
        let delivered: Vec<&str> = input.delivered().map(|(name, _)| name).collect();
        assert_eq!(delivered, vec!["Farms"]);
        assert!(matches!(input.components[1].outcome, ComponentOutcome::GaveUp));
    }
}
