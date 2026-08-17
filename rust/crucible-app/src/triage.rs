//! What a fuzz finding means: the reproducing sequence behind a crash, whether the metadata alone
//! shows it to be a *harness* defect, and which of a shared target's rows it actually refutes.
//!
//! This is the backend's own attribution — the host never parses a finding.

use std::path::{Path, PathBuf};

use autoprover_sdk::authoring::Property;
use autoprover_sdk::outcome::{Check, Exploration, Outcome, Target, ValidateOutcome, Verdict};

use crate::layout::{harness_dir, CRASHES_DIR};

/// One action in a crash's reproducing sequence, as Crucible records it in
/// `crash_<id>.meta.json`.
///
/// The two fields answer different questions, and conflating them is what makes a sequence
/// unreadable. `success` is the **action's own report** — Crucible records the `-> bool` the
/// `action_*` returned ("did this action do what it was designed to do"). `error_code` is the
/// **transaction's** result, present when the last instruction the action sent errored.
///
/// For a positive action the two coincide. For a *negative* one — an action whose purpose is an
/// attempt the program must reject — they deliberately diverge: it returns `true` because making
/// the attempt is it working (see `author_setup.j2`), while the transaction is expected to error.
#[derive(serde::Deserialize)]
struct CrashAction {
    name: String,
    #[serde(default)]
    success: bool,
    #[serde(default)]
    error_code: Option<i64>,
}

impl std::fmt::Display for CrashAction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // An error code beside a successful action is NOT an upstream inconsistency (this once
        // suppressed it as one, on evidence from the klend run) — it is a negative action
        // reporting that it ran while its transaction was correctly rejected. Suppressing it made
        // the sequence contradict the verdict table: the 2026-08-07 rerun rendered
        // `reinitialize -> OK` beside a GOOD verdict for the very property that says re-init must
        // fail. This is the only field in the record that separates "the attempt was rejected"
        // from "the attempt went through", which for a negative action IS the finding. Never hide
        // it.
        match (self.success, self.error_code) {
            (true, Some(c)) => write!(f, "{} -> OK (tx rejected: {c})", self.name),
            (true, None) => write!(f, "{} -> OK", self.name),
            (false, Some(c)) => write!(f, "{} -> FAIL({c})", self.name),
            (false, None) => write!(f, "{} -> FAIL", self.name),
        }
    }
}

/// The reproducing sequence Crucible writes next to a crash payload.
///
/// `iteration` is the fuzzer's **global test-case counter** — which input this was, not how much
/// ran within it. It is reported for reference and is NOT a smell: `0` is simply the first test
/// case, which carries a full action sequence like any other, and Crucible resets it to `0`
/// outright when replaying a saved crash. Reading it as "nothing had been mutated yet" labelled a
/// genuine one-action finding a suspected harness bug on the 2026-08-07 rerun; what the sequence
/// ran is in `actions`, and that is what [`CrashRepro::suspicion`] reads.
#[derive(serde::Deserialize)]
struct CrashRepro {
    #[serde(default)]
    iteration: u64,
    #[serde(default)]
    actions: Vec<CrashAction>,
}

/// Why a counterexample looks like a *harness* bug rather than a program bug — the two smells
/// that are decidable from the crash metadata alone. Absent when the violation followed a
/// successful state transition, i.e. when the finding looks genuine and deserves a human.
///
/// An enum rather than a bool-plus-string: each variant carries exactly the evidence that
/// justifies it, so a reader gets the specific smell instead of an unexplained "suspect".
enum HarnessSuspicion {
    /// Fired on a sequence where NO action succeeded — an empty one, or one whose every step
    /// failed. Nothing moved the chain off the fixture's post-setup state, so that state itself
    /// violates the invariant and no program behaviour is implicated.
    InitialState,
    /// Fired on an action that did not succeed. A property scoped to "after a successful X"
    /// cannot be refuted by an X that reverted; the assertion is running outside its
    /// precondition (or over state the failed action never created).
    ViolatingActionFailed(String),
}

impl std::fmt::Display for HarnessSuspicion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InitialState => write!(
                f,
                "not one action in the sequence succeeded, so nothing moved the chain off the \
                 fixture's post-setup state — that state already violates this invariant, and no \
                 program behaviour is implicated. Assert only after the action the property is \
                 scoped to, or exclude the fixture state the property does not cover."
            ),
            Self::ViolatingActionFailed(a) => write!(
                f,
                "the action it fired on did not succeed ({a}) — the state transition this \
                 property is scoped to never happened, so the assertion is running outside its \
                 precondition (or over state that action failed to create). Gate the assertion \
                 on the action succeeding, and skip uninitialized accounts."
            ),
        }
    }
}

/// Where Crucible may have left `crash_<id>.meta.json`, relative to the run's workdir: its
/// default flat output dir, then the per-test layout `tmin`/`show` use.
fn crash_meta_paths(workdir: &Path, program: &str, unit: &str, crash_id: &str) -> Vec<PathBuf> {
    let file = format!("{crash_id}.meta.json");
    vec![
        workdir.join(CRASHES_DIR).join(&file),
        workdir.join(harness_dir(program)).join("crashes").join(unit).join(&file),
    ]
}

