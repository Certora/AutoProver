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
    /// Design-doc discovery, which the host's entry point runs *before* the pipeline (and only when
    /// the doc wasn't given on the command line). Unlike the four above it is optional: claim it to
    /// give that task a section of its own, or leave it and the host groups the task under the first
    /// declared phase.
    Discovery,
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
/// results such as a per-unit verdict. Ordinary kinds stream into the log.
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
    /// Under `DeliverableMode::Callout`, the project-relative path of the primary deliverable
    /// file, `{program}`-templated (Crucible: `fuzz/{program}/src/main.rs`). Used only as each
    /// component's report link — the actual files come from `finalize`. Ignored `PerComponent`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deliverable_primary: Option<String>,
}

/// A shared "setup" artifact authored once, before per-component formalization (Crucible's
/// shared fixture). When a descriptor carries one, the host runs the author→compile loop for a
/// `kind="setup"` [`AuthorInput`] under `phase_key`, then threads the compiled spec into every
/// component's `AuthorInput.context` under `context_key`. Absent → no setup step.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SetupSpec {
    /// The descriptor phase key the setup task is grouped under (a UI-only phase).
    pub phase_key: String,
    /// The task label shown in the frontend.
    pub label: String,
    /// The `AuthorInput.context` key the compiled setup spec is injected under for components.
    pub context_key: String,
}

/// An analysis-independent **preflight** gate on the prepared workspace, run *concurrently with
/// system analysis* — before a single property exists (`docs/rust-backend-api.md` §3.1).
///
/// When a descriptor carries one, the host follows its [`WorkspacePrep`] with a
/// `kind="preflight"` [`Backend::compile`] call whose `spec` is **empty**: nothing has been
/// authored yet, so the wheel renders its own minimal skeleton — the smallest artifact that still
/// exercises everything an authored one will depend on.
///
/// The point is to fail on a *toolchain* problem — an unresolvable dependency graph, a harness that
/// doesn't link, IDL codegen the generator rejects — while the run has spent almost no LLM budget.
/// Without it, such a problem first surfaces as compiler errors in the first authored draft, which
/// the author cannot fix: it does not own the manifest, and it burns every re-author attempt
/// trying. So a preflight failure is **terminal** — the host raises instead of re-authoring, and
/// the driver cancels the analysis and extraction running alongside it.
///
/// Absent → no gate; the workspace prep still runs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PreflightSpec {
    /// The descriptor phase key the preflight task is grouped under (a UI-only phase).
    pub phase_key: String,
    /// The task label shown in the frontend.
    pub label: String,
}

/// How the source deliverable is written to disk. `PerComponent` (the default): the generic
/// store writes one `{prefix}_{slug}.{ext}` file per component from its `artifact_text`.
/// `Callout`: the store writes no per-component source; the wheel's `finalize` renders the whole
/// deliverable (e.g. Crucible's one shared crate assembled from all sections + the fixture).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliverableMode {
    #[default]
    PerComponent,
    Callout,
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
    /// Optional preflight gate on the prepared workspace, run concurrently with system analysis
    /// (see [`PreflightSpec`]).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preflight: Option<PreflightSpec>,
    /// Optional shared-setup step run before per-component formalization (see [`SetupSpec`]).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub setup: Option<SetupSpec>,
    /// How the source deliverable is written (see [`DeliverableMode`]).
    #[serde(default)]
    pub deliverable_mode: DeliverableMode,
    /// Serialize the blocking toolchain callouts (`prepare_workspace`/`compile`/`validate`) on
    /// one semaphore — set when the app shares a single build dir / target across units.
    #[serde(default)]
    pub serialize_toolchain: bool,
    /// Default to the fail-closed `launcher` sandbox provider (still overridable by
    /// `COMPOSER_SANDBOX_PROVIDER`). Set by any wheel that runs untrusted native toolchains.
    #[serde(default)]
    pub confine_by_default: bool,
    /// Human noun for one formalized unit in the console/TUI summary ("instruction" for
    /// Crucible). Defaults to "component".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub component_noun: Option<String>,
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

