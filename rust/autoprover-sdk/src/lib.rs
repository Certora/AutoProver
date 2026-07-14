//! # autoprover-sdk
//!
//! The library a Rust-based AutoProver application imports. It defines the seam
//! between a Rust backend and the generic Python pipeline
//! (`composer/pipeline/core.py`), realized over a **synchronous, JSON** FFI
//! boundary — the service-shaped design in `docs/rust-backend-api.md`.
//!
//! The backend is a **passive service**, not a driver: the Python pipeline owns the
//! author→compile→judge→validate loop and every LLM turn, and calls the backend's
//! callouts. Most are pure ([`Backend::descriptor`], [`Backend::units`],
//! [`Backend::author_prompt`], [`Backend::judge_prompt`], [`Backend::finalize`]). The
//! two gating callouts ([`Backend::compile`], [`Backend::validate`]) run the toolchain
//! directly — each spawns the `run-confined` launcher via [`run_confined`] — and BLOCK;
//! the host calls them off the event loop (`asyncio.to_thread`) while the wheel releases
//! the GIL. There is no `async`/`pyo3-async` bridge and no `Command`/`Observation` resume
//! protocol on the Rust side.
//!
//! An application implements [`Backend`] and calls [`export_app!`] to emit the PyO3
//! module the Python host loads.

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
///
/// A `notice` kind is surfaced as a persistent, always-visible callout (plus a toast)
/// rather than a line in the collapsible per-task events log — for one-shot important
/// results such as a per-invariant verdict. Ordinary kinds stream into the log.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventKind {
    pub kind: String,
    pub label: String,
    #[serde(default)]
    pub notice: bool,
}

impl EventKind {
    /// A streaming event kind — rendered as a line in the collapsible events log.
    pub fn log(kind: impl Into<String>, label: impl Into<String>) -> Self {
        Self { kind: kind.into(), label: label.into(), notice: false }
    }

    /// A notice event kind — surfaced as a persistent callout + toast.
    pub fn notice(kind: impl Into<String>, label: impl Into<String>) -> Self {
        Self { kind: kind.into(), label: label.into(), notice: true }
    }
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
// The service API — the data crossing the FFI. The backend is PASSIVE: the Python
// pipeline drives the author→compile→judge→validate loop and calls these callouts;
// nothing here holds state across calls (see docs/rust-backend-api.md).
// ===========================================================================

/// One property to formalize (mirrors `composer.spec.types.PropertyFormulation`), plus a
/// host-assigned unique `slug` used to name its unit/artifact (Crucible: `c_<slug>`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Property {
    pub title: String,
    /// One of "attack_vector" | "safety_property" | "invariant".
    pub sort: String,
    pub description: String,
    #[serde(default)]
    pub slug: String,
}

/// The input to the authoring/gating callouts for one artifact. `kind` selects what is being
/// authored ("setup" fixture vs "component" tests); `context` carries backend dependencies
/// (e.g. the shared fixture source a component builds on). `component`/`context` are opaque
/// JSON the backend interprets.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorInput {
    pub kind: String,
    pub program: String,
    #[serde(default)]
    pub component: serde_json::Value,
    #[serde(default)]
    pub props: Vec<Property>,
    #[serde(default)]
    pub context: serde_json::Value,
}

/// An authoring instruction (+ optional backend-defined system prompt) for one LLM turn.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Prompt {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system: Option<String>,
    pub instruction: String,
}

/// Why a draft was rejected — the failing `draft` plus the compiler errors / judge feedback
/// — fed into the next `author_prompt` as revise context. `draft` is carried because each
/// authoring turn is fresh (no LLM-side memory of the prior attempt).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Failure {
    #[serde(default)]
    pub draft: String,
    pub errors: String,
}

/// The outcome of `compile`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum CompileResult {
    Ok,
    Failed { errors: String },
}

/// One report row / fuzz target: a property title and its backend-specific unit name.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Unit {
    pub property: String,
    pub unit: String,
}