impl CrashRepro {
    /// Read the sequence recorded for `crash_id`. Best-effort by design — a finding must still
    /// report when the metadata is missing or in a shape we don't know; it just reports without
    /// its sequence rather than failing the run.
    fn read(workdir: &Path, program: &str, unit: &str, crash_id: &str) -> Option<Self> {
        crash_meta_paths(workdir, program, unit, crash_id)
            .iter()
            .find_map(|p| std::fs::read_to_string(p).ok())
            .and_then(|s| serde_json::from_str(&s).ok())
    }

    /// The smell, if the metadata shows one — read off the action sequence, which is the only
    /// part of the metadata that says what actually ran.
    ///
    /// **No action succeeded** (including an empty sequence) is the initial-state smell: nothing
    /// moved the chain off the fixture's post-setup state, so the invariant was already false
    /// there and no program behaviour is implicated. Checked first because it is the stronger
    /// statement — it says the whole sequence was inert, not just its last step.
    ///
    /// Otherwise the violation fired on the LAST executed action (Crucible stops the sequence
    /// there), and an action that did not do its job cannot have produced the transition the
    /// property is scoped to.
    fn suspicion(&self) -> Option<HarnessSuspicion> {
        if !self.actions.iter().any(|a| a.success) {
            return Some(HarnessSuspicion::InitialState);
        }
        match self.actions.last() {
            Some(a) if !a.success => Some(HarnessSuspicion::ViolatingActionFailed(a.to_string())),
            _ => None,
        }
    }

    /// The sequence as report text. Kept off the first line so the live console view stays
    /// one line per verdict (see `composer.rustapp.adapter`).
    fn render_sequence(&self) -> String {
        let head = format!(
            "reproducing sequence (iteration {}, {} action(s)):",
            self.iteration,
            self.actions.len()
        );
        let body = self
            .actions
            .iter()
            .enumerate()
            .map(|(i, a)| format!("  {}. {a}", i + 1))
            .collect::<Vec<_>>()
            .join("\n");
        if body.is_empty() { head } else { format!("{head}\n{body}") }
    }
}

/// Pull the human-readable findings out of a `crucible run` log so a `BAD` verdict explains itself,
/// each enriched with its crash's reproducing sequence and any harness-bug smell. Crucible prints
/// `[FUZZ_FINDING] crash:<id> reproduces:<bool> summary:<msg>` — one line per NOVEL crash, where
/// `<msg>` is the failed `fuzz_assert_*` message.
///
/// Every line, not just the first: unless it is told to stop at the first crash, the campaign
/// records each novel crash and keeps fuzzing, so a component's run reports as many findings as it
/// found. Reading only the first is what let a klend campaign that refuted two properties report one
/// of them and mark the other "no counterexample" (docs/rust-applications.md).
///
/// Each finding's **first line** is the summary plus, when present, a `SUSPECT HARNESS BUG` marker
/// — the signal that decides whether a human should look at all. The sequence follows on later
/// lines. Both reach the report as the rule's `message`; the console shows the first line.
pub(crate) fn findings(out: &str, workdir: &Path, program: &str, unit: &str) -> Vec<String> {
    out.lines()
        .filter(|l| l.contains("[FUZZ_FINDING]"))
        .filter_map(|l| finding_detail(l.trim(), workdir, program, unit))
        .collect()
}

/// One `[FUZZ_FINDING]` line as report text, or `None` when the line carries no summary to report.
fn finding_detail(line: &str, workdir: &Path, program: &str, unit: &str) -> Option<String> {
    let Some((head, summary)) = line.split_once("summary:") else {
        return Some(line.to_string());
    };
    if summary.trim().is_empty() {
        return Some(line.to_string());
    }
    let crash = head.split_whitespace().find_map(|t| t.strip_prefix("crash:")).unwrap_or("?");
    let mut detail = format!("crash {crash}: {}", summary.trim());

    let Some(repro) = CrashRepro::read(workdir, program, unit, crash) else {
        return Some(detail);
    };
    if let Some(smell) = repro.suspicion() {
        detail.push_str(&format!(" — SUSPECT HARNESS BUG (likely not a program bug): {smell}"));
    }
    detail.push('\n');
    detail.push_str(&repro.render_sequence());
    Some(detail)
}