/// Where the code under analysis lives as a compilation unit, for a wheel that must *depend* on
/// it (Crucible's harness declares the program under test as a path dependency). The host resolves
/// it from the main source file's manifest (`composer.spec.cargo`) because none of the three parts
/// follows from `AuthorInput::program`: that is the *analysis* identifier, while a crate's
/// directory, package name and lib name are independent of each other and of it (a real lending
/// program we hit had directory `programs/lend`, package `example-lending`, lib `example_lending`).
///
/// Every field may be empty — a host that resolved nothing, or one predating this struct. Read it
/// through [`ProgramCrate::resolved`], which fills the gaps from the old `programs/<program>`
/// convention, rather than using the fields raw.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProgramCrate {
    /// The crate directory relative to the project root, forward-slashed (`"."` = the root).
    #[serde(default)]
    pub dir: String,
    /// `[package] name` — the key a dependent's `[dependencies]` must use.
    #[serde(default)]
    pub package: String,
    /// The lib target name — the Rust identifier (`use <lib>::*`) and the built artifact's
    /// basename (`target/deploy/<lib>.so`). NOT interchangeable with `package`, which may
    /// contain `-`.
    #[serde(default)]
    pub lib: String,
    /// The crate's declared `anchor-lang` requirement, verbatim (`"0.29.0"`, `"^1.0.1"`, `">=0.31"`;
    /// workspace inheritance already resolved by the host). Empty when the crate declares none, so
    /// it is not an Anchor program — or the host couldn't tell.
    ///
    /// A *depending* wheel needs this because the dependency is only usable when the program's
    /// Anchor major matches the one the wheel's own stack links: Anchor's generated
    /// `InstructionData` / `ToAccountMetas` impls are tied to the exact `anchor-lang` crate that
    /// generated them, so a program on a different major can't satisfy a harness's trait bounds no
    /// matter how the versions are pinned.
    #[serde(default)]
    pub anchor: String,
}

impl ProgramCrate {
    /// This crate with every empty part filled from the pre-`program_crate` convention: the crate
    /// sits at `programs/<program>` and is named `<program>`. The fallback only holds for a
    /// workspace whose directory and package names happen to match the analysis identifier, but it
    /// keeps a host that sends nothing working exactly as before.
    pub fn resolved(&self, program: &str) -> ProgramCrate {
        let package =
            if self.package.is_empty() { program.to_string() } else { self.package.clone() };
        ProgramCrate {
            dir: if self.dir.is_empty() { format!("programs/{program}") } else { self.dir.clone() },
            lib: if self.lib.is_empty() { package.replace('-', "_") } else { self.lib.clone() },
            package,
            anchor: self.anchor.clone(),
        }
    }

    /// The compatibility unit of the crate's declared `anchor-lang` requirement — the major, or
    /// `(0, minor)` for a `0.x` release, since Cargo treats a `0.x` minor as a major. `None` when
    /// no requirement is declared or it isn't a plain version (a git/path dep, say).
    ///
    /// Compare it against the wheel's own `anchor-lang` to decide whether depending on the program
    /// crate is even possible (see the `anchor` field).
    pub fn anchor_compat(&self) -> Option<(u64, u64)> {
        anchor_compat_key(&self.anchor)
    }
}

/// The `(major, minor-if-0.x)` compatibility unit of a version requirement: leading operators and
/// whitespace are dropped (`">=0.31"` → `(0, 31)`, `"^1.0.1"` → `(1, 0)`), and anything that isn't
/// a plain `major[.minor]` is `None`.
pub fn anchor_compat_key(req: &str) -> Option<(u64, u64)> {
    let digits = req.trim_start_matches(['^', '~', '=', '>', '<', ' ']);
    let mut parts = digits.split(['.', ',', ' ', '-', '+']);
    let major: u64 = parts.next()?.parse().ok()?;
    let minor: u64 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    Some(if major == 0 { (0, minor) } else { (major, 0) })
}