/// The result of `validate` — the fused build+check for one unit. Either the harness failed
/// to BUILD (so the whole spec must be re-authored — the build is shared across units), or it
/// built and produced a per-unit `Verdict`. Fusing the build gate into `validate` (rather than a
/// separate `compile` dry-run per unit) is the component path's efficiency win — one toolchain
/// run per unit, as the old loop did (docs/rust-backend-api.md; e2e was ~2× without it).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ValidateOutcome {
    BuildFailed { errors: String },
    Verdict { verdict: Verdict },
}

/// A per-unit outcome (mirrors `composer…report.collect.Verdict`). `outcome` is one of
/// GOOD | BAD | ERROR | TIMEOUT | UNKNOWN.
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
    pub fn with_outcome(outcome: impl Into<String>) -> Self {
        Verdict { outcome: outcome.into(), line: None, duration_seconds: None, unit_file: None }
    }
}

// ===========================================================================
// Sandbox — the confinement policy (Python-authored) + the shared launcher helper.
// ===========================================================================

/// Resource caps (rlimits); `None` = unset.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Rlimits {
    #[serde(default)]
    pub mem_bytes: Option<u64>,
    #[serde(default)]
    pub cpu_seconds: Option<u64>,
    #[serde(default)]
    pub nproc: Option<u64>,
    #[serde(default)]
    pub fsize_bytes: Option<u64>,
}

/// The confinement policy for a command, authored by Python (`SandboxConfig`/`SandboxPolicy`)
/// and passed to `compile`/`validate`. `run_confined = None` runs the command directly (the
/// trusted / `none` path). The backend never invents policy — it only assembles this into a
/// `run-confined` argv (see [`run_confined`]); the mapping mirrors
/// `composer/sandbox/launcher.py::LauncherProvider.wrap`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Sandbox {
    #[serde(default)]
    pub run_confined: Option<String>,
    #[serde(default)]
    pub rw: Vec<String>,
    #[serde(default)]
    pub ro: Vec<String>,
    #[serde(default)]
    pub allow_env: Vec<String>, // "NAME=VALUE"
    #[serde(default)]
    pub network: bool,
    #[serde(default)]
    pub rlimits: Rlimits,
    #[serde(default = "default_timeout")]
    pub timeout_s: u64,
}

fn default_timeout() -> u64 {
    600
}

/// The captured result of a (confined) command.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandOutput {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Exit code synthesized when the program isn't found (mirrors shells' 127).
const NOT_FOUND_EXIT: i32 = 127;

/// Reject absolute paths / `..` traversal (mirrors `composer.sandbox.command._confined_target`).
fn confined_join(workdir: &std::path::Path, rel: &str) -> Result<std::path::PathBuf, String> {
    use std::path::{Component, Path};
    let p = Path::new(rel);
    if p.is_absolute() || p.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(format!("unsafe file path {rel:?}: absolute or traverses outside the workdir"));
    }
    Ok(workdir.join(p))
}