/// Why this backend cannot answer for a check the author mapped several properties onto — `None`
/// when every covered check claims exactly one, which is the only shape it can attribute.
///
/// A campaign places a counterexample by the `[<title>]` tag in the assertion message, so a check
/// claiming several properties is refuted by a finding naming *any* of them: the whole set would go
/// `BAD` together on evidence about one. The seam permits many-to-one (a CVL rule can genuinely
/// discharge three invariants) — Crucible cannot, because its unit of evidence is one tagged
/// assertion, and this is where it says so rather than reporting a verdict it cannot stand behind.
pub(crate) fn undeclarable(target: &Target) -> Option<String> {
    let shared: Vec<String> = target
        .checks
        .iter()
        .filter(|c| c.properties.len() > 1)
        .map(|c| format!("`{}` claims {}", c.name, c.properties.join(", ")))
        .collect();
    if shared.is_empty() {
        return None;
    }
    Some(format!(
        "an invariant cannot cover more than one property here: {}. A counterexample is placed by \
         the `[<title>]` tag in its assertion message, so one invariant covering several properties \
         would fail them all together on evidence about one of them. Declare exactly one invariant \
         per property, named `c_<property title>` after the tag it asserts, and map each property \
         to that one alone.",
        shared.join("; "),
    ))
}

/// The run property whose title the finding names but this target does not check — the owner of a
/// foreign assertion. Longest title wins, so one title being a prefix of another resolves to the
/// more specific of the two.
fn foreign_owner<'a>(target: &Target, run_props: &'a [Property], detail: &str) -> Option<&'a Property> {
    let mine = |title: &str| target.checks.iter().any(|c| c.properties.iter().any(|t| t == title));
    run_props
        .iter()
        .filter(|p| !p.title.is_empty() && detail.contains(&p.title) && !mine(&p.title))
        .max_by_key(|p| p.title.len())
}

/// Why a check the campaign did not refute still has no verdict — only reachable on a run the host
/// allowed to stop early, since one that explored to budget has actually checked it.
fn unexplored(target: &Target, run_props: &[Property], stopper: &str) -> String {
    match foreign_owner(target, run_props, stopper) {
        Some(p) => format!(
            "campaign stopped on a violation of {}'s property `{}`, which this component does not \
             check — this one was not explored to budget.\n{stopper}",
            p.component, p.title,
        ),
        None => format!(
            "campaign stopped at its first finding, so this check was not explored to \
             budget.\n{stopper}",
        ),
    }
}

/// Attribute a shared-target campaign's counterexamples across the rows the target covers. Crucible
/// tags each assertion message with its property title (`[<title>]`), so a finding names the
/// invariant it refutes — which is why a [`Check`] carries the properties its author claimed for it,
/// rather than this having to place a finding by a name the author chose. There are three things
/// that title can be. This is the backend's own
/// attribution — the host never parses a finding.
///
/// **One of this target's own properties.** That row is `BAD`, carrying the findings that name it —
/// several, when a campaign refuted one property along more than one action sequence.
///
/// **Another component's.** The shared fixture is built into every component's target, so an
/// assertion it carries fires in campaigns that never asked about it — and its title is one this
/// target has no row for. Refuting all of them would be a false report: the counterexample says
/// nothing about them, and the component that *does* own the title reports it from its own campaign.
///
/// **Unknown to the whole run.** No one can place it, so mark them all `BAD` rather than silently
/// pass a real counterexample. This is the case [`AuthorInput::run_props`](autoprover_sdk::authoring::AuthorInput)
/// exists to separate out from the one above; a wheel run without it degrades to exactly this.
///
/// What a check no finding names gets is [`Target::exploration`]'s to say, and it is the whole point
/// of that field. A campaign that ran [`ToBudget`](Exploration::ToBudget) explored every check it
/// covers whatever it found on the way, so an unnamed check held and is `GOOD`. One the host let
/// stop [`UntilFirstFinding`](Exploration::UntilFirstFinding) abandoned the rest wherever it
/// happened to be, so `GOOD` there would be a claim about a space nothing searched — `UNKNOWN`, and
/// the detail says which finding ended it.
///
/// "Explored every check it covers" presumes the check's assertion was *evaluated* at all — which
/// this attribution cannot see, and a guarded assertion whose guard never opens makes false. That
/// premise is [`tally::gate`](crate::tally::gate)'s to collect, applied over this outcome in
/// `validate` (docs/crucible.md §8).
pub(crate) fn attribute_findings(
    target: &Target, run_props: &[Property], findings: &[String],
) -> ValidateOutcome {
    // By the properties the author claimed for the check, not by its name: the fixture tags each
    // assertion with a property *title*, so that is what a finding names. A check claiming several
    // properties is refuted by a finding naming any of them.
    let refutes = |c: &Check, d: &String| {
        c.properties.iter().any(|t| !t.is_empty() && d.contains(t))
    };
    let is_mine = |d: &String| target.checks.iter().any(|c| refutes(c, d));

    // A finding no one in the run owns could be refuting anything this target covers, so it
    // condemns all of it. Checked over every finding, not just the first: a campaign that reported
    // one placeable finding and one unplaceable one must not have the second silently dropped.
    let unplaceable: Vec<&str> = findings
        .iter()
        .filter(|d| !is_mine(d) && foreign_owner(target, run_props, d).is_none())
        .map(String::as_str)
        .collect();
    if !unplaceable.is_empty() {
        return target.all(Outcome::Bad, Some(unplaceable.join("\n\n")));
    }

    target.verdicts(|c| {
        let against: Vec<&str> =
            findings.iter().filter(|d| refutes(c, d)).map(String::as_str).collect();
        match (against.is_empty(), target.exploration) {
            (false, _) => Verdict::detailed(Outcome::Bad, against.join("\n\n")),
            (true, Exploration::ToBudget) => Verdict::with_outcome(Outcome::Good),
            (true, Exploration::UntilFirstFinding) => match findings.first() {
                Some(stopper) => {
                    Verdict::detailed(Outcome::Unknown, unexplored(target, run_props, stopper))
                }
                None => Verdict::with_outcome(Outcome::Good),
            },
        }
    })
}