/// The input to the authoring/gating callouts for one artifact. `context` carries backend
/// dependencies (e.g. the shared fixture source a component builds on). `component`/`context` are
/// opaque JSON the backend interprets.
///
/// `kind` selects what is being authored or gated. The host sends three:
///
///  * `"preflight"` — nothing is authored; the wheel renders its own skeleton and `compile` gates
///    the prepared workspace (see [`PreflightSpec`]). `props` is empty and `component` carries
///    nothing: it runs before analysis has finished.
///  * `"setup"` — the shared artifact every unit builds on (Crucible's fixture), authored once from
///    every unit's properties (see [`SetupSpec`]). `component` carries the analyzed model.
///  * `"component"` — one unit's spec. `component` carries that unit, `context` the setup artifact.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorInput {
    pub kind: String,
    /// The analysis identifier of the program/contract under test (the `Name` half of the host's
    /// `path:Name` argument) — a label and a namespace, NOT a Cargo name: see [`ProgramCrate`].
    pub program: String,
    /// The compilation unit holding the program's source, when the host's language has one.
    #[serde(default)]
    pub program_crate: ProgramCrate,
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

/// Whether a draft was rejected by the toolchain or by the optional LLM judge — so a
/// re-author can frame the retry correctly (a judge rejection is NOT a build failure: the
/// draft compiled). Defaults to `Compile` for backends/hosts that predate the field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureKind {
    #[default]
    Compile,
    Judge,
}

/// Why a draft was rejected — the failing `draft` plus the compiler errors / judge feedback
/// — fed into the next `author_prompt` as revise context. `draft` is carried because each
/// authoring turn is fresh (no LLM-side memory of the prior attempt). `kind` says which gate
/// rejected it so the revise prompt can distinguish compiler errors from review feedback.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Failure {
    #[serde(default)]
    pub draft: String,
    pub errors: String,
    #[serde(default)]
    pub kind: FailureKind,
}

/// The outcome of `compile`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum CompileResult {
    Ok,
    Failed { errors: String },
}

/// One report row: a property title and its backend-specific unit name (the rule the report keys
/// by). `target` is the *validation target the host runs* — several report units may share one
/// target (e.g. Crucible puts a component's whole property set in one `c_<slug>` target), so the host runs the
/// target once and the backend attributes the outcome back to each unit. `None` ⇒ the unit is its
/// own target (one run per unit, the default).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Unit {
    pub property: String,
    pub unit: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
}

impl Unit {
    /// The validation target this unit is checked by — its own `unit` name unless it shares a
    /// target with others.
    pub fn target_or_unit(&self) -> &str {
        self.target.as_deref().unwrap_or(&self.unit)
    }
}

/// A pure plan for preparing the workspace before formalization (Crucible: place the harness
/// `Cargo.toml`, warm its deps, build the program `.so`). The wheel *declares* the plan; the
/// **host executes it with the shared toolchain helpers**, so the standard network posture holds
/// without the wheel touching a command line: dependency fetches run *unconfined* (network, no
/// untrusted code), and the code-executing build runs *confined + offline*
/// (`docs/command-sandbox.md` §5). This keeps warming out of confinement — the codebase never
/// gives a confined process network — while still letting a pure-Rust app own its layout.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WorkspacePrep {
    /// Files to write under the workdir (path-confined) before warming — e.g. the harness
    /// `Cargo.toml` (whose deps only the wheel knows). Contents only; no command line.
    #[serde(default)]
    pub files: BTreeMap<String, String>,
    /// Project-relative manifest dirs to `cargo fetch` (unconfined, network) so a later
    /// confined + offline build finds every dep warm in the private `CARGO_HOME`.
    #[serde(default)]
    pub warm_dirs: Vec<String>,
    /// If set, build the workspace and expect this artifact, via the host's shared build
    /// capability (Solana: `cargo-build-sbf` → `target/deploy/<name>.so`). It names the *build
    /// artifact*, so for Cargo it is the crate's lib target ([`ProgramCrate::lib`]) — not the
    /// analysis identifier, which need not match it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub build_program: Option<String>,
    /// If set, the wheel needs the program's **IDL** and wants it at this workdir-relative path.
    /// The host obtains it (a user-supplied file, else the build capability's IDL build), writes it
    /// there, and echoes the path back as the `idl` context key on every later `AuthorInput` — so
    /// "the key is set" means "the file is in place". A hard error if it can't be produced: the
    /// wheel only asks when it cannot proceed without one.
    ///
    /// This is what lets a harness target a program whose toolchain it can't link against
    /// ([`ProgramCrate::anchor`]): types generated from the IDL belong to the *wheel's* stack, so
    /// the program's own dependency graph never enters the harness build.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub idl_dest: Option<String>,
}

