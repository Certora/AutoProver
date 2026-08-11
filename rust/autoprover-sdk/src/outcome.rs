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

/// One check the **author declared**: the backend's name for a runnable verification — a CVL rule, a
/// foundry test, a tagged fuzz assertion. A check yields a [`Verdict`] and becomes one row of the
/// report.
///
/// `properties` is what the author declared this check verifies — the mapping's own claim, carried
/// verbatim rather than guessed by the wheel, and never empty (a check exists *because* something
/// claimed it). Usually one title; several when one rule discharges several related invariants. It
/// is here because some checkers speak in properties rather than in check names — Crucible tags each
/// assertion message with its property title, so this is what lets it place a counterexample —
/// while a backend whose checker reports per check (the Prover, forge) can ignore it.
///
/// `target` names the [`Target`] this check runs under — **one invocation of the checker**. Several
/// checks may share one (e.g. Crucible puts a component's whole property set in a single fuzz
/// target), so the host runs it once and the backend attributes the outcome back to each check.
/// `None` ⇒ the check is its own target (one invocation per check, the default), and is what
/// [`Backend::target_for`](crate::Backend::target_for) answers by default.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct Check {
    pub name: String,
    pub properties: Vec<String>,
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

/// How much of its budget one invocation must spend before it may conclude — the difference between
/// a run whose clean checks mean something and one that only got as far as the first problem.
///
/// A checker that can stop early is cheaper when the author is iterating and wrong when the verdicts
/// are going to be reported: a check that a run abandoned before exploring it is not a check that
/// held. The host knows which kind of run it is asking for (only a full run stamps the publish gate),
/// so it says, rather than leaving each backend to guess from the shape of its check set.
///
/// A backend with nothing to spend — a typechecker, a backend whose run is exhaustive by
/// construction — answers the same either way and can ignore this.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
pub enum Exploration {
    /// Stop as soon as there is something to report. The checks the run did not refute were not
    /// explored to budget, so their `GOOD` says "not refuted yet" and nothing more — fine for an
    /// author iterating on one problem, and never enough to report.
    UntilFirstFinding,
    /// Explore every covered check to the full budget, whatever is found on the way. The default,
    /// because a run that quietly stops short is the failure mode worth defaulting away from.
    #[default]
    ToBudget,
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
/// The host computes the grouping from the checks the author declared, each targeted by
/// [`Backend::target_for`](crate::Backend::target_for) (it decides what to run, and in what order),
/// so it hands the answer over rather than leaving each backend to recover it by filtering a set it
/// would have to rebuild. A target name is only meaningful within its session: two units naming the
/// same target each run their own.
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
    /// How far this invocation must explore before concluding. Set by the host from what it will do
    /// with the answer — a partial run it lets the author iterate against may stop early; the run
    /// that stamps the publish gate may not.
    pub exploration: Exploration,
}

impl Target {
    /// The same verdict for every covered check — what a run that concluded one thing about the
    /// whole target produces (it errored; it timed out).
    ///
    /// Note what is NOT such a conclusion: a run that *finished cleanly*. "Nothing was refuted" is
    /// one fact about the target, but [`Outcome::Good`] per check is a claim about each check
    /// individually, and a check the run never exercised has not been shown to hold — see the
    /// verdict contract on [`Backend::validate`](crate::Backend::validate). Reach for
    /// [`Target::verdicts`] there.
    pub fn all(&self, outcome: Outcome, detail: Option<String>) -> ValidateOutcome {
        self.verdicts(|_| Verdict::with_outcome(outcome).with_detail(detail.clone()))
    }

    /// A verdict per covered check, keyed by check name so a backend never spells one itself.
    pub fn verdicts(&self, mut of: impl FnMut(&Check) -> Verdict) -> ValidateOutcome {
        ValidateOutcome::Verdicts {
            verdicts: CheckVerdicts(
                self.checks
                    .iter()
                    .map(|c| (c.name.clone(), of(c)))
                    .collect(),
            ),
        }
    }
}

/// What a target's run concluded, as `(check_name, verdict)` for every check the target covers.
///
/// The payload is private, so the only way a backend builds one is [`Target::all`] or
/// [`Target::verdicts`] — both of which take the names from the target's own checks. A backend
/// therefore attributes its run to the checks it was handed instead of naming them: it can neither
/// invent a check the target does not cover nor leave one of them without a verdict, and the host
/// resolves each name back to the [`Check`] it sent.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(transparent)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
pub struct CheckVerdicts(Vec<(String, Verdict)>);

impl CheckVerdicts {
    /// Each covered check's name and what it concluded.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Verdict)> {
        self.0
            .iter()
            .map(|(name, verdict)| (name.as_str(), verdict))
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
    Verdicts { verdicts: CheckVerdicts },
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
        Verdict {
            outcome,
            line: None,
            duration_seconds: None,
            unit_file: None,
            detail: None,
        }
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
