//! The sync, JSON-string boundary. [`export_app!`](crate::export_app) wraps these in
//! `#[pyfunction]`s (compile/validate release the GIL); also unit-testable without Python.

use crate::args::AppArgs;
use crate::authoring::{AuthorInput, Prompt};
use crate::backend::Backend;
use crate::finalize::FinalizeInput;
use crate::outcome::{CompileResult, Outcome, Target};
use crate::sandbox::Workspace;

fn parse<T: serde::de::DeserializeOwned>(json: &str, what: &str) -> Result<T, String> {
    serde_json::from_str(json).map_err(|e| format!("invalid {what} JSON: {e}"))
}

/// Parse an [`AuthorInput`].
///
/// Nothing is normalized on the way through: the project facts a payload carries are
/// [chain-shaped](crate::chain::ChainData), so filling in an unresolved one means applying *one
/// ecosystem's* layout convention — which is the wheel's business, or its chain support crate's, not
/// this boundary's.
fn parse_input(json: &str) -> Result<AuthorInput, String> {
    parse(json, "AuthorInput")
}

/// The workspace a blocking callout runs in, from the two strings the host sends it as.
fn workspace(workdir: &str, sandbox_json: &str) -> Workspace {
    Workspace {
        dir: std::path::PathBuf::from(workdir),
        sandbox: parse(sandbox_json, "Sandbox").unwrap_or_default(),
    }
}

fn parse_args(json: &str) -> Result<AppArgs, String> {
    parse(json, "AppArgs")
}

/// `descriptor() -> str` (JSON).
pub fn descriptor(b: &dyn Backend) -> String {
    serde_json::to_string(&b.descriptor())
        .unwrap_or_else(|e| format!("{{\"error\":\"descriptor serialize: {e}\"}}"))
}

/// `validate_preconditions(args_json) -> str | None` (None = ok). A payload this can't parse is a
/// host bug, and it is reported as a failed precondition — the one callout with a channel to say so.
pub fn validate_preconditions(b: &dyn Backend, args_json: &str) -> Option<String> {
    match parse_args(args_json) {
        Ok(args) => b.validate_preconditions(&args).err(),
        Err(e) => Some(e),
    }
}

/// `checks(input_json) -> str` (JSON `[Check]`).
pub fn checks(b: &dyn Backend, input_json: &str) -> String {
    match parse_input(input_json) {
        Ok(input) => serde_json::to_string(&b.checks(&input)).unwrap_or_else(|_| "[]".into()),
        Err(_) => "[]".into(),
    }
}

/// `author_prompt(input_json) -> str` (JSON `Prompt`).
pub fn author_prompt(b: &dyn Backend, input_json: &str) -> String {
    let input = match parse_input(input_json) {
        Ok(v) => v,
        Err(e) => {
            return serde_json::to_string(&Prompt { system: None, instruction: format!("ERROR: {e}") })
                .unwrap_or_default()
        }
    };
    serde_json::to_string(&b.author_prompt(&input)).unwrap_or_default()
}

/// `check_syntax(input_json, spec) -> str | None` (None = the spec may be written). A payload this
/// can't parse rejects the write, naming the parse error: the alternative is accepting a spec no
/// backend ever saw.
pub fn check_syntax(b: &dyn Backend, input_json: &str, spec: &str) -> Option<String> {
    match parse_input(input_json) {
        Ok(input) => b.check_syntax(&input, spec),
        Err(e) => Some(e),
    }
}

/// `judge(input_json) -> str | None` (JSON `Judge`; None = this wheel does not review this input).
/// Takes no spec: it is asked once, before anything is authored.
pub fn judge(b: &dyn Backend, input_json: &str) -> Option<String> {
    let input = parse_input(input_json).ok()?;
    b.judge(&input)
        .map(|j| serde_json::to_string(&j).unwrap_or_default())
}