/// Extra sandbox grants a wheel needs unioned into the host-authored policy (Crucible: the
/// crucible checkout + the `crucible` binary dir as read-only). Pure data — the wheel declares
/// grants, Python decides the policy; the wheel never invents confinement.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SandboxGrants {
    /// Extra read-only paths.
    #[serde(default)]
    pub extra_ro: Vec<String>,
    /// Extra `NAME=VALUE` env entries to pass through confinement.
    #[serde(default)]
    pub extra_env: Vec<String>,
}

/// The result of `validate` — the fused build+check for one validation **target**. Either the
/// build failed (so the whole spec must be re-authored — the build is shared), or it built and
/// produced a `Verdict` **per report unit the target covers** (`(unit, verdict)`). A target may
/// cover several units (e.g. Crucible runs every invariant in one target), and the backend — which
/// owns its own result/failure format — attributes the run to those units; the host records the
/// verdicts verbatim (it does no verdict logic). Fusing the build gate into `validate` (rather than
/// a separate `compile` dry-run) is the component path's efficiency win (docs/rust-backend-api.md).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ValidateOutcome {
    BuildFailed { errors: String },
    Verdicts { verdicts: Vec<(String, Verdict)> },
}

/// What checking one unit concluded — the report's backend-agnostic vocabulary (mirrors
/// `composer…report.schema.Outcome`), which every backend's native status maps into. The
/// human-facing wording ("No counterexample" vs "Verified") is picked at render time from the
/// application's `backend_tag`, so a backend never spells it out here.
///
/// An enum rather than a free string: the host validates this field against the same closed set, so
/// a typo that used to reach a report row and read there as an unexplained `UNKNOWN` now doesn't
/// compile.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Outcome {
    /// The property holds.
    Good,
    /// The property is violated — `Verdict::detail` should carry the counterexample.
    Bad,
    /// The check errored out without reaching a verdict.
    Error,
    /// The check ran out of time without reaching a verdict.
    Timeout,
    /// No conclusive result.
    Unknown,
}

/// A per-unit outcome (mirrors `composer…report.collect.Verdict`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Verdict {
    pub outcome: Outcome,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duration_seconds: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit_file: Option<String>,
    /// Human-readable explanation of a non-GOOD outcome — the failure detail (a counterexample /
    /// assertion message) for a `BAD`, or the error text for an `ERROR`. Surfaced live and persisted
    /// to the report so a verdict is self-explaining (otherwise a bare `BAD` gives no clue why).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl Verdict {
    /// A bare verdict: just the outcome, no diagnostics. Set the fields you have on the result.
    pub fn with_outcome(outcome: Outcome) -> Self {
        Verdict { outcome, line: None, duration_seconds: None, unit_file: None, detail: None }
    }

    /// A failing verdict carrying its explanation — the shape a backend almost always wants for a
    /// `Bad` or an `Error`, since a bare one gives a reader no clue why.
    pub fn detailed(outcome: Outcome, detail: impl Into<String>) -> Self {
        Verdict { detail: Some(detail.into()), ..Verdict::with_outcome(outcome) }
    }
}

