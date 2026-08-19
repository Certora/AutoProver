//! The declarative spine the Python host consumes to synthesize the phase enum,
//! argparse, frontend and artifact store (see `docs/rust-applications.md` §3).

use serde::{Deserialize, Serialize};

/// Which step of the run a declared phase groups — and, for the steps the host runs as their own
/// visible task, the declaration *of* that step: its task is the phase's label under the phase
/// itself. A role no phase claims is a step this application doesn't have.
///
/// The four the driver itself runs must each be claimed exactly once; the rest are optional.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
pub enum PhaseRole {
    /// Grouping only — the host runs no step of its own here (cf. autoprove's harness/autosetup).
    #[default]
    Grouping,
    Analysis,
    Extraction,
    Formalization,
    Report,
    /// Design-doc discovery, which the host's entry point runs *before* the pipeline (and only when
    /// the doc wasn't given on the command line). Optional: claim it to give that task a section of
    /// its own, or leave it and the host groups the task under the first declared phase.
    Discovery,
    /// The analysis-independent gate on the prepared workspace, run concurrently with system
    /// analysis — before a single property exists. The host follows
    /// [`WorkspacePrep`](crate::prep::WorkspacePrep) with an
    /// [`Authored::Preflight`](crate::authoring::Authored::Preflight) `compile` whose `spec` is
    /// **empty**: nothing has been authored yet, so the wheel renders its own skeleton — the smallest
    /// spec that still exercises everything an authored one will depend on.
    ///
    /// The point is to fail on a *toolchain* problem — an unresolvable dependency graph, a harness
    /// that doesn't link, IDL codegen the generator rejects — while the run has spent almost no LLM
    /// budget. Without it, such a problem first surfaces as compiler errors in the first authored
    /// draft, which the author cannot fix: it does not own the manifest, and it burns every
    /// re-author attempt trying. So a preflight failure is **terminal** — the host raises instead of
    /// re-authoring, and the driver cancels the analysis and extraction running alongside it.
    Preflight,
    /// The shared spec authored once before per-component formalization (Crucible's fixture).
    /// The host runs the author→compile loop for an
    /// [`Authored::Setup`](crate::authoring::Authored::Setup) input, then hands the compiled spec
    /// to every component as
    /// [`AuthorInput::setup`](crate::authoring::AuthorInput::setup).
    Setup,
}

impl PhaseRole {
    /// The four steps the shared driver runs itself, and therefore tags every run with. Every
    /// application must claim each of them.
    pub const REQUIRED: [PhaseRole; 4] = [
        Self::Analysis,
        Self::Extraction,
        Self::Formalization,
        Self::Report,
    ];
}

/// One task-grouping phase. `key` becomes the synthesized `enum.Enum` member name; `label`/`order`
/// drive UI grouping; `role` says which step of the run it groups (and declares that step).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct PhaseSpec {
    pub key: String,
    pub label: String,
    pub order: u32,
    pub role: PhaseRole,
}

impl PhaseSpec {
    /// A phase that only groups tasks.
    pub fn grouping(key: impl Into<String>, label: impl Into<String>, order: u32) -> Self {
        Self {
            key: key.into(),
            label: label.into(),
            order,
            role: PhaseRole::Grouping,
        }
    }

    /// A phase that groups — and declares — the step `role`.
    pub fn step(
        key: impl Into<String>,
        label: impl Into<String>,
        order: u32,
        role: PhaseRole,
    ) -> Self {
        Self {
            key: key.into(),
            label: label.into(),
            order,
            role,
        }
    }
}

/// Default value for a declared CLI argument.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub enum ArgDefault {
    Str { value: Option<String> },
    Int { value: Option<i64> },
    Bool { value: bool },
}

/// A CLI flag the generic entry point adds beyond the three positional inputs
/// (`project_root`, `main_contract`, `system_doc`).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct ArgSpec {
    pub flag: String,
    pub help: String,
    pub default: ArgDefault,
    pub required: bool,
}

/// A domain event kind the frontend should render.
///
/// A `notice` kind is surfaced as a persistent, always-visible callout (plus a toast)
/// rather than a line in the collapsible per-task events log — for one-shot important
/// results such as one check's verdict. Ordinary kinds stream into the log.
///
/// The events themselves are emitted by the **host**, around the callouts it drives (a build
/// failure, a review verdict, a check's outcome): a wheel has no emit channel, because its blocking
/// callouts run to completion with the GIL released. Declaring a kind nothing emits renders
/// nothing.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct EventKind {
    pub kind: String,
    pub label: String,
    pub notice: bool,
}

impl EventKind {
    /// A streaming event kind — rendered as a line in the collapsible events log.
    pub fn log(kind: impl Into<String>, label: impl Into<String>) -> Self {
        Self {
            kind: kind.into(),
            label: label.into(),
            notice: false,
        }
    }

    /// A notice event kind — surfaced as a persistent callout + toast.
    pub fn notice(kind: impl Into<String>, label: impl Into<String>) -> Self {
        Self {
            kind: kind.into(),
            label: label.into(),
            notice: true,
        }
    }
}

/// On-disk deliverable layout. All paths are project-root-relative.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
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
    /// The store's term for the property→check map file suffix (`property_rules`, `property_tests`).
    pub property_suffix: String,
}