/// Materialize `files` into `workdir` (path-confined), then run `program args` there confined
/// by `run-confined` per `sandbox` (or directly, when `sandbox.run_confined` is `None`). Blocks
/// on the child; **call from within `Python::allow_threads`**. Enforces `sandbox.timeout_s`.
///
/// The **command line (`program`/`args`) is authored by the trusted backend**; only file
/// *contents* may derive from the LLM (`docs/command-sandbox.md` §2). `run-confined` confines
/// *itself* (Landlock+seccomp+rlimits+env scrub) and `execve`s the tool.
pub fn run_confined(
    sandbox: &Sandbox,
    program: &str,
    args: &[String],
    files: &BTreeMap<String, String>,
    workdir: &std::path::Path,
) -> Result<CommandOutput, String> {
    use std::io::Read;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    // 1. Materialize untrusted files (contents may be LLM-derived; the command line is not).
    std::fs::create_dir_all(workdir).map_err(|e| e.to_string())?;
    for (rel, contents) in files {
        let target = confined_join(workdir, rel)?;
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        std::fs::write(&target, contents).map_err(|e| e.to_string())?;
    }

    // 2. Build argv: `run-confined <policy flags> -- program args`, or `program args` direct.
    let mut cmd = match &sandbox.run_confined {
        Some(bin) => {
            let mut c = Command::new(bin);
            for p in &sandbox.ro {
                c.arg("--ro").arg(p);
            }
            for p in &sandbox.rw {
                c.arg("--rw").arg(p);
            }
            for e in &sandbox.allow_env {
                c.arg("--allow-env").arg(e);
            }
            if sandbox.network {
                c.arg("--allow-network");
            }
            if let Some(v) = sandbox.rlimits.mem_bytes {
                c.arg("--rlimit-as").arg(v.to_string());
            }
            if let Some(v) = sandbox.rlimits.cpu_seconds {
                c.arg("--rlimit-cpu").arg(v.to_string());
            }
            if let Some(v) = sandbox.rlimits.nproc {
                c.arg("--rlimit-nproc").arg(v.to_string());
            }
            if let Some(v) = sandbox.rlimits.fsize_bytes {
                c.arg("--rlimit-fsize").arg(v.to_string());
            }
            c.arg("--").arg(program).args(args);
            c
        }
        None => {
            let mut c = Command::new(program);
            c.args(args);
            c
        }
    };
    cmd.current_dir(workdir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    // 3. Spawn + capture with a timeout. Reader threads avoid a pipe-buffer deadlock.
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(CommandOutput {
                exit_code: NOT_FOUND_EXIT,
                stdout: String::new(),
                stderr: format!("{}: not found", sandbox.run_confined.as_deref().unwrap_or(program)),
            })
        }
        Err(e) => return Err(e.to_string()),
    };
    let mut out = child.stdout.take().expect("piped stdout");
    let mut err = child.stderr.take().expect("piped stderr");
    let t_out = std::thread::spawn(move || {
        let mut s = Vec::new();
        let _ = out.read_to_end(&mut s);
        s
    });
    let t_err = std::thread::spawn(move || {
        let mut s = Vec::new();
        let _ = err.read_to_end(&mut s);
        s
    });

    let deadline = Instant::now() + Duration::from_secs(sandbox.timeout_s.max(1));
    let mut timed_out = false;
    let status = loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(st) => break Some(st),
            None => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    timed_out = true;
                    break None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
    };
    let stdout = String::from_utf8_lossy(&t_out.join().unwrap_or_default()).into_owned();
    let stderr = String::from_utf8_lossy(&t_err.join().unwrap_or_default()).into_owned();
    if timed_out {
        return Ok(CommandOutput {
            exit_code: -1,
            stdout,
            stderr: format!("{stderr}\ncommand timed out after {}s", sandbox.timeout_s),
        });
    }
    Ok(CommandOutput {
        exit_code: status.and_then(|s| s.code()).unwrap_or(-1),
        stdout,
        stderr,
    })
}

// ===========================================================================
// The trait an application implements.
// ===========================================================================

/// A Rust AutoProver backend — a **passive service** the Python pipeline drives. One instance
/// per wheel; construct it in [`export_app!`]. Metadata/authoring callouts are pure; `compile`
/// and `validate` run the toolchain (via [`run_confined`]) and BLOCK — the host calls them off
/// the event loop (`asyncio.to_thread`) while the wheel releases the GIL.
pub trait Backend: Send + Sync + 'static {
    /// The declaration the Python host reads at load time.
    fn descriptor(&self) -> AppDescriptor;

    /// Validate application-specific preconditions before any service opens. `Err(msg)` aborts.
    fn validate_preconditions(&self, _args: &serde_json::Value) -> Result<(), String> {
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
    fn compile(
        &self,
        input: &AuthorInput,
        spec: &str,
        workdir: &std::path::Path,
        sandbox: &Sandbox,
    ) -> CompileResult;

    /// Build + check ONE unit against the spec (the fused build gate — no separate compile for
    /// components). Returns [`ValidateOutcome::BuildFailed`] to trigger a re-author of the whole
    /// spec (the build is shared across units), or a per-unit [`Verdict`]. Per-unit so the host
    /// owns enumeration/scheduling. BLOCKING.
    fn validate(
        &self,
        input: &AuthorInput,
        spec: &str,
        unit: &str,
        workdir: &std::path::Path,
        sandbox: &Sandbox,
    ) -> ValidateOutcome;

    /// Optional run-level artifacts from the full outcome set, as `{relpath: contents}`.
    fn finalize(&self, _outcomes: &serde_json::Value) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}