/// `judge_instruction(input_json, spec) -> str` — the instruction itself, not JSON. Asked per review
/// round, only for an input `judge` claimed. An unparseable payload reaches the reviewer as its
/// instruction, as in `author_prompt`: a host bug is better read than reviewed around.
pub fn judge_instruction(b: &dyn Backend, input_json: &str, spec: &str) -> String {
    match parse_input(input_json) {
        Ok(input) => b.judge_instruction(&input, spec),
        Err(e) => format!("ERROR: {e}"),
    }
}

/// `compile(input_json, spec | None, workdir, sandbox_json) -> str` (JSON `CompileResult`).
/// BLOCKING. `None` is the preflight: nothing has been authored, so there is no spec at all.
pub fn compile(
    b: &dyn Backend,
    input_json: &str,
    spec: Option<&str>,
    workdir: &str,
    sandbox_json: &str,
) -> String {
    let input = match parse_input(input_json) {
        Ok(v) => v,
        Err(e) => return serde_json::to_string(&CompileResult::Failed { errors: e }).unwrap_or_default(),
    };
    let r = b.compile(&input, spec, &workspace(workdir, sandbox_json));
    serde_json::to_string(&r).unwrap_or_else(|e| {
        serde_json::to_string(&CompileResult::Failed { errors: e.to_string() }).unwrap_or_default()
    })
}

/// `validate(input_json, spec, target_json, workdir, sandbox_json) -> str` (JSON
/// `ValidateOutcome`). BLOCKING.
///
/// An unparseable target is the one payload with nowhere to report to — the check names a verdict
/// would be keyed by are exactly what failed to parse — so it yields no verdicts. The host sent that
/// target and knows what it covers, so an answer covering nothing is refused there as the protocol
/// failure it is, rather than read as a run with nothing to object to.
pub fn validate(
    b: &dyn Backend,
    input_json: &str,
    spec: &str,
    target_json: &str,
    workdir: &str,
    sandbox_json: &str,
) -> String {
    let target: Target = parse(target_json, "Target").unwrap_or_default();
    let outcome = match parse_input(input_json) {
        Ok(input) => b.validate(&input, spec, &target, &workspace(workdir, sandbox_json)),
        Err(e) => target.all(Outcome::Error, Some(e)),
    };
    serde_json::to_string(&outcome).unwrap_or_default()
}

/// `sandbox_grants(args_json) -> str` (JSON `SandboxGrants`).
pub fn sandbox_grants(b: &dyn Backend, args_json: &str) -> String {
    let args = parse_args(args_json).unwrap_or_default();
    serde_json::to_string(&b.sandbox_grants(&args)).unwrap_or_else(|_| "{}".into())
}

/// `workspace_prep(input_json) -> str` (JSON `WorkspacePrep`). Pure.
pub fn workspace_prep(b: &dyn Backend, input_json: &str) -> String {
    match parse_input(input_json) {
        Ok(input) => serde_json::to_string(&b.workspace_prep(&input)).unwrap_or_else(|_| "{}".into()),
        Err(_) => "{}".into(),
    }
}

/// `finalize(outcomes_json) -> str | None` (JSON `{relpath: contents}`, or None).
pub fn finalize(b: &dyn Backend, outcomes_json: &str) -> Option<String> {
    let outcomes: FinalizeInput = parse(outcomes_json, "FinalizeInput").ok()?;
    let files = b.finalize(&outcomes);
    if files.is_empty() {
        None
    } else {
        serde_json::to_string(&files).ok()
    }
}

#[cfg(test)]
mod tests {
    //! The boundary's own guarantees: what a backend receives is what the host sent, and a payload's
    //! `kind` decides what there is to read.
    use super::*;
    use crate::chain::ChainData;
    use crate::descriptor::AppDescriptor;
    use crate::finalize::ComponentOutcome;
    use crate::outcome::{Check, ValidateOutcome};
    use crate::prep::WorkspacePrep;
    use serde::{Deserialize, Serialize};
    use std::sync::Mutex;

    /// Stands in for the type a chain's support crate defines. The SDK cannot name a real one — that
    /// it doesn't have to is the property these tests cover — so the Cargo shape is spelled here,
    /// where it is just another wheel's idea of what a project is.
    #[derive(Debug, Default, PartialEq, Serialize, Deserialize)]
    struct CargoUnit {
        dir: String,
        package: String,
        lib: String,
    }