#[cfg(test)]
mod attribution {
    //! Attributing a shared-target finding (docs/crucible.md §8): one
    //! campaign covers a component's whole property set, and the fixture's assertions carry titles
    //! from components that campaign never asked about.
    use super::*;
    use crate::testkit::{iterating_target, owned, prop, target_over, verdicts_of};

    #[test]
    fn one_invariant_per_property_is_the_only_shape_this_backend_can_attribute() {
        // The 2026-08-11 vault run: the author mapped all ten of a component's properties onto one
        // check, so the two properties a campaign actually refuted took the other eight down with
        // them. The seam allows many-to-one; this backend's evidence is one tagged assertion, so it
        // refuses rather than reporting a verdict it cannot stand behind — and refuses BEFORE the
        // campaign, since those verdicts would be unattributable however long it ran.
        let props = [prop("fifo", "fifo"), prop("solvency", "solvency")];
        assert!(undeclarable(&target_over("c_queue", &props)).is_none(), "one each is fine");

        let mut shared = target_over("c_queue", &props);
        shared.checks = vec![Check {
            name: "c_all".into(),
            properties: props.iter().map(|p| p.title.clone()).collect(),
            target: Some("c_queue".into()),
        }];
        let why = undeclarable(&shared).expect("refused");
        assert!(why.contains("c_all") && why.contains("fifo") && why.contains("solvency"), "{why}");
        // Actionable: it says what to do instead, in the noun the prompt used.
        assert!(why.contains("one invariant per property"), "{why}");
    }

    /// The whole run's properties: two components, and the fixture's negative actions tagged with
    /// titles from both — the shape the 2026-08-07 e2e run had.
    fn two_component_run() -> Vec<Property> {
        vec![
            owned("Vault Initialization", "authority_must_sign_initialization", "auth_signs"),
            owned("Lamport Management", "deposit_overflow_prevented", "no_overflow"),
            owned("Lamport Management", "withdrawal_authority_only", "wd_auth"),
        ]
    }

    /// Lamport Management's target: its two properties in one campaign, explored to budget.
    fn lamport_target() -> (Vec<Property>, Target) {
        let run = two_component_run();
        let target = target_over("c_lamport_management", &run[1..]);
        (run, target)
    }

    fn found(details: &[&str]) -> Vec<String> {
        details.iter().map(|d| (*d).to_string()).collect()
    }

