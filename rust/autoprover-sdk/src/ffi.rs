//! The sync, JSON-string boundary. [`export_app!`](crate::export_app) wraps these in
//! `#[pyfunction]`s (compile/validate release the GIL); also unit-testable without Python.

use crate::authoring::{AuthorInput, Failure, Prompt};
use crate::backend::Backend;
use crate::outcome::{CompileResult, Outcome, ValidateOutcome, Verdict};
use crate::sandbox::Sandbox;

fn parse<T: serde::de::DeserializeOwned>(json: &str, what: &str) -> Result<T, String> {
    serde_json::from_str(json).map_err(|e| format!("invalid {what} JSON: {e}"))
}

/// `descriptor() -> str` (JSON).
pub fn ffi_descriptor(b: &dyn Backend) -> String {
    serde_json::to_string(&b.descriptor())
        .unwrap_or_else(|e| format!("{{\"error\":\"descriptor serialize: {e}\"}}"))
}

/// `validate_preconditions(args_json) -> str | None` (None = ok).
pub fn ffi_validate_preconditions(b: &dyn Backend, args_json: &str) -> Option<String> {
    let args: serde_json::Value = serde_json::from_str(args_json).unwrap_or(serde_json::Value::Null);
    b.validate_preconditions(&args).err()
}

/// `units(input_json) -> str` (JSON `[Unit]`).
pub fn ffi_units(b: &dyn Backend, input_json: &str) -> String {
    match parse::<AuthorInput>(input_json, "AuthorInput") {
        Ok(input) => serde_json::to_string(&b.units(&input)).unwrap_or_else(|_| "[]".into()),
        Err(_) => "[]".into(),
    }
}

/// `author_prompt(input_json, failure_json | None) -> str` (JSON `Prompt`).
pub fn ffi_author_prompt(b: &dyn Backend, input_json: &str, failure_json: Option<&str>) -> String {
    let input: AuthorInput = match parse(input_json, "AuthorInput") {
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
    let input: AuthorInput = parse(input_json, "AuthorInput").ok()?;
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
    let input: AuthorInput = match parse(input_json, "AuthorInput") {
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
    let outcome = match parse::<AuthorInput>(input_json, "AuthorInput") {
        Ok(input) => b.validate(&input, spec, unit, std::path::Path::new(workdir), &sandbox),
        Err(e) => ValidateOutcome::Verdicts {
            verdicts: vec![(unit.to_string(), Verdict::detailed(Outcome::Error, e))],
        },
    };
    serde_json::to_string(&outcome).unwrap_or_default()
}

/// `sandbox_grants(args_json) -> str` (JSON `SandboxGrants`).
pub fn ffi_sandbox_grants(b: &dyn Backend, args_json: &str) -> String {
    let args: serde_json::Value = serde_json::from_str(args_json).unwrap_or(serde_json::Value::Null);
    serde_json::to_string(&b.sandbox_grants(&args)).unwrap_or_else(|_| "{}".into())
}

/// `workspace_prep(input_json) -> str` (JSON `WorkspacePrep`). Pure.
pub fn ffi_workspace_prep(b: &dyn Backend, input_json: &str) -> String {
    match parse::<AuthorInput>(input_json, "AuthorInput") {
        Ok(input) => serde_json::to_string(&b.workspace_prep(&input)).unwrap_or_else(|_| "{}".into()),
        Err(_) => "{}".into(),
    }
}

/// `finalize(outcomes_json) -> str | None` (JSON `{relpath: contents}`, or None).
pub fn ffi_finalize(b: &dyn Backend, outcomes_json: &str) -> Option<String> {
    let outcomes: serde_json::Value = serde_json::from_str(outcomes_json).ok()?;
    let files = b.finalize(&outcomes);
    if files.is_empty() {
        None
    } else {
        serde_json::to_string(&files).ok()
    }
}
