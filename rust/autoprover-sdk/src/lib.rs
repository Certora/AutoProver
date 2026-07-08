//! # autoprover-sdk
//!
//! The library a Rust-based AutoProver application imports. It defines the seam
//! between a Rust backend and the generic Python pipeline
//! (`composer/pipeline/core.py`), realized over a **synchronous, JSON** FFI
//! boundary — the inversion-of-control ("Tier 2") design from
//! `docs/rust-formalization-backends.md` and `docs/rust-applications.md`.
//!
//! Python owns the asyncio event loop and every effect (LLM calls, the prover,
//! the feedback judge, the cache, event streaming). Rust is a *pure decider*:
//! [`FormalizeSession::resume`] takes an [`Observation`] (the result of the last
//! effect) and returns the next [`Command`] (the effect to perform, or a
//! terminal `Publish` / `GiveUp`). There is no `async` and no `pyo3-async`
//! bridge on the Rust side.
//!
//! An application implements [`Application`] (+ one or more [`FormalizeSession`]s)
//! and calls [`export_app!`] to emit the PyO3 module the Python host loads.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Re-exported so [`export_app!`] can reference `$crate::pyo3::…`; an app crate
/// still depends on pyo3 directly to enable `extension-module` / `abi3-py312`.
pub use pyo3;

// ===========================================================================
// Descriptor — the declarative spine the Python host consumes to synthesize the
// phase enum, argparse, frontend and artifact store (see rust-applications.md).
// ===========================================================================

/// Which of the four driver-tagged core phases a declared phase fills. A phase
/// with no core slot is a UI-only phase (cf. autoprove's harness/autosetup).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CoreSlot {
    Analysis,
    Extraction,
    Formalization,
    Report,
}

/// One task-grouping phase. `key` becomes the synthesized `enum.Enum` member
/// name; `label`/`order` drive UI grouping.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PhaseSpec {
    pub key: String,
    pub label: String,
    pub order: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub core_slot: Option<CoreSlot>,
}

/// Default value for a declared CLI argument.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ArgDefault {
    Str { value: Option<String> },
    Int { value: Option<i64> },
    Bool { value: bool },
}

/// A CLI flag the generic entry point adds beyond the three positional inputs
/// (`project_root`, `main_contract`, `system_doc`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArgSpec {
    pub flag: String,
    pub help: String,
    pub default: ArgDefault,
    #[serde(default)]
    pub required: bool,
}

/// A domain event kind the frontend should render (see `Command::Emit`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventKind {
    pub kind: String,
    pub label: String,
}

/// On-disk deliverable layout. All paths are project-root-relative.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactLayout {
    pub deliverable_dir: String,
    pub internal_dir: String,
    pub report_dir: String,
    /// Where the verification artifacts themselves are written.
    pub artifact_dir: String,
    /// Filename prefix for a per-component artifact (e.g. `autospec` → `autospec_<slug>.spec`).
    pub artifact_prefix: String,
    /// Artifact file extension, no dot (e.g. `spec`, `t.sol`).
    pub artifact_extension: String,
    /// The store's term for the property→units map file suffix (`property_rules`, `property_tests`).
    pub property_suffix: String,
}

/// The complete declaration the Python host reads once at load time.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppDescriptor {
    pub name: String,
    pub header_text: String,
    /// The ecosystem (chain) tag: "evm" | "solana" | "soroban". Selects the shared front
    /// half's system model + prompts; the Python host resolves it against its ecosystem
    /// registry. Defaults to "evm" so a descriptor built before this field existed still loads.
    #[serde(default = "default_ecosystem")]
    pub ecosystem: String,
    /// The report's backend tag (`AutoProverReport.backend`).
    pub backend_tag: String,
    /// Prose injected into the property-extraction prompt (verification-surface guidance).
    pub backend_guidance: String,
    /// The system-analysis cache key (`SystemAnalysisSpec.analysis_key`).
    pub analysis_key: String,
    pub phases: Vec<PhaseSpec>,
    #[serde(default)]
    pub args: Vec<ArgSpec>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rag_db_default: Option<String>,
    #[serde(default)]
    pub event_kinds: Vec<EventKind>,
    pub artifact_layout: ArtifactLayout,
}