/// How the source deliverable is written to disk.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub enum DeliverableMode {
    /// The generic store writes one `{prefix}_{slug}.{ext}` file per component from its
    /// `artifact_text`.
    #[default]
    PerComponent,
    /// The store writes no per-component source; the wheel's `finalize` renders the whole
    /// deliverable (e.g. Crucible's one shared crate assembled from all sections + the fixture).
    Callout {
        /// Does the deliverable have a representative primary file? `finalize` renders a whole
        /// tree, but every delivered component still records one path — its basename becomes the
        /// component's `unit_file`, the report's rule-identity fallback, echoed back to
        /// `finalize` — and the store can't guess where in that tree the components' checks land.
        /// A path (project-relative, `{program}`-templated — Crucible:
        /// `certora/crucible/fuzz/{program}/src/main.rs`) names that file; `None` declares that
        /// no one file represents the deliverable, and components anchor to the layout's `deliverable_dir`
        /// instead. The wire requires the key either way, so opting out is an explicit choice,
        /// not an omitted field. Carried by the variant because it means nothing under
        /// `PerComponent`.
        #[serde(deserialize_with = "crate::required::present")]
        deliverable_path: Option<String>,
    },
}

/// How a violated check of this backend becomes a written audit finding.
///
/// Declared by the wheel because a write-up rests on claims only the wheel can make: what its
/// evidence *is* — a symbolic refutation, or a fuzzer's crash against a harness the author wrote —
/// what that evidence establishes, and how to read its own markers. The host owns everything
/// around that: which rows, each row's properties and the audit groups they sit in, the
/// concurrency, grouping the rows that share one finding, and composing the record. It owns none
/// of the prose.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct FindingsDeclaration {
    /// The *domain* half of the write-up system prompt — what this backend's evidence is, what it
    /// does and does not establish, and what its markers mean. The host appends the output contract
    /// (the sections asked for, the grounding rules), so no wheel restates it.
    pub system: String,
}

/// The complete declaration the Python host reads once at load time.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppDescriptor {
    pub name: String,
    pub header_text: String,
    /// The ecosystem (chain) tag: "evm" | "solana" | "soroban". Selects the shared front
    /// half's system model + prompts; the Python host resolves it against its ecosystem
    /// registry, and rejects a tag it doesn't know.
    pub ecosystem: String,
    /// The report's backend tag (`AutoProverReport.backend`).
    pub backend_tag: String,
    /// Prose injected into the property-extraction prompt (verification-surface guidance).
    pub backend_guidance: String,
    /// The system-analysis cache key (`SystemAnalysisSpec.analysis_key`).
    pub analysis_key: String,
    pub phases: Vec<PhaseSpec>,
    pub args: Vec<ArgSpec>,
    #[serde(deserialize_with = "crate::required::present")]
    pub rag_db_default: Option<String>,
    pub event_kinds: Vec<EventKind>,
    pub artifact_layout: ArtifactLayout,
    /// How the source deliverable is written (see [`DeliverableMode`]).
    pub deliverable_mode: DeliverableMode,
    /// Serialize the blocking toolchain callouts (`prepare_workspace`/`compile`/`validate`) on
    /// one semaphore — set when the app shares a single build dir / target across components.
    pub serialize_toolchain: bool,
    /// Default to the fail-closed `launcher` sandbox provider (still overridable by
    /// `COMPOSER_SANDBOX_PROVIDER`). Set by any wheel that runs untrusted native toolchains.
    pub confine_by_default: bool,
    /// Human noun for one formalized component in the console/TUI summary ("instruction" for
    /// Crucible). Defaults to "component".
    #[serde(deserialize_with = "crate::required::present")]
    pub component_noun: Option<String>,
    /// What this backend calls one [`Check`](crate::outcome::Check) *to the model* — "rule",
    /// "harness function", "invariant". It is the word the authoring prompts use throughout, so it
    /// should be the word the wheel's own prompts and its generated code already use.
    ///
    /// Declared rather than fixed because an author writes better when the prompt speaks its
    /// domain's language; the host's tool *names* stay `check`-worded either way, so this changes
    /// prose, not the API the model calls. `None` → "check".
    #[serde(deserialize_with = "crate::required::present")]
    pub check_noun: Option<String>,
    /// What an author may cite when it rebuts the judge's prior-round feedback. The host builds the
    /// rebuttal tool's `evidence_type` from this, so it is a closed set the model picks from.
    ///
    /// Declared per wheel because the evidence a backend can actually produce is a property of that
    /// backend: a fuzzing wheel can show a counterexample, a typechecking one an error from a
    /// checker that never runs code. See [`EVIDENCE_KINDS`] for the default set.
    pub evidence_kinds: Vec<String>,
    /// How this backend's violated checks are written up as audit findings (see
    /// [`FindingsDeclaration`]) — `None` produces none, and the report carries only the verdict rows.
    ///
    /// Declining is the default because a write-up has to say what the evidence behind it is, and
    /// only the wheel knows. A run that reports verdicts without claiming to know what they
    /// establish is a coherent wheel; one whose findings are prose the host guessed is not.
    #[serde(deserialize_with = "crate::required::present")]
    pub findings: Option<FindingsDeclaration>,
}

/// The evidence an author can usually offer, for a wheel with no reason to name its own: the build
/// failed, the checker said so, the checker produced a counterexample, the manual says so, or the
/// author is arguing. The last is deliberately last — an argument is a conversation, not a veto.
pub const EVIDENCE_KINDS: [&str; 5] = [
    "build_failure",
    "check_output",
    "counterexample",
    "manual_citation",
    "reasoned",
];

/// [`EVIDENCE_KINDS`] as the descriptor field wants it.
pub fn default_evidence_kinds() -> Vec<String> {
    EVIDENCE_KINDS.iter().map(|s| (*s).to_string()).collect()
}