    /// One `AuthorInput` with every field the wire requires, so a test can vary the one it is about.
    /// Absence is an error on this seam (see `crate::required`), which is exactly why a fixture that
    /// spells only what it cares about would fail to parse.
    fn component_json(source_unit: &str, prep_facts: &str) -> String {
        format!(
            r#"{{"kind":"component","program":"vault","unit":{{"slug":"farms"}},
                "source_unit":{source_unit},"prep_facts":{prep_facts},
                "props":[],"setup":null,"args":{{}}}}"#
        )
    }

    /// Records the project facts each callout was handed, so a test can assert on what crossed the
    /// seam rather than on what a backend chose to do with them.
    #[derive(Default)]
    struct Spy {
        seen: Mutex<Vec<ChainData>>,
    }

    impl Backend for Spy {
        fn descriptor(&self) -> AppDescriptor {
            unimplemented!("not exercised")
        }
        fn checks(&self, input: &AuthorInput) -> Vec<Check> {
            self.seen.lock().unwrap().push(input.source_unit.clone());
            Vec::new()
        }
        fn author_prompt(&self, _input: &AuthorInput) -> Prompt {
            unimplemented!("not exercised")
        }
        fn compile(
            &self,
            _input: &AuthorInput,
            _spec: Option<&str>,
            _ws: &Workspace,
        ) -> CompileResult {
            unimplemented!("not exercised")
        }
        fn validate(
            &self,
            _input: &AuthorInput,
            _spec: &str,
            _target: &Target,
            _ws: &Workspace,
        ) -> ValidateOutcome {
            unimplemented!("not exercised")
        }
        fn workspace_prep(&self, input: &AuthorInput) -> WorkspacePrep {
            self.seen.lock().unwrap().push(input.source_unit.clone());
            WorkspacePrep::default()
        }
        fn validate_preconditions(&self, args: &AppArgs) -> Result<(), String> {
            self.seen.lock().unwrap().push(args.source_unit.clone());
            Ok(())
        }
    }

    #[test]
    fn the_projects_own_shape_crosses_the_boundary_untouched() {
        // The lend shape: directory, package and lib all differ from the analysis identifier and
        // from each other. Every callout carrying project facts gets them verbatim — this boundary
        // knows no layout convention to "helpfully" apply, which is what lets a wheel for a chain
        // with no crates use the same seam.
        let spy = Spy::default();
        let unit = r#"{"dir":"programs/lend","package":"example-lending","lib":"example_lending"}"#;
        checks(&spy, &component_json(unit, "{}"));
        workspace_prep(&spy, &component_json(unit, "{}"));
        validate_preconditions(
            &spy,
            &format!(
                r#"{{"project_root":"/p","program":"vault","source_path":"src/lib.rs",
                     "system_doc":null,"source_unit":{unit},"declared":{{}}}}"#
            ),
        );
        let seen = spy.seen.lock().unwrap();
        assert_eq!(seen.len(), 3, "every callout that carries project facts was exercised");
        for data in seen.iter() {
            assert_eq!(
                data.parse::<CargoUnit>().expect("the chain's own shape"),
                CargoUnit {
                    dir: "programs/lend".into(),
                    package: "example-lending".into(),
                    lib: "example_lending".into(),
                }
            );
        }
    }

    #[test]
    fn an_unresolved_project_reaches_the_backend_empty() {
        // The host resolved nothing (no toolchain registered for the chain, no such unit in the
        // language, an unreadable layout). The wheel is told exactly that, and applies its own
        // convention if it has one — the alternative, filling it in here, would mean this seam
        // choosing one ecosystem's layout for every wheel.
        let spy = Spy::default();
        checks(&spy, &component_json("{}", "{}"));
        let seen = spy.seen.lock().unwrap();
        let data = seen.first().expect("checks was called");
        assert!(data.is_empty());
        assert!(data.parse::<CargoUnit>().is_err(), "empty is not a resolved unit");
    }

    #[test]
    fn an_input_carries_only_the_payload_its_kind_has() {
        // The host's wire shape is flat: `kind` selects the variant, and the field beside it
        // belongs to that variant alone. A component turn has no analyzed model to read, and a
        // preflight has neither — it runs before anything is analyzed.
        let comp: AuthorInput = serde_json::from_str(
            r#"{"kind":"component","program":"vault","unit":{"slug":"farms"},
                "source_unit":{},"prep_facts":{"idl":"fuzz/vault/idls/vault.json"},
                "props":[],"setup":"struct Fixture {}","args":{"fuzz_timeout":900}}"#,
        )
        .expect("parse");
        assert_eq!(comp.unit().and_then(|u| u.get("slug")).and_then(|v| v.as_str()), Some("farms"));
        assert!(comp.model().is_none());
        assert_eq!(comp.setup.as_deref(), Some("struct Fixture {}"));
        assert_eq!(comp.args.get::<u64>("fuzz_timeout"), Some(900));
        // An absent flag and one left at a null default are the same answer.
        assert_eq!(comp.args.get::<u64>("nope"), None);

        let setup: AuthorInput = serde_json::from_str(
            r#"{"kind":"setup","program":"vault","model":{"components":[]},
                "units":[{"slug":"farms"},{"slug":"vaults"}],
                "source_unit":{},"prep_facts":{},"props":[],"setup":null,"args":{}}"#,
        )
        .expect("parse");
        assert!(setup.model().is_some() && setup.unit().is_none());
        // The one turn holding the whole unit set: it is the run's, not this spec's, which is why
        // it arrives here and not through `unit()`.
        assert_eq!(setup.units().len(), 2);
        assert!(comp.units().is_empty(), "a component turn holds its own unit, not the set");
        assert!(setup.prep_facts.is_empty(), "a prep that established nothing says so");

        let pre: AuthorInput = serde_json::from_str(
            r#"{"kind":"preflight","program":"vault","source_unit":{},"prep_facts":{},
                "props":[],"setup":null,"args":{}}"#,
        )
        .expect("parse");
        assert!(pre.unit().is_none() && pre.model().is_none() && pre.props.is_empty());
        assert!(pre.units().is_empty(), "nothing is analyzed yet, so there is no unit set");

        // …and it round-trips flat, which is what the host parses back.
        let json = serde_json::to_value(&pre).expect("serialize");
        assert_eq!(json.get("kind").and_then(|v| v.as_str()), Some("preflight"));
    }

    #[test]
    fn a_malformed_args_payload_is_reported_as_a_failed_precondition() {
        // The only callout with a channel to say the host sent nonsense; the alternative is a
        // silent default that reads as "no preconditions to check".
        let err = validate_preconditions(&Spy::default(), "not json").expect("an error");
        assert!(err.contains("AppArgs"), "{err}");
    }

    #[test]
    fn the_outcome_set_parses_the_hosts_payload() {
        let input: FinalizeInput = serde_json::from_str(
            r#"{"program":"lending",
                "source_unit":{"dir":"p","package":"l","lib":"l"},"prep_facts":{},
                "setup":"struct Fixture {}","components":[
                  {"name":"Farms","outcome":{"status":"delivered","artifact_text":"fn c_farms(){}",
                   "targets":["c_farms"],"property_checks":[["fifo",["c_fifo"]]],
                   "skipped":[],"unit_file":null,"run_link":null}},
                  {"name":"Referrals","outcome":{"status":"gave_up"}}]}"#,
        )
        .expect("parse");
        // What ships is rendered from the same facts the gated builds used.
        assert_eq!(input.source_unit.parse::<CargoUnit>().expect("the chain's shape").package, "l");
        assert!(input.prep_facts.is_empty());
        assert_eq!(input.setup.as_deref(), Some("struct Fixture {}"));
        // A component that gave up carries nothing to read, and `delivered` skips it.
        let delivered: Vec<&str> = input.delivered().map(|(name, _)| name).collect();
        assert_eq!(delivered, vec!["Farms"]);
        assert!(matches!(input.components[1].outcome, ComponentOutcome::GaveUp));
    }
}