fn default_ecosystem() -> String {
    "evm".to_string()
}

// ===========================================================================
// Formalize I/O — the data crossing at `formalize` time.
// ===========================================================================

/// One property to formalize (mirrors `composer.spec.types.PropertyFormulation`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Property {
    pub title: String,
    /// One of "attack_vector" | "safety_property" | "invariant".
    pub sort: String,
    pub description: String,
}

/// The input handed to [`Application::new_session`]. `component` and `config`
/// are opaque JSON (a component's `model_dump()` and the backend's config
/// blob); apps deserialize the parts they need.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FormalizeInput {
    pub label: String,
    pub component: serde_json::Value,
    pub props: Vec<Property>,
    #[serde(default)]
    pub config: serde_json::Value,
}

/// The input handed to [`Application::new_setup_session`] — the *program-wide*
/// setup authored once, before per-component formalization (e.g. a Crucible
/// fixture + actions). `analyzed` is the ecosystem's system model as JSON (e.g. a
/// `SolanaApplication`); `program` is the target program's identifier.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SetupInput {
    pub program: String,
    pub analyzed: serde_json::Value,
    #[serde(default)]
    pub config: serde_json::Value,
}

/// A property the author declined to formalize (mirrors `SkippedProperty`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Skipped {
    pub property_title: String,
    pub reason: String,
}

/// A successful formalization — the payload of [`Command::Publish`]. The Python
/// side validates this into `RustFormalResult`, which satisfies both
/// `FormalResult` and `ReportableResult` and is what the cache/report/store key
/// off.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Formalized {
    pub commentary: String,
    /// The bytes written to the artifact file (`FormalResult.artifact_text`).
    pub artifact_text: String,
    /// property title → the unit names (rules / tests) that demonstrate it.
    #[serde(default)]
    pub property_units: Vec<(String, Vec<String>)>,
    #[serde(default)]
    pub skipped: Vec<Skipped>,
    /// The verification-run link, or `None` for backends with no run service.
    #[serde(default)]
    pub output_link: Option<String>,
    /// Per-unit verdicts baked in at formalize time, for a **self-contained**
    /// backend whose pass/fail is known when the artifact is produced (e.g. a
    /// fuzzer: crash = BAD, clean run to budget = GOOD). Keyed by unit name (the
    /// test/rule). When non-empty the host uses these directly and does not call
    /// `fetch_verdicts`; a run-service-backed backend leaves this empty and answers
    /// through `fetch_verdicts` instead.
    #[serde(default)]
    pub verdicts: BTreeMap<String, Verdict>,
}

impl Formalized {
    /// Convenience: a result with just artifact text + commentary.
    pub fn new(artifact_text: impl Into<String>, commentary: impl Into<String>) -> Self {
        Formalized {
            commentary: commentary.into(),
            artifact_text: artifact_text.into(),
            ..Default::default()
        }
    }
}

// ===========================================================================
// The inversion-of-control protocol: Observation (Py → Rust) / Command (Rust → Py)
// ===========================================================================

/// What Python hands back to [`FormalizeSession::resume`] after performing the
/// effect the previous [`Command`] requested.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Observation {
    /// The initial tick that starts a session.
    Start,
    /// An LLM turn's reply text.
    LlmReply { text: String },
    /// The prover/verifier result (backend-shaped JSON).
    ProverResult { data: serde_json::Value },
    /// The feedback judge's result (backend-shaped JSON).
    FeedbackResult { data: serde_json::Value },
    /// The value read from the cache (`None` on miss).
    Cached { value: Option<serde_json::Value> },
    /// The result of a `RunCommand` effect: the process's exit code and captured
    /// streams. Python has already materialized the requested files, run the
    /// command (no shell), and captured its output.
    CommandResult {
        exit_code: i32,
        stdout: String,
        stderr: String,
    },
    /// Acknowledgement of a fire-and-forget effect (`CachePut`, `Emit`).
    Ack,
}

