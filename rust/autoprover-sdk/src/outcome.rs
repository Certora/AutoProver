//! What the gating callouts hand back: the compile verdict, the report's property→check map,
//! and the per-check validation outcomes.

use serde::{Deserialize, Serialize};

/// The outcome of `compile`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub enum CompileResult {
    Ok,
    Failed { errors: String },
}

/// A property the author declined to formalize, with its justification. Carried into
/// [`Delivered`](crate::finalize::Delivered) so the deliverable and the report can both say what was
/// left out and why — a property that is absent for a stated reason is a different thing from one
/// that was silently dropped.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct SkippedProperty {
    pub property_title: String,
    pub reason: String,
}

/// One check: a property title and the backend's name for the thing that verifies it — a CVL rule,
/// a foundry test, a fuzz harness function. A check yields a [`Verdict`] and becomes one row of the
/// report.
///
/// `target` names the [`Target`] this check runs under — **one invocation of the checker**. Several
/// checks may share one (e.g. Crucible puts a component's whole property set in a single fuzz
/// target), so the host runs it once and the backend attributes the outcome back to each check.
/// `None` ⇒ the check is its own target (one invocation per check, the default).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct Check {
    pub property: String,
    pub name: String,
    #[serde(deserialize_with = "crate::required::present")]
    pub target: Option<String>,
}

impl Check {
    /// The validation target this check runs under — its own `name` unless it shares a target with
    /// others.
    pub fn target_or_name(&self) -> &str {
        self.target.as_deref().unwrap_or(&self.name)
    }
}

/// **One invocation of the checker** — one build + one run — and the checks that invocation covers,
/// which are the checks [`Backend::validate`](crate::Backend::validate) must return a verdict for.
///
/// Targets are how *running* is grouped; [`Check`]s are how *reporting* is. A target always sits
/// inside a single unit's session (the [`AuthorInput::unit`](crate::authoring::AuthorInput::unit)
/// being formalized), so the three nest: one unit's checks partition into its targets. That is the
/// point of the split — a backend that fuzzes a whole property set in one campaign pays for one
/// build and one run, and still reports a row per property.
///
/// The host computes the grouping from [`Backend::checks`](crate::Backend::checks) (it decides what
/// to run, and in what order), so it hands the answer over rather than leaving each backend to
/// recover it by re-deriving its own checks and filtering them by name. A target name is only
/// meaningful within its session: two units naming the same target each run their own.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct Target {
    /// The target's name — what the backend selects when it invokes the checker (Crucible: the
    /// unit's harness fn, which is also its Cargo feature).
    pub name: String,
    /// The checks this run must produce a verdict for. Usually one; several when a backend checks
    /// a whole property set in one run.
    pub checks: Vec<Check>,
}

impl Target {
    /// The same verdict for every covered check — what a run that concluded one thing about the
    /// whole target produces (it passed; it errored; it timed out).
    pub fn all(&self, outcome: Outcome, detail: Option<String>) -> ValidateOutcome {
        self.verdicts(|_| Verdict::with_outcome(outcome).with_detail(detail.clone()))
    }

    /// A verdict per covered check, keyed by check name so a backend never spells one itself.
    pub fn verdicts(&self, mut of: impl FnMut(&Check) -> Verdict) -> ValidateOutcome {
        ValidateOutcome::Verdicts {
            verdicts: self.checks.iter().map(|c| (c.name.clone(), of(c))).collect(),
        }
    }
}

/// The result of `validate` — the fused build+check for one validation **target**. Either the
/// build failed (so the whole spec must be re-authored — the build is shared), or it built and
/// produced a `Verdict` **per check the target covers** (`(check_name, verdict)`). A target may
/// cover several checks (e.g. Crucible runs every invariant in one target), and the backend — which
/// owns its own result/failure format — attributes the run to those checks; the host records the
/// verdicts verbatim (it does no verdict logic). The build gate is fused in here rather than run as
/// a separate `compile` dry-run, so a component pays for one build (docs/rust-applications.md §4.4).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub enum ValidateOutcome {
    BuildFailed { errors: String },
    Verdicts { verdicts: Vec<(String, Verdict)> },
}

/// What one check concluded — the report's backend-agnostic vocabulary (mirrors
/// `composer…report.schema.Outcome`), which every backend's native status maps into. The
/// human-facing wording ("No counterexample" vs "Verified") is picked at render time from the
/// application's `backend_tag`, so a backend never spells it out here.
///
/// An enum rather than a free string: the host validates this field against the same closed set, so
/// a typo fails to compile here instead of reaching a report row as an unexplained `UNKNOWN`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
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

/// One check's outcome (mirrors `composer…report.collect.Verdict`).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Verdict {
    pub outcome: Outcome,
    #[serde(deserialize_with = "crate::required::present")]
    pub line: Option<u32>,
    #[serde(deserialize_with = "crate::required::present")]
    pub duration_seconds: Option<f64>,
    #[serde(deserialize_with = "crate::required::present")]
    pub unit_file: Option<String>,
    /// Human-readable explanation of a non-GOOD outcome — the failure detail (a counterexample /
    /// assertion message) for a `BAD`, or the error text for an `ERROR`. Surfaced live and persisted
    /// to the report so a verdict is self-explaining (otherwise a bare `BAD` gives no clue why).
    #[serde(deserialize_with = "crate::required::present")]
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
        Verdict::with_outcome(outcome).with_detail(Some(detail.into()))
    }

    /// This verdict with `detail` when there is one — for a caller holding the `Option` a parsed
    /// tool output gives it, rather than one deciding between two constructors.
    pub fn with_detail(self, detail: Option<String>) -> Self {
        Verdict { detail, ..self }
    }
}