// ===========================================================================
// FFI helpers — the sync, JSON-string boundary. `export_app!` wraps these in
// #[pyfunction]s (compile/validate release the GIL); also unit-testable without Python.
// ===========================================================================

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
        Err(e) => ValidateOutcome::Verdict {
            verdict: Verdict { outcome: "ERROR".into(), line: None, duration_seconds: None, unit_file: Some(e) },
        },
    };
    serde_json::to_string(&outcome).unwrap_or_default()
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

// ===========================================================================
// The export macro.
// ===========================================================================

/// Emit the PyO3 module the Python host loads. Invoke it once in an application crate
/// (a `cdylib` depending on `autoprover-sdk` and `pyo3`):
///
/// ```ignore
/// autoprover_sdk::export_app!(my_app, MyApp::new());
/// ```
///
/// `module_ident` MUST match the wheel's module name. The expansion defines the pure callouts
/// (`descriptor`/`validate_preconditions`/`units`/`author_prompt`/`judge_prompt`/`finalize`) and
/// the two BLOCKING ones (`compile`/`validate`, which release the GIL while `run-confined` runs),
/// all delegating to the `ffi_*` helpers.
#[macro_export]
macro_rules! export_app {
    ($module:ident, $ctor:expr) => {
        fn __autoprover_app() -> &'static dyn $crate::Backend {
            static APP: ::std::sync::OnceLock<::std::boxed::Box<dyn $crate::Backend>> =
                ::std::sync::OnceLock::new();
            &**APP.get_or_init(|| ::std::boxed::Box::new($ctor))
        }

        #[$crate::pyo3::pyfunction]
        fn descriptor() -> ::std::string::String {
            $crate::ffi_descriptor(__autoprover_app())
        }

        #[$crate::pyo3::pyfunction]
        fn validate_preconditions(
            args_json: ::std::string::String,
        ) -> ::std::option::Option<::std::string::String> {
            $crate::ffi_validate_preconditions(__autoprover_app(), &args_json)
        }

        #[$crate::pyo3::pyfunction]
        fn units(input_json: ::std::string::String) -> ::std::string::String {
            $crate::ffi_units(__autoprover_app(), &input_json)
        }

        #[$crate::pyo3::pyfunction]
        #[pyo3(signature = (input_json, failure_json=None))]
        fn author_prompt(
            input_json: ::std::string::String,
            failure_json: ::std::option::Option<::std::string::String>,
        ) -> ::std::string::String {
            $crate::ffi_author_prompt(__autoprover_app(), &input_json, failure_json.as_deref())
        }

        #[$crate::pyo3::pyfunction]
        fn judge_prompt(
            input_json: ::std::string::String,
            spec: ::std::string::String,
        ) -> ::std::option::Option<::std::string::String> {
            $crate::ffi_judge_prompt(__autoprover_app(), &input_json, &spec)
        }

        #[$crate::pyo3::pyfunction]
        fn compile(
            py: $crate::pyo3::Python<'_>,
            input_json: ::std::string::String,
            spec: ::std::string::String,
            workdir: ::std::string::String,
            sandbox_json: ::std::string::String,
        ) -> ::std::string::String {
            // Release the GIL for the (minutes-long) build — no async runtime needed.
            py.allow_threads(move || {
                $crate::ffi_compile(__autoprover_app(), &input_json, &spec, &workdir, &sandbox_json)
            })
        }

        #[$crate::pyo3::pyfunction]
        fn validate(
            py: $crate::pyo3::Python<'_>,
            input_json: ::std::string::String,
            spec: ::std::string::String,
            unit: ::std::string::String,
            workdir: ::std::string::String,
            sandbox_json: ::std::string::String,
        ) -> ::std::string::String {
            py.allow_threads(move || {
                $crate::ffi_validate(
                    __autoprover_app(),
                    &input_json,
                    &spec,
                    &unit,
                    &workdir,
                    &sandbox_json,
                )
            })
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
            m.add_function($crate::pyo3::wrap_pyfunction!(units, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(author_prompt, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(judge_prompt, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(compile, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(validate, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(finalize, m)?)?;
            ::std::result::Result::Ok(())
        }
    };
}
