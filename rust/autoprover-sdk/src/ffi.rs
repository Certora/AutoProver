//! The sync, JSON-string boundary. [`export_app!`](crate::export_app) wraps these in
//! `#[pyfunction]`s (compile/validate release the GIL); also unit-testable without Python.
//!
//! Every callout keeps a [`Result`] until this module writes the string the host reads. `Ok` is
//! the payload type unchanged. `Err` is always [`CalloutError`] — the one extra inbound JSON
//! shape — so a host bug cannot be read as an empty plan, a skipped review, or a failed build.

use crate::args::AppArgs;
use crate::authoring::AuthorInput;
use crate::backend::Backend;
use crate::prep::CrateRootInput;
use crate::sandbox::Workspace;
use serde::{Deserialize, Serialize};

/// Why a callout produced no payload. Success is the payload type unchanged; this is the only
/// extra JSON shape on the inbound side of the seam.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub enum CalloutError {
    Error { message: String },
}

/// JSON for a successful payload, or [`CalloutError`] when the callout could not produce one.
pub fn encode<T: Serialize>(result: Result<T, String>) -> String {
    match result {
        Ok(value) => match serde_json::to_string(&value) {
            Ok(json) => json,
            Err(e) => encode_err(e),
        },
        Err(e) => encode_err(e),
    }
}

fn encode_err(e: impl ToString) -> String {
    match serde_json::to_string(&CalloutError::Error {
        message: e.to_string(),
    }) {
        Ok(json) => json,
        Err(_) => "{\"kind\":\"error\",\"message\":\"unserializable error\"}".into(),
    }
}

/// `None` is a successful empty answer (no judge, no files). A failed callout is `Some` of the
/// error envelope, never `None`.
fn encode_opt<T: Serialize>(result: Result<Option<T>, String>) -> Option<String> {
    match result {
        Ok(None) => None,
        Ok(Some(value)) => Some(encode(Ok(value))),
        Err(e) => Some(encode_err(e)),
    }
}

/// Like [`encode_opt`], but a successful `Some` is returned as-is — a target name, a precondition
/// or syntax complaint — rather than JSON-encoded.
fn encode_opt_text(result: Result<Option<String>, String>) -> Option<String> {
    match result {
        Ok(value) => value,
        Err(e) => Some(encode_err(e)),
    }
}

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
fn workspace(workdir: &str, sandbox_json: &str) -> Result<Workspace, String> {
    Ok(Workspace {
        dir: std::path::PathBuf::from(workdir),
        sandbox: parse(sandbox_json, "Sandbox")?,
    })
}

fn parse_args(json: &str) -> Result<AppArgs, String> {
    parse(json, "AppArgs")
}

/// `descriptor() -> str` (JSON).
pub fn descriptor(b: &dyn Backend) -> String {
    encode(Ok(b.descriptor()))
}

/// `validate_preconditions(args_json) -> str | None` (None = ok). A payload this can't parse is a
/// [`CalloutError`], not a failed precondition: the host sent the args and a parse failure is a
/// protocol bug, not something the wheel asked to refuse.
pub fn validate_preconditions(b: &dyn Backend, args_json: &str) -> Option<String> {
    encode_opt_text(parse_args(args_json).map(|args| b.validate_preconditions(&args).err()))
}

/// `target_for(input_json, check) -> str | None` — the target the named check runs under, `None`
/// for its own. Pure. An unparseable input is a [`CalloutError`], not `None`: `None` means the
/// check is its own target.
pub fn target_for(b: &dyn Backend, input_json: &str, check: &str) -> Option<String> {
    encode_opt_text(parse_input(input_json).map(|input| b.target_for(&input, check)))
}

/// `author_prompt(input_json) -> str` (JSON `Prompt`).
pub fn author_prompt(b: &dyn Backend, input_json: &str) -> String {
    encode(parse_input(input_json).map(|input| b.author_prompt(&input)))
}

/// `check_syntax(input_json, spec) -> str | None` (None = the spec may be written). A payload this
/// can't parse is a [`CalloutError`]: the alternative is accepting a spec no backend ever saw.
pub fn check_syntax(b: &dyn Backend, input_json: &str, spec: &str) -> Option<String> {
    encode_opt_text(parse_input(input_json).map(|input| b.check_syntax(&input, spec)))
}

/// `judge(input_json) -> str | None` (JSON `Judge`; None = this wheel does not review this input).
/// Takes no spec: it is asked once, before anything is authored. An unparseable input is a
/// [`CalloutError`], not `None`: `None` means no judge.
pub fn judge(b: &dyn Backend, input_json: &str) -> Option<String> {
    encode_opt(parse_input(input_json).map(|input| b.judge(&input)))
}