// ===========================================================================
// Sandbox — the confinement wrapper (Python-authored) + the shared launcher helper.
// ===========================================================================

/// The confinement wrapper for a command, authored by Python
/// (`SandboxConfig.backend_spec`) and passed to `compile`/`validate`. The backend never
/// invents policy or names a sandbox mechanism: Python owns the confinement *intent* and
/// translates it into `argv_prefix`, an opaque argv the backend simply prepends to its
/// command — `[*argv_prefix, program, *args]` (see [`run_confined`]).
///
/// `argv_prefix` is **empty** for a passthrough (`provider="none"`) spec — the command runs
/// directly (the trusted path). Otherwise it is a full `run-confined <flags…> --` wrapper
/// (mirrors `composer/sandbox/launcher.py::LauncherProvider.argv_prefix`); its first element
/// is the launcher binary. Because the prefix is opaque, swapping the sandbox mechanism never
/// changes this shape.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Sandbox {
    #[serde(default)]
    pub argv_prefix: Vec<String>,
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

/// Materialize `files` into `workdir` (path-confined), then run `program args` there behind
/// `sandbox.argv_prefix` — i.e. `[*argv_prefix, program, *args]` (or `program args` directly,
/// when `argv_prefix` is empty). Blocks on the child; **call from within
/// `Python::allow_threads`**. Enforces `sandbox.timeout_s`.
///
/// The **command line (`program`/`args`) is authored by the trusted backend**; only file
/// *contents* may derive from the LLM (`docs/command-sandbox.md` §2). When present, the prefix's
/// `run-confined` confines *itself* (Landlock+seccomp+rlimits+env scrub) and `execve`s the tool.
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

    // 2. Build argv by prepending the (opaque, Python-authored) confinement prefix:
    // `[*argv_prefix, program, *args]`. When the prefix is empty this is `program args`
    // run directly; otherwise its first element is the launcher binary to spawn.
    let (launch, mut launch_args): (&str, Vec<String>) = match sandbox.argv_prefix.split_first() {
        Some((bin, rest)) => (bin, rest.to_vec()),
        None => (program, Vec::new()),
    };
    if !sandbox.argv_prefix.is_empty() {
        // The prefix ends at its `--`; the wrapped command follows it.
        launch_args.push(program.to_string());
    }
    launch_args.extend_from_slice(args);
    let mut cmd = Command::new(launch);
    cmd.args(&launch_args);
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
                stderr: format!("{launch}: not found"),
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
    ///
    /// Also the preflight gate: for `input.kind == "preflight"` the `spec` is empty and the wheel
    /// supplies its own skeleton, so one implementation covers "does the authored artifact build"
    /// and "could *any* artifact build here" (see [`PreflightSpec`]).
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

    /// Extra sandbox grants to union into the host's policy (see [`SandboxGrants`]). Pure; called
    /// once before any confined step. Default: no extra grants.
    fn sandbox_grants(&self, _args: &serde_json::Value) -> SandboxGrants {
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
    /// Under [`DeliverableMode::Callout`] this renders the whole source deliverable (Crucible's
    /// one crate); the host enriches the outcome set with each component's `artifact_text` /
    /// `property_units` and the `setup` result so the wheel has everything the deliverable needs.
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
        fn sandbox_grants(args_json: ::std::string::String) -> ::std::string::String {
            $crate::ffi_sandbox_grants(__autoprover_app(), &args_json)
        }

        #[$crate::pyo3::pyfunction]
        fn workspace_prep(input_json: ::std::string::String) -> ::std::string::String {
            $crate::ffi_workspace_prep(__autoprover_app(), &input_json)
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
            m.add_function($crate::pyo3::wrap_pyfunction!(sandbox_grants, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(workspace_prep, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(finalize, m)?)?;
            ::std::result::Result::Ok(())
        }
    };
}