/// What Rust asks Python to do next. Terminal variants (`Publish`/`GiveUp`) end
/// the loop; all others yield an [`Observation`] on the next `resume`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Command {
    /// Perform an LLM turn with the given messages/prompt payload.
    CallLlm { messages: serde_json::Value },
    /// Run the verifier over `spec` (optionally restricted to `rules`).
    RunProver {
        spec: String,
        #[serde(default)]
        config: serde_json::Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        rules: Option<Vec<String>>,
    },
    /// General effect: materialize `files` (workdir-relative path → contents) into
    /// this session's workdir, then run `program` with `args` there — as a child
    /// process, never through a shell. Yields [`Observation::CommandResult`].
    ///
    /// The **command line is authored by this decider**, not the LLM: `program`
    /// and `args` come from the backend's own compiled logic; only file *contents*
    /// may derive from LLM output (see `docs/crucible-application.md` §7.2). Python
    /// enforces exec-not-shell + workdir path-confinement, and (once built) runs it
    /// inside the sandbox (§7.4). Any backend that gates artifacts with a local CLI
    /// (Crucible, `cargo build-sbf`, `anchor idl`, …) uses this rather than the
    /// prover-specific `RunProver`.
    RunCommand {
        program: String,
        #[serde(default)]
        args: Vec<String>,
        #[serde(default)]
        files: BTreeMap<String, String>,
    },
    /// Run the feedback judge.
    RunFeedback {
        spec: String,
        #[serde(default)]
        skipped: serde_json::Value,
        #[serde(default)]
        rebuttals: serde_json::Value,
    },
    /// Read a cache key (yields `Observation::Cached`).
    CacheGet { key: String },
    /// Write a cache key (yields `Observation::Ack`).
    CachePut { key: String, value: serde_json::Value },
    /// Stream a domain event to this task's panel (yields `Observation::Ack`).
    Emit {
        event_kind: String,
        payload: serde_json::Value,
    },
    /// Terminal: formalization succeeded.
    Publish { result: Formalized },
    /// Terminal: the author declined this component.
    GiveUp { reason: String },
}

// ===========================================================================
// Verdicts — the report's per-unit outcomes.
// ===========================================================================

/// A per-unit outcome (mirrors `composer…report.collect.Verdict`). `outcome` is
/// one of GOOD | BAD | ERROR | TIMEOUT | UNKNOWN.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Verdict {
    pub outcome: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duration_seconds: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit_file: Option<String>,
}

impl Verdict {
    pub fn good() -> Self {
        Verdict { outcome: "GOOD".into(), line: None, duration_seconds: None, unit_file: None }
    }
    pub fn with_outcome(outcome: impl Into<String>) -> Self {
        Verdict { outcome: outcome.into(), line: None, duration_seconds: None, unit_file: None }
    }
}

/// The input to [`Application::fetch_verdicts`] — the report's view of one
/// formalized component.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerdictInput {
    pub name: String,
    pub unit_file: String,
    #[serde(default)]
    pub run_link: Option<String>,
    #[serde(default)]
    pub property_units: Vec<(String, Vec<String>)>,
}

// ===========================================================================
// The traits an application implements.
// ===========================================================================

/// A pure decider for one component's formalization. Holds whatever loop state
/// the app needs (draft spec, validation digests, turn budget, …) and advances
/// one effect per `resume`. Never blocks, never awaits.
///
/// `Send + Sync` because PyO3 wraps the session in a `#[pyclass]`, which must be
/// `Sync` — a state machine over plain owned data satisfies this without effort;
/// just avoid non-`Sync` interior mutability.
pub trait FormalizeSession: Send + Sync {
    /// Given the result of the previous effect, decide the next [`Command`].
    fn resume(&mut self, observation: Observation) -> Command;
}