/// `judge_instruction(input_json, spec) -> str` — the instruction itself, not JSON. Asked per review
/// round, only for an input `judge` claimed. An unparseable payload is a [`CalloutError`] rather
/// than an instruction the reviewer would be asked to follow.
pub fn judge_instruction(b: &dyn Backend, input_json: &str, spec: &str) -> String {
    match parse_input(input_json) {
        Ok(input) => b.judge_instruction(&input, spec),
        Err(e) => encode_err(e),
    }
}

/// `compile(input_json, spec | None, workdir, sandbox_json) -> str` (JSON `CompileResult`).
/// BLOCKING. `None` is the preflight: nothing has been authored, so there is no spec at all.
/// A payload this can't parse is a [`CalloutError`], not a failed build: the author cannot fix
/// a host bug by revising the spec.
pub fn compile(
    b: &dyn Backend,
    input_json: &str,
    spec: Option<&str>,
    workdir: &str,
    sandbox_json: &str,
) -> String {
    encode((|| {
        let input = parse_input(input_json)?;
        let ws = workspace(workdir, sandbox_json)?;
        Ok(b.compile(&input, spec, &ws))
    })())
}

/// `validate(input_json, spec, target_json, workdir, sandbox_json) -> str` (JSON
/// `ValidateOutcome`). BLOCKING.
///
/// An unparseable target (or input, or sandbox) is a [`CalloutError`]. The host already has the
/// target it sent, so a coverage miss would only hide the parse failure behind a different one.
pub fn validate(
    b: &dyn Backend,
    input_json: &str,
    spec: &str,
    target_json: &str,
    workdir: &str,
    sandbox_json: &str,
) -> String {
    encode((|| {
        let input = parse_input(input_json)?;
        let target = parse(target_json, "Target")?;
        let ws = workspace(workdir, sandbox_json)?;
        Ok(b.validate(&input, spec, &target, &ws))
    })())
}

/// `sandbox_grants(args_json) -> str` (JSON `SandboxGrants`).
pub fn sandbox_grants(b: &dyn Backend, args_json: &str) -> String {
    encode(parse_args(args_json).map(|args| b.sandbox_grants(&args)))
}

/// `workspace_prep(input_json) -> str` (JSON `WorkspacePrep`). Pure.
pub fn workspace_prep(b: &dyn Backend, input_json: &str) -> String {
    encode(parse_input(input_json).map(|input| b.workspace_prep(&input)))
}

/// `crate_root(input_json) -> str | None` (JSON `{relpath: contents}`, or None). Pure.
pub fn crate_root(b: &dyn Backend, input_json: &str) -> Option<String> {
    let input: CrateRootInput = parse(input_json, "CrateRootInput").ok()?;
    let files = b.crate_root(&input);
    if files.is_empty() {
        None
    } else {
        serde_json::to_string(&files).ok()
    }
}

/// `finalize(outcomes_json) -> str | None` (JSON `{relpath: contents}`, or None).
pub fn finalize(b: &dyn Backend, outcomes_json: &str) -> Option<String> {
    encode_opt(parse(outcomes_json, "FinalizeInput").map(|outcomes| {
        let files = b.finalize(&outcomes);
        (!files.is_empty()).then_some(files)
    }))
}