    #[test]
    fn a_finding_naming_this_targets_own_property_refutes_only_that_one() {
        let (run, target) = lamport_target();
        let detail = "crash abc: [deposit_overflow_prevented] total=2 exceeds cap=1";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[detail])));

        assert_eq!(got[0].0, "c_no_overflow");
        assert_eq!(got[0].1, Outcome::Bad);
        assert_eq!(got[0].2, detail, "the refuted row carries the counterexample");
        // …and the rest held, which here is a real claim: the campaign ran to budget, so it went on
        // exploring this one after recording the crash above.
        assert_eq!(got[1].1, Outcome::Good);
    }

    #[test]
    fn every_finding_a_campaign_reports_lands_on_the_property_it_names() {
        // The klend regression. A campaign that does not stop at the first crash reports one
        // `[FUZZ_FINDING]` per novel crash, and reading only the first left the second property
        // reported as "No counterexample" — while the author's own commentary carried its
        // reproducing sequence.
        let (run, target) = lamport_target();
        let overflow = "crash abc: [deposit_overflow_prevented] total=2 exceeds cap=1";
        let auth = "crash def: [withdrawal_authority_only] withdrew as 11111 (owner 22222)";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[overflow, auth])));

        assert_eq!(got[0].1, Outcome::Bad);
        assert_eq!(got[0].2, overflow);
        assert_eq!(got[1].1, Outcome::Bad, "the second finding must not be dropped");
        assert_eq!(got[1].2, auth, "and each row carries its OWN counterexample");
    }

    #[test]
    fn several_findings_against_one_property_all_reach_its_row() {
        // One property can be refuted along more than one action sequence. Each is a distinct way
        // in and worth triaging, so the row carries them all rather than whichever came first.
        let (run, target) = lamport_target();
        let one = "crash abc: [deposit_overflow_prevented] total=2 exceeds cap=1";
        let two = "crash def: [deposit_overflow_prevented] total=9 exceeds cap=8";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[one, two])));

        assert_eq!(got[0].1, Outcome::Bad);
        assert!(got[0].2.contains(one) && got[0].2.contains(two), "{}", got[0].2);
        assert_eq!(got[1].1, Outcome::Good);
    }

    #[test]
    fn a_foreign_finding_leaves_a_target_that_ran_to_budget_alone() {
        // The shared fixture is in EVERY component's build, so an assertion it carries fires in
        // campaigns that never asked about it. It cost this campaign one test case and nothing
        // else — it explored every check it covers regardless — so these rows are the campaign's
        // own answer, and the component that owns the title reports it from its own run.
        let (run, target) = lamport_target();
        let detail = "crash def: [authority_must_sign_initialization] init without the authority \
                      signing must fail";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[detail])));

        assert!(got.iter().all(|(_, o, _)| *o == Outcome::Good), "{got:?}");
    }

    #[test]
    fn a_run_that_stopped_early_leaves_the_checks_it_abandoned_undetermined() {
        // The same finding on a run the host let stop at the first crash. Now the campaign really
        // did end there, so GOOD would be a claim about a space nothing searched.
        let run = two_component_run();
        let target = iterating_target("c_lamport_management", &run[1..]);
        let detail = "crash def: [authority_must_sign_initialization] init must fail";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[detail])));

        assert!(got.iter().all(|(_, o, _)| *o == Outcome::Unknown), "{got:?}");
        // An unexplained UNKNOWN is no better than a wrong BAD: the row has to say what ended it.
        let said = &got[0].2;
        assert!(said.contains("Vault Initialization"), "{said}");
        assert!(said.contains("authority_must_sign_initialization"), "{said}");
        assert!(said.contains(detail), "the finding itself is still carried:\n{said}");
    }

    #[test]
    fn a_run_that_stopped_on_its_own_finding_still_reports_that_one() {
        // Stopping early costs the *other* checks their verdict, not the one that was refuted.
        let run = two_component_run();
        let target = iterating_target("c_lamport_management", &run[1..]);
        let detail = "crash abc: [deposit_overflow_prevented] total=2 exceeds cap=1";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[detail])));

        assert_eq!(got[0].1, Outcome::Bad);
        assert_eq!(got[0].2, detail);
        assert_eq!(got[1].1, Outcome::Unknown);
        assert!(got[1].2.contains("stopped at its first finding"), "{}", got[1].2);
    }

    #[test]
    fn a_finding_no_one_in_the_run_owns_still_condemns_the_whole_target() {
        // The safety net, unchanged: an unplaceable counterexample is real until shown otherwise,
        // and silently passing it is the one failure this must never have.
        let (run, target) = lamport_target();
        let detail = "crash 999: assertion failed at fixture.rs:42";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[detail])));

        assert!(got.iter().all(|(_, o, d)| *o == Outcome::Bad && d == detail), "{got:?}");
    }

    #[test]
    fn an_unplaceable_finding_condemns_the_target_even_beside_a_placeable_one() {
        // Reading findings as a set is what makes this reachable at all, and the safety net has to
        // survive it: a campaign that reported one attributable crash and one nobody can place
        // must not have the second quietly absorbed by the first one's row.
        let (run, target) = lamport_target();
        let mine = "crash abc: [deposit_overflow_prevented] total=2 exceeds cap=1";
        let orphan = "crash 999: assertion failed at fixture.rs:42";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[mine, orphan])));

        assert!(got.iter().all(|(_, o, _)| *o == Outcome::Bad), "{got:?}");
        assert!(got.iter().all(|(_, _, d)| d == orphan), "the unplaceable one is what says why");
    }

    #[test]
    fn without_the_runs_properties_attribution_degrades_to_the_safety_net() {
        // A wheel driven by a host that declares no setup step gets an empty `run_props`, so a
        // foreign title is indistinguishable from an unknown one. That must read as the old
        // behaviour, not as a panic or a silent pass.
        let (_, target) = lamport_target();
        let detail = "crash def: [authority_must_sign_initialization] init must fail";
        let got = verdicts_of(&attribute_findings(&target, &[], &found(&[detail])));

        assert!(got.iter().all(|(_, o, _)| *o == Outcome::Bad), "{got:?}");
    }

    #[test]
    fn the_more_specific_of_two_overlapping_titles_names_the_owner() {
        // Titles are free text, so one can be a prefix of another. Matching is by substring — the
        // finding is a rendered message, not a structured field — so the longer match wins. Read
        // through a run that stopped early, which is the case whose wording names the owner.
        let run = vec![
            owned("Deposits", "deposit_bounded", "bounded"),
            owned("Overflow Guards", "deposit_bounded_by_reserve_cap", "bounded_cap"),
        ];
        let target = iterating_target("c_withdrawals", &[owned("Withdrawals", "fifo", "fifo")]);
        let detail = "crash 1: [deposit_bounded_by_reserve_cap] total=9 cap=8";
        let got = verdicts_of(&attribute_findings(&target, &run, &found(&[detail])));

        assert_eq!(got[0].1, Outcome::Unknown);
        assert!(got[0].2.contains("Overflow Guards"), "{}", got[0].2);
    }
}

