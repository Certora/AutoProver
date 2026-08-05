//! The declarative spine the Python host consumes to synthesize the phase enum,
//! argparse, frontend and artifact store (see `docs/rust-applications.md` §3).

use serde::{Deserialize, Serialize};

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
/// shared fixture). When a descriptor carries one, the host runs the author→compile loop for an
/// [`Authored::Setup`](crate::authoring::Authored::Setup) input under `phase_key`, then hands the
/// compiled spec to every component as [`AuthorInput::setup`](crate::AuthorInput::setup).
/// Absent → no setup step.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SetupSpec {
    /// The descriptor phase key the setup task is grouped under (a UI-only phase).
    pub phase_key: String,
    /// The task label shown in the frontend.
    pub label: String,
}

/// An analysis-independent **preflight** gate on the prepared workspace, run *concurrently with
/// system analysis* — before a single property exists (`docs/rust-applications.md` §4.2).
///
/// When a descriptor carries one, the host follows its [`WorkspacePrep`](crate::WorkspacePrep) with
/// a `kind="preflight"` [`Backend::compile`](crate::Backend::compile) call whose `spec` is
/// **empty**: nothing has been authored yet, so the wheel renders its own minimal skeleton — the
/// smallest artifact that still exercises everything an authored one will depend on.
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