#[cfg(test)]
mod tests {
    //! The boundary's own guarantees: what a backend receives is what the host sent, and a payload's
    //! `kind` decides what there is to read.
    use super::*;
    use crate::authoring::Prompt;
    use crate::chain::ChainData;
    use crate::descriptor::AppDescriptor;
    use crate::finalize::{ComponentOutcome, FinalizeInput};
    use crate::outcome::{CompileResult, Target, ValidateOutcome};
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
        fn target_for(&self, input: &AuthorInput, _check: &str) -> Option<String> {
            self.seen.lock().unwrap().push(input.source_unit.clone());
            None
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
        target_for(&spy, &component_json(unit, "{}"), "r_x");
        workspace_prep(&spy, &component_json(unit, "{}"));
        validate_preconditions(
            &spy,
            &format!(
                r#"{{"project_root":"/p","program":"vault","source_path":"src/lib.rs",
                     "system_doc":null,"source_unit":{unit},"declared":{{}}}}"#
            ),
        );
        let seen = spy.seen.lock().unwrap();
        assert_eq!(
            seen.len(),
            3,
            "every callout that carries project facts was exercised"
        );
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
        target_for(&spy, &component_json("{}", "{}"), "r_x");
        let seen = spy.seen.lock().unwrap();
        let data = seen.first().expect("target_for was called");
        assert!(data.is_empty());
        assert!(
            data.parse::<CargoUnit>().is_err(),
            "empty is not a resolved unit"
        );
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
        assert_eq!(
            comp.unit()
                .and_then(|u| u.get("slug"))
                .and_then(|v| v.as_str()),
            Some("farms")
        );
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
        assert!(
            comp.units().is_empty(),
            "a component turn holds its own unit, not the set"
        );
        assert!(
            setup.prep_facts.is_empty(),
            "a prep that established nothing says so"
        );

        let pre: AuthorInput = serde_json::from_str(
            r#"{"kind":"preflight","program":"vault","source_unit":{},"prep_facts":{},
                "props":[],"setup":null,"args":{}}"#,
        )
        .expect("parse");
        assert!(pre.unit().is_none() && pre.model().is_none() && pre.props.is_empty());
        assert!(
            pre.units().is_empty(),
            "nothing is analyzed yet, so there is no unit set"
        );

        // …and it round-trips flat, which is what the host parses back.
        let json = serde_json::to_value(&pre).expect("serialize");
        assert_eq!(json.get("kind").and_then(|v| v.as_str()), Some("preflight"));
    }

    fn assert_error(raw: &str, needle: &str) {
        let CalloutError::Error { message } = serde_json::from_str(raw).expect("error envelope");
        assert!(message.contains(needle), "{message}");
    }

    #[test]
    fn a_malformed_payload_is_the_error_envelope() {
        // None of these may look like a successful empty answer: no judge, no files, the check is
        // its own target, a failed build the author should revise.
        let spy = Spy::default();
        let input = component_json("{}", "{}");
        assert_error(&author_prompt(&spy, "not json"), "AuthorInput");
        assert_error(
            &compile(&spy, "not json", None, "/tmp", "{}"),
            "AuthorInput",
        );
        assert_error(&compile(&spy, &input, None, "/tmp", "not json"), "Sandbox");
        let target = r#"{"name":"t","checks":[]}"#;
        assert_error(
            &validate(&spy, "not json", "", target, "/tmp", "{}"),
            "AuthorInput",
        );
        assert_error(
            &validate(&spy, &input, "", "not json", "/tmp", "{}"),
            "Target",
        );
        assert_error(
            &validate(&spy, &input, "", target, "/tmp", "not json"),
            "Sandbox",
        );
        assert_error(&workspace_prep(&spy, "not json"), "AuthorInput");
        assert_error(&sandbox_grants(&spy, "not json"), "AppArgs");
        assert_error(&judge_instruction(&spy, "not json", ""), "AuthorInput");
        assert_error(
            judge(&spy, "not json").as_deref().expect("Err is Some"),
            "AuthorInput",
        );
        assert_error(
            target_for(&spy, "not json", "c")
                .as_deref()
                .expect("Err is Some"),
            "AuthorInput",
        );
        assert_error(
            validate_preconditions(&spy, "not json")
                .as_deref()
                .expect("Err is Some"),
            "AppArgs",
        );
        assert_error(
            check_syntax(&spy, "not json", "")
                .as_deref()
                .expect("Err is Some"),
            "AuthorInput",
        );
        assert_error(
            finalize(&spy, "not json").as_deref().expect("Err is Some"),
            "FinalizeInput",
        );
        assert!(
            judge(&spy, &input).is_none(),
            "a valid input with no judge stays None"
        );
        assert!(
            target_for(&spy, &input, "c").is_none(),
            "Spy groups each check as its own"
        );
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
                  {"name":"Referrals","outcome":{"status":"gave_up",
                   "unit":{"slug":"referrals"},"reason":"no action mints referral fees"}}]}"#,
        )
        .expect("parse");
        // What ships is rendered from the same facts the gated builds used.
        assert_eq!(
            input
                .source_unit
                .parse::<CargoUnit>()
                .expect("the chain's shape")
                .package,
            "l"
        );
        assert!(input.prep_facts.is_empty());
        assert_eq!(input.setup.as_deref(), Some("struct Fixture {}"));
        // The two outcomes partition the set, and each iterator reads only its own variant.
        let delivered: Vec<&str> = input.delivered().map(|(name, _)| name).collect();
        assert_eq!(delivered, vec!["Farms"]);
        let gave_up: Vec<(&str, &str)> =
            input.gave_up().map(|(name, g)| (name, g.reason.as_str())).collect();
        assert_eq!(gave_up, vec![("Referrals", "no action mints referral fees")]);
        // Its unit travels with it: a deliverable declaring a target per unit needs to name this one,
        // and it has no `targets` of its own because it ran no build.
        assert!(matches!(input.components[1].outcome, ComponentOutcome::GaveUp(_)));
    }
}
