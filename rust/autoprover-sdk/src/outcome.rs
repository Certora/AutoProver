//! What the gating callouts hand back: the compile verdict, the report's property→unit map,
//! and the per-unit validation outcomes.

use serde::{Deserialize, Serialize};

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

/// The result of `validate` — the fused build+check for one validation **target**. Either the
/// build failed (so the whole spec must be re-authored — the build is shared), or it built and
/// produced a `Verdict` **per report unit the target covers** (`(unit, verdict)`). A target may
/// cover several units (e.g. Crucible runs every invariant in one target), and the backend — which
/// owns its own result/failure format — attributes the run to those units; the host records the
/// verdicts verbatim (it does no verdict logic). The build gate is fused in here rather than run as
/// a separate `compile` dry-run, so a component pays for one build (docs/rust-applications.md §4.4).
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
/// a typo fails to compile here instead of reaching a report row as an unexplained `UNKNOWN`.
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