/// A Rust AutoProver application/backend. One instance per wheel; construct it
/// in [`export_app!`].
pub trait Application: Send + Sync + 'static {
    /// The declaration the Python host reads at load time.
    fn descriptor(&self) -> AppDescriptor;

    /// Validate application-specific preconditions before any service opens
    /// (cf. foundry's `foundry.toml` check). `Err(msg)` aborts the run.
    fn validate_preconditions(&self, _args: &serde_json::Value) -> Result<(), String> {
        Ok(())
    }

    /// Author program-wide shared setup once, before per-component formalization
    /// (e.g. a Crucible fixture + `action_*`), as an IoC decider driven through the
    /// same effect loop. `None` (the default) means the backend has no shared setup
    /// phase. The published `Formalized.artifact_text` is the setup source, which
    /// the host hands to the artifact store (e.g. as the harness fixture).
    fn new_setup_session(&self, _input: SetupInput) -> Option<Box<dyn FormalizeSession>> {
        None
    }

    /// Begin formalizing one component's property batch.
    fn new_session(&self, input: FormalizeInput) -> Box<dyn FormalizeSession>;

    /// Per-unit verdicts for the report. A self-contained backend computes these
    /// directly; one backed by a run service surfaces them through Python
    /// effects instead and can return `{}` here.
    fn fetch_verdicts(&self, input: VerdictInput) -> BTreeMap<String, Verdict>;

    /// Optional run-level artifacts from the full outcome set, as
    /// `{project_relative_path: file_contents}`. Default: none.
    fn finalize(&self, _outcomes: &serde_json::Value) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}

// ===========================================================================
// FFI helpers — the sync, JSON-string boundary. `export_app!` wraps these in
// #[pyfunction]s; they are also directly unit-testable without Python.
// ===========================================================================

fn err_json(context: &str, e: impl std::fmt::Display) -> String {
    serde_json::json!({ "kind": "give_up", "reason": format!("{context}: {e}") }).to_string()
}

/// `descriptor() -> str` (JSON).
pub fn ffi_descriptor(app: &dyn Application) -> String {
    // The descriptor is app-authored and small; a serialization failure is a bug.
    serde_json::to_string(&app.descriptor())
        .unwrap_or_else(|e| format!("{{\"error\":\"descriptor serialize: {e}\"}}"))
}

/// `validate_preconditions(args_json) -> str | None` (None = ok).
pub fn ffi_validate(app: &dyn Application, args_json: &str) -> Option<String> {
    let args: serde_json::Value = match serde_json::from_str(args_json) {
        Ok(v) => v,
        Err(e) => return Some(format!("invalid args JSON: {e}")),
    };
    app.validate_preconditions(&args).err()
}

/// A session that immediately gives up — used when `FormalizeInput` fails to parse.
struct FailSession(String);
impl FormalizeSession for FailSession {
    fn resume(&mut self, _observation: Observation) -> Command {
        Command::GiveUp { reason: self.0.clone() }
    }
}

/// `new_session(input_json) -> Session`.
pub fn ffi_new_session(app: &dyn Application, input_json: &str) -> Box<dyn FormalizeSession> {
    match serde_json::from_str::<FormalizeInput>(input_json) {
        Ok(input) => app.new_session(input),
        Err(e) => Box::new(FailSession(format!("invalid FormalizeInput JSON: {e}"))),
    }
}

/// `new_setup_session(input_json) -> Session | None`. `None` if the app declares no
/// setup phase; a give-up session if the input fails to parse (so the host sees a
/// clean `GiveUp` rather than a silent skip).
pub fn ffi_new_setup_session(
    app: &dyn Application,
    input_json: &str,
) -> Option<Box<dyn FormalizeSession>> {
    match serde_json::from_str::<SetupInput>(input_json) {
        Ok(input) => app.new_setup_session(input),
        Err(e) => Some(Box::new(FailSession(format!("invalid SetupInput JSON: {e}")))),
    }
}

/// `session.resume(observation_json) -> command_json`.
pub fn ffi_resume(session: &mut dyn FormalizeSession, observation_json: &str) -> String {
    let obs: Observation = match serde_json::from_str(observation_json) {
        Ok(o) => o,
        Err(e) => return err_json("invalid Observation JSON", e),
    };
    let cmd = session.resume(obs);
    serde_json::to_string(&cmd).unwrap_or_else(|e| err_json("command serialize", e))
}

/// `fetch_verdicts(input_json) -> str` (JSON `{unit_name: Verdict}`).
pub fn ffi_fetch_verdicts(app: &dyn Application, input_json: &str) -> String {
    let input: VerdictInput = match serde_json::from_str(input_json) {
        Ok(v) => v,
        Err(_) => return "{}".to_string(),
    };
    let verdicts = app.fetch_verdicts(input);
    serde_json::to_string(&verdicts).unwrap_or_else(|_| "{}".to_string())
}