#[cfg(test)]
mod crash_triage {
    //! A `BAD` verdict must carry enough to triage it without rebuilding the harness: the
    //! reproducing sequence, and a flag when the metadata alone shows the counterexample is a
    //! harness defect rather than a program bug.
    //!
    //! The two `meta.json` bodies below are the REAL ones from the klend run of 2026-08-03, where
    //! both counterexamples turned out to be harness bugs and manual triage cost an hour. They are
    //! the regression cases.
    use super::*;

    /// `crash_ad707f0d6fef8cca` — `deposit_limit_not_exceeded`. Its one action FAILED, so nothing
    /// moved the chain: the fixture seeded 1000 liquidity into reserves left at `deposit_limit =
    /// 0`, and the invariant was false in the post-setup state before the fuzzer did anything.
    const KLEND_INITIAL_STATE: &str = r#"{
        "test_name": "c_liquidity_supply_ctoken_exchange",
        "iteration": 0,
        "seed": 1785812846177550995,
        "actions": [
            {"name": "mark_deleveraging_unauthorized", "params": {}, "success": false,
             "error_code": 3007}
        ]
    }"#;

    /// `crash_492173e388336c5d` — `reserve_lending_market_immutable`, after `tmin`. The violating
    /// `clone_reserve_config` FAILED, so reserve C was never initialized and its zeroed
    /// `lending_market` (all-zero pubkey) was compared against the real market key.
    const KLEND_ACTION_FAILED: &str = r#"{
        "test_name": "c_reserve_lifecycle_configuration",
        "iteration": 4211,
        "actions": [
            {"name": "update_lending_market_owner", "params": {}, "success": true},
            {"name": "clone_reserve_config", "params": {}, "success": false, "error_code": 3002}
        ]
    }"#;

    fn repro(json: &str) -> CrashRepro {
        serde_json::from_str(json).expect("parse crash meta")
    }

    /// A workdir with `output/<crash>.meta.json` in it. Named per-test so cases can't collide.
    fn workdir_with_meta(tag: &str, crash_id: &str, meta: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("crucible_triage_{tag}"));
        let out = dir.join(CRASHES_DIR);
        std::fs::create_dir_all(&out).expect("mkdir");
        std::fs::write(out.join(format!("{crash_id}.meta.json")), meta).expect("write meta");
        dir
    }

    fn finding_line(crash_id: &str, summary: &str) -> String {
        format!("[FUZZ_FINDING] crash:{crash_id} reproduces:true summary:{summary}")
    }

    /// The one finding `out` reports, read through the entry point the backend actually calls.
    fn only_finding(out: &str, workdir: &Path, program: &str, unit: &str) -> String {
        let found = findings(out, workdir, program, unit);
        assert_eq!(found.len(), 1, "expected exactly one finding, got {found:#?}");
        found.into_iter().next().expect("one finding")
    }

    #[test]
    fn a_sequence_where_nothing_succeeded_is_the_fixtures_own_state_not_a_program_bug() {
        // klend's case: one action, and it failed. Nothing moved the chain off the post-setup
        // state, so that state is what violates the invariant.
        let s = repro(KLEND_INITIAL_STATE).suspicion();
        assert!(matches!(s, Some(HarnessSuspicion::InitialState)), "expected InitialState");
    }

    #[test]
    fn a_first_test_case_that_actually_ran_is_a_finding_not_an_initial_state_smell() {
        // This smell used to key on `iteration == 0`. But Crucible's `iteration` is the fuzzer's
        // GLOBAL test-case counter — not a count of what ran inside this one — and it is reset to
        // 0 outright when replaying a saved crash. The metadata below is real: the 2026-08-07
        // rerun's `crash_752c6358904f0e14`, where the first test case drew a one-action sequence,
        // the action RAN, and the invariant failed after it. A genuine finding, and the old rule
        // would have stamped it a suspected harness bug — the exact inversion this flag exists to
        // avoid.
        let r = repro(r#"{"iteration": 0, "actions": [
            {"name": "init_without_signer", "params": {}, "success": true}
        ]}"#);
        assert!(r.suspicion().is_none(), "a first-iteration finding is still a finding");
    }

    #[test]
    fn a_violation_on_a_failed_action_is_outside_the_propertys_precondition() {
        match repro(KLEND_ACTION_FAILED).suspicion() {
            Some(HarnessSuspicion::ViolatingActionFailed(a)) => {
                // The action string carries the evidence: which action, and its error code.
                assert_eq!(a, "clone_reserve_config -> FAIL(3002)");
            }
            other => panic!("expected ViolatingActionFailed, got {}", other.is_some()),
        }
    }

    #[test]
    fn a_violation_after_a_successful_action_is_left_alone() {
        // The case that must NOT be flagged — a real counterexample deserves a human, and crying
        // "suspect harness" over every finding would make the flag worthless.
        let genuine = r#"{"iteration": 91, "actions": [
            {"name": "deposit_reserve_liquidity", "params": {}, "success": true}
        ]}"#;
        assert!(repro(genuine).suspicion().is_none());
    }

    #[test]
    fn an_empty_sequence_is_the_initial_state_smell_whenever_it_surfaces() {
        // Nothing ran at all — the strongest form of "the post-setup state violates this". It
        // must not panic, and it must not depend on where in the campaign the crash landed.
        for meta in [r#"{"iteration": 0, "actions": []}"#, r#"{"iteration": 4096, "actions": []}"#]
        {
            assert!(
                matches!(repro(meta).suspicion(), Some(HarnessSuspicion::InitialState)),
                "{meta}"
            );
        }
    }

    #[test]
    fn a_rejected_transaction_shows_even_when_the_action_reports_success() {
        // `success` is the ACTION's own report; `error_code` is the TRANSACTION's. A negative
        // action returns `true` while its instruction is correctly rejected, so the two diverge by
        // design — this was read as an upstream inconsistency and suppressed. Suppressing it made
        // the report contradict itself on the 2026-08-07 rerun, which printed
        // `reinitialize -> OK` beside a GOOD verdict for the property saying re-init must fail.
        let meta = r#"{"iteration": 3, "actions": [
            {"name": "reinitialize", "params": {}, "success": true, "error_code": 0},
            {"name": "init_without_signer", "params": {}, "success": true}
        ]}"#;
        let seq = repro(meta).render_sequence();
        assert!(seq.contains("1. reinitialize -> OK (tx rejected: 0)"), "{seq}");
        // …while an attempt that went THROUGH stays bare. For a negative action that difference
        // IS the finding, so the two must not render alike.
        assert!(seq.ends_with("2. init_without_signer -> OK"), "{seq}");
    }

    /// `crash_e953b49e3a4dca3b` — the crash the 2026-08-07 rerun actually reported, verbatim. Two
    /// correctly-rejected re-inits, then an unsigned init that went through. Every action reports
    /// `success: true`, because that field is the action's own `-> bool` and a negative action
    /// returns `true` for making the attempt; the transaction outcomes are in `error_code`.
    const VAULT_UNSIGNED_INIT: &str = r#"{
        "test_name": "c_vault_initialization",
        "iteration": 3,
        "actions": [
            {"name": "reinitialize", "params": {}, "success": true, "error_code": 0},
            {"name": "reinitialize", "params": {}, "success": true, "error_code": 0},
            {"name": "init_without_signer", "params": {}, "success": true}
        ]
    }"#;

    #[test]
    fn the_reported_sequence_distinguishes_a_rejected_attempt_from_one_that_went_through() {
        // The whole verdict rests on this distinction, and the report used to erase it: all three
        // rows printed `-> OK`, so the sequence said re-init succeeded while the table reported
        // GOOD for the property that says re-init must fail.
        let wd = workdir_with_meta("vault", "crash_e953", VAULT_UNSIGNED_INIT);
        let out = finding_line(
            "crash_e953",
            "[authority_must_sign_initialization] initialize without the authority signing \
             was accepted (accepted.init_without_signer=true)",
        );
        let detail = only_finding(&out, &wd, "vault", "c_vault_initialization");

        // A real finding: something ran and succeeded, and the last action did its job.
        assert!(!detail.contains("SUSPECT HARNESS BUG"), "{detail}");
        // The two rejected attempts are visibly rejected…
        assert_eq!(detail.matches("reinitialize -> OK (tx rejected: 0)").count(), 2, "{detail}");
        // …and the one that went through is visibly different.
        assert!(detail.ends_with("3. init_without_signer -> OK"), "{detail}");
    }

    #[test]
    fn the_sequence_renders_one_numbered_action_per_line() {
        let r = repro(KLEND_ACTION_FAILED).render_sequence();
        assert_eq!(
            r,
            "reproducing sequence (iteration 4211, 2 action(s)):\n  \
             1. update_lending_market_owner -> OK\n  2. clone_reserve_config -> FAIL(3002)"
        );
    }

    #[test]
    fn the_detail_leads_with_the_summary_and_the_suspicion_then_the_sequence() {
        let wd = workdir_with_meta("lead", "crash_ad70", KLEND_INITIAL_STATE);
        let out = finding_line("crash_ad70", "[deposit_limit_not_exceeded] total supply exceeds");
        let detail =
            only_finding(&out, &wd, "kamino_lending", "c_liquidity_supply_ctoken_exchange");

        let (first, rest) = detail.split_once('\n').expect("multi-line detail");
        // The console shows only this line, so the deciding signal has to be on it.
        assert!(first.starts_with("crash crash_ad70: [deposit_limit_not_exceeded]"), "{first}");
        assert!(first.contains("SUSPECT HARNESS BUG"), "{first}");
        // The smell names what the metadata showed — that nothing in the sequence ran — rather
        // than the iteration number, which says only which test case this was.
        assert!(first.contains("not one action in the sequence succeeded"), "{first}");
        // …and the sequence follows, for the report.
        assert!(rest.starts_with("reproducing sequence (iteration 0, 1 action(s)):"), "{rest}");
        assert!(rest.contains("mark_deleveraging_unauthorized -> FAIL(3007)"), "{rest}");
    }

    #[test]
    fn a_genuine_finding_gets_its_sequence_but_no_suspicion_marker() {
        let meta = r#"{"iteration": 91, "actions": [
            {"name": "deposit_reserve_liquidity", "params": {}, "success": true}
        ]}"#;
        let wd = workdir_with_meta("genuine", "crash_beef", meta);
        let out = finding_line("crash_beef", "[conservation] vault drifted");
        let detail = only_finding(&out, &wd, "p", "c_u");

        assert!(!detail.contains("SUSPECT HARNESS BUG"), "{detail}");
        assert!(detail.contains("reproducing sequence (iteration 91, 1 action(s)):"), "{detail}");
    }

    #[test]
    fn a_finding_still_reports_when_the_metadata_is_missing() {
        // Best-effort enrichment: no crash file (or an unreadable one) must not lose the finding,
        // and must not change the one-line shape the report relied on before this existed.
        let out = finding_line("0001", "[balance never overflows] expected 100, got 0");
        let missing = std::env::temp_dir().join("crucible_triage_does_not_exist");
        let detail = only_finding(&out, &missing, "p", "c_u");
        assert_eq!(detail, "crash 0001: [balance never overflows] expected 100, got 0");
    }

    #[test]
    fn every_marker_line_in_the_log_becomes_its_own_finding() {
        // A campaign that is not told to stop prints one marker per NOVEL crash and keeps fuzzing,
        // so the log holds as many as it found — interleaved with the fuzzer's own chatter, which
        // must not become a finding of its own.
        let dir = workdir_with_meta("multi", "crash_a", KLEND_ACTION_FAILED);
        std::fs::write(
            dir.join(CRASHES_DIR).join("crash_b.meta.json"),
            r#"{"iteration": 11619, "actions": [{"name": "refresh", "success": true}]}"#,
        )
        .expect("write second meta");
        let log = format!(
            "[FUZZ] Running with 600s timeout\n{}\n[FUZZ] 4000 execs/s\n{}\n[FUZZ] done\n",
            finding_line("crash_a", "[stored_price_timestamp_not_in_future] ts=2 ahead of 0"),
            finding_line("crash_b", "[block_price_usage_kill_switch_effective] status=0b111111"),
        );

        let found = findings(&log, &dir, "kamino_lending", "c_oracle");

        assert_eq!(found.len(), 2, "{found:#?}");
        assert!(found[0].contains("stored_price_timestamp_not_in_future"), "{}", found[0]);
        assert!(found[1].contains("block_price_usage_kill_switch_effective"), "{}", found[1]);
        // Each is enriched from its OWN crash metadata, not the first one's.
        assert!(found[0].contains("clone_reserve_config -> FAIL(3002)"), "{}", found[0]);
        assert!(found[1].contains("iteration 11619"), "{}", found[1]);
    }

    #[test]
    fn a_log_with_no_marker_reports_nothing() {
        // The clean-run path is keyed on this being empty, so fuzzer chatter must never look like
        // a finding.
        let missing = std::env::temp_dir().join("crucible_triage_no_marker");
        assert!(findings("[FUZZ] 4000 execs/s\n[FUZZ] done\n", &missing, "p", "c_u").is_empty());
    }

    #[test]
    fn unparsable_metadata_degrades_to_the_bare_finding() {
        let wd = workdir_with_meta("garbage", "crash_bad", "{ not json");
        let out = finding_line("crash_bad", "[p] boom");
        assert_eq!(
            only_finding(&out, &wd, "p", "c_u"),
            "crash crash_bad: [p] boom"
        );
    }

    #[test]
    fn the_per_test_crashes_layout_is_also_searched() {
        // `crucible tmin`/`show` keep crashes under the harness crate's own `crashes/<test>/`
        // rather than the flat output dir, so a minimized crash is still found.
        let dir = std::env::temp_dir().join("crucible_triage_layout");
        let nested = dir.join(harness_dir("kamino_lending")).join("crashes").join("c_unit");
        std::fs::create_dir_all(&nested).expect("mkdir");
        std::fs::write(nested.join("crash_x.meta.json"), KLEND_INITIAL_STATE).expect("write");

        let detail =
            only_finding(&finding_line("crash_x", "[p] boom"), &dir, "kamino_lending", "c_unit");
        assert!(detail.contains("SUSPECT HARNESS BUG"), "{detail}");
    }

    #[test]
    fn a_marker_line_without_a_summary_is_passed_through_unchanged() {
        let raw = "[FUZZ_FINDING] crash:0001 reproduces:true";
        let missing = std::env::temp_dir().join("crucible_triage_nosummary");
        assert_eq!(only_finding(raw, &missing, "p", "c_u"), raw);
    }
}