/// `finalize(outcomes_json) -> str | None` (JSON `{relpath: contents}`, or None).
pub fn ffi_finalize(app: &dyn Application, outcomes_json: &str) -> Option<String> {
    let outcomes: serde_json::Value = serde_json::from_str(outcomes_json).ok()?;
    let files = app.finalize(&outcomes);
    if files.is_empty() {
        None
    } else {
        serde_json::to_string(&files).ok()
    }
}

// ===========================================================================
// The export macro.
// ===========================================================================

/// Emit the PyO3 module the Python host loads. Invoke it once in an application
/// crate (a `cdylib` depending on `autoprover-sdk` and `pyo3`):
///
/// ```ignore
/// autoprover_sdk::export_app!(my_app, MyApp::new());
/// ```
///
/// `module_ident` MUST match the wheel's module name (`[tool.maturin] module-name`
/// / the `lib.name`). The expansion defines the `RustSession` class plus the
/// `descriptor` / `validate_preconditions` / `new_session` / `fetch_verdicts` /
/// `finalize` functions the host expects, all delegating to the `ffi_*` helpers.
#[macro_export]
macro_rules! export_app {
    ($module:ident, $ctor:expr) => {
        fn __autoprover_app() -> &'static dyn $crate::Application {
            static APP: ::std::sync::OnceLock<::std::boxed::Box<dyn $crate::Application>> =
                ::std::sync::OnceLock::new();
            &**APP.get_or_init(|| ::std::boxed::Box::new($ctor))
        }

        /// A live formalization decider held across `resume` calls.
        #[$crate::pyo3::pyclass]
        struct RustSession {
            inner: ::std::boxed::Box<dyn $crate::FormalizeSession>,
        }

        #[$crate::pyo3::pymethods]
        impl RustSession {
            /// Feed the last effect's Observation (JSON); get the next Command (JSON).
            fn resume(&mut self, observation: ::std::string::String) -> ::std::string::String {
                $crate::ffi_resume(self.inner.as_mut(), &observation)
            }
        }

        #[$crate::pyo3::pyfunction]
        fn descriptor() -> ::std::string::String {
            $crate::ffi_descriptor(__autoprover_app())
        }

        #[$crate::pyo3::pyfunction]
        fn validate_preconditions(
            args_json: ::std::string::String,
        ) -> ::std::option::Option<::std::string::String> {
            $crate::ffi_validate(__autoprover_app(), &args_json)
        }

        #[$crate::pyo3::pyfunction]
        fn new_session(input_json: ::std::string::String) -> RustSession {
            RustSession {
                inner: $crate::ffi_new_session(__autoprover_app(), &input_json),
            }
        }

        #[$crate::pyo3::pyfunction]
        fn new_setup_session(
            input_json: ::std::string::String,
        ) -> ::std::option::Option<RustSession> {
            $crate::ffi_new_setup_session(__autoprover_app(), &input_json)
                .map(|inner| RustSession { inner })
        }

        #[$crate::pyo3::pyfunction]
        fn fetch_verdicts(input_json: ::std::string::String) -> ::std::string::String {
            $crate::ffi_fetch_verdicts(__autoprover_app(), &input_json)
        }

        #[$crate::pyo3::pyfunction]
        fn finalize(
            outcomes_json: ::std::string::String,
        ) -> ::std::option::Option<::std::string::String> {
            $crate::ffi_finalize(__autoprover_app(), &outcomes_json)
        }

        #[$crate::pyo3::pymodule]
        fn $module(
            m: &$crate::pyo3::Bound<'_, $crate::pyo3::types::PyModule>,
        ) -> $crate::pyo3::PyResult<()> {
            use $crate::pyo3::types::PyModuleMethods as _;
            m.add_function($crate::pyo3::wrap_pyfunction!(descriptor, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(validate_preconditions, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(new_session, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(new_setup_session, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(fetch_verdicts, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(finalize, m)?)?;
            m.add_class::<RustSession>()?;
            ::std::result::Result::Ok(())
        }
    };
}
