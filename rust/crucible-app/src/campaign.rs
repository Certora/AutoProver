//! What one campaign spent, and how every verdict it produced says so.
//!
//! A `GOOD` from a campaign that explored to a ten-minute budget is a real claim; the same `GOOD`
//! from one that ran twelve seconds is nearly none. The report has nowhere else to tell them apart —
//! a row shows its check, its outcome and its `message`, and nothing about the run behind it — so
//! the message carries it. On *every* verdict, not only the failures: a failure explains itself
//! through its counterexample, and the green rows are the ones whose strength is otherwise
//! invisible.
//!
//! It also names the component. The report's groups are synthesized across components, so a reader
//! looking at a failing row cannot otherwise tell whose commentary to open.

use std::time::Duration;

use autoprover_sdk::outcome::{Outcome, Target, ValidateOutcome, Verdict};

/// What a finished campaign is known to have spent, and for which component.
///
/// Both counts are `Option` because each is a line of console output rather than a contract: a run
/// that ends some other way, or a future Crucible that words its summary differently, still has to
/// produce a verdict. What is always known is the wall clock, so the note degrades to that rather
/// than disappearing.
pub(crate) struct Campaign {
    component: String,
    /// What the host allowed this campaign, in seconds — `fuzz_timeout`. Reported beside what was
    /// spent because the *gap* is the interesting part: a campaign that stopped well inside its
    /// budget did not run out of time, it ran out of something else.
    budget_s: u64,
    elapsed: Duration,
    executions: Option<u64>,
    reach: Option<Reach>,
}

/// A `reached/total` pair as the fuzzer prints it.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Ratio {
    reached: u64,
    total: u64,
}

impl Ratio {
    fn parse(field: &str) -> Option<Self> {
        let (reached, total) = field.split_once('/')?;
        Some(Ratio { reached: reached.parse().ok()?, total: total.parse().ok()? })
    }

    fn pct(&self) -> f64 {
        if self.total == 0 {
            0.0
        } else {
            100.0 * self.reached as f64 / self.total as f64
        }
    }
}

/// How far into the program a campaign got, read off the last `[FUZZ_PULSE]` line it printed.
///
/// Crucible tracks edges and branches to steer itself, so these are on the console of *every*
/// campaign whether or not `--coverage` was asked for — capturing them costs nothing and is the
/// only evidence a green row otherwise has that anything was exercised behind it.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Reach {
    edges: Ratio,
    branches: Ratio,
    /// Harness actions that succeeded at least once, over every action the harness defines
    /// (Crucible's `discovered`). The sharpest of the three: klend's 2026-08-10 run reached 44 of
    /// 92, so half that suite's own action surface never once ran, and every property behind it
    /// held vacuously.
    actions: Ratio,
}

/// The value after `<label>:` on a `key: value, key: value` console line — Crucible's own lines
/// and the tally lines the wheel's wrappers print ([`crate::tally`]) — with any trailing comma
/// stripped. Read by label rather than by position so a line can add or reorder fields without
/// this silently reporting one number as another.
pub(crate) fn field<'a>(line: &'a str, label: &str) -> Option<&'a str> {
    let key = format!("{label}:");
    let tokens: Vec<&str> = line.split_whitespace().collect();
    let at = tokens.iter().position(|t| *t == key)?;
    Some(tokens.get(at + 1)?.trim_end_matches(','))
}

/// The last pulse line, which is the only one that describes the whole campaign — Crucible prints
/// one periodically.
fn last_pulse(out: &str) -> Option<&str> {
    out.lines().rfind(|l| l.contains("[FUZZ_PULSE]"))
}

/// The count Crucible reports at the end of a *stateful* campaign: `[STATEFUL] Final stats: 41231
/// iterations, 88 novel states, …`. Read off the token before `iterations` rather than by position,
/// for the same reason as [`field`].
///
/// This backend never passes `--stateful`, and a stateless single-core run prints no `Final stats`
/// line at all — so in practice the count comes from the pulse and this is the fallback for a run
/// configured some other way.
fn reported_iterations(out: &str) -> Option<u64> {
    out.lines().filter(|l| l.contains("Final stats:")).find_map(|l| {
        let tokens: Vec<&str> = l.split_whitespace().collect();
        let at = tokens.iter().position(|t| t.trim_end_matches(',') == "iterations")?;
        tokens.get(at.checked_sub(1)?)?.parse().ok()
    })
}

impl Campaign {
    pub(crate) fn of(component: &str, budget_s: u64, elapsed: Duration, out: &str) -> Self {
        let pulse = last_pulse(out);
        Campaign {
            component: component.to_string(),
            budget_s,
            elapsed,
            executions: pulse
                .and_then(|l| field(l, "executions")?.parse().ok())
                .or_else(|| reported_iterations(out)),
            reach: pulse.and_then(|l| {
                Some(Reach {
                    edges: Ratio::parse(field(l, "edges")?)?,
                    branches: Ratio::parse(field(l, "branches")?)?,
                    actions: Ratio::parse(field(l, "discovered")?)?,
                })
            }),
        }
    }

    /// The line every verdict from this campaign ends with.
    fn note(&self) -> String {
        let spent = match self.executions {
            Some(n) => format!("{n} executions in {:.0}s", self.elapsed.as_secs_f64()),
            None => format!("{:.0}s", self.elapsed.as_secs_f64()),
        };
        let mut note =
            format!("[{}] campaign spent {spent} of a {}s budget", self.component, self.budget_s);
        if let Some(r) = &self.reach {
            note.push_str(&format!(
                "; reached {:.1}% of edges and {:.1}% of branches, and got {}/{} of the harness's \
                 actions to succeed at least once",
                r.edges.pct(),
                r.branches.pct(),
                r.actions.reached,
                r.actions.total,
            ));
        }
        note
    }

    /// `outcome` with this campaign's note on every verdict, and the wall clock it took.
    ///
    /// The note rides `Verdict::accounting`, not `detail`. What the campaign spent is a claim about
    /// the run; a counterexample is a claim about the program, and a reader — or a findings
    /// write-up — asking for one should not be handed the other inside it. The host composes the
    /// two into the report row's message, so a green row still says what it cost.
    ///
    /// Rebuilt through [`Target::verdicts`] rather than edited in place: the verdict set's payload
    /// is deliberately private, so a backend cannot name a check the target does not cover or leave
    /// one of them out. That holds here too — this re-derives from the same target, and a verdict
    /// the outcome somehow lacks degrades to a bare note rather than vanishing.
    pub(crate) fn annotate(&self, target: &Target, outcome: ValidateOutcome) -> ValidateOutcome {
        let ValidateOutcome::Verdicts { verdicts } = &outcome else { return outcome };
        let note = self.note();
        let seconds = self.elapsed.as_secs_f64();
        let by_name: Vec<(&str, &Verdict)> = verdicts.iter().collect();
        target.verdicts(|c| {
            let ran = by_name.iter().find(|(n, _)| *n == c.name).map(|(_, v)| (*v).clone());
            let mut v = ran.unwrap_or_else(|| Verdict::with_outcome(Outcome::Unknown));
            v.duration_seconds = Some(seconds);
            v.noting(note.clone())
        })
    }
}

#[cfg(test)]
mod tests {
    //! What a green row has to say for itself. The klend report of 2026-08-10 carried 375 of them
    //! and not one said how hard anything had looked.
    use super::*;
    use crate::testkit::{accounting_of, prop, target_over, verdicts_of};

    /// The real tail of a clean *stateful* campaign, chatter included.
    const FINAL_STATS: &str = "[FUZZ] 4000 execs/s\n\
        [STATEFUL] Final stats: 41231 iterations, 88 novel states, 0 crashes, pool: 256 (active: 12)\n";

    /// A real pulse line from the klend harness, verbatim — this is what a campaign the way this
    /// backend runs Crucible (stateless, single core) actually prints, and it prints no
    /// `Final stats` line at all.
    const PULSE: &str = "[FUZZ_PULSE] run time: 3m-5s, clients: 1, corpus: 1839, crashes: 728, \
        executions: 67798, exec/sec: 380.5, edges: 4699/76548 (6.1%), \
        branches: 4077/38274 (10.7%), actions/exec: 2.9, ok: 96676/195582 (49.4%), \
        discovered: 44/92 actions";

    fn one_check() -> Target {
        target_over("c_oracle", &[prop("refresh_is_value_neutral", "neutral")])
    }

    /// The first row's outcome and its **accounting** — what these tests are about. Its `detail`
    /// is the campaign's evidence, which this never touches.
    fn annotated(out: &str, outcome: ValidateOutcome) -> (Outcome, String) {
        let t = one_check();
        let c = Campaign::of("Oracle-Driven Refresh", 600, Duration::from_secs(597), out);
        let annotated = c.annotate(&t, outcome);
        (verdicts_of(&annotated)[0].1, accounting_of(&annotated)[0].clone())
    }

    #[test]
    fn a_green_row_says_what_the_campaign_spent_and_what_it_was_allowed() {
        let t = one_check();
        let (outcome, said) = annotated(FINAL_STATS, t.all(Outcome::Good, None));

        assert_eq!(outcome, Outcome::Good, "the verdict itself is untouched");
        // The gap between the two numbers is the point: a campaign that stopped well inside its
        // budget did not run out of time, and a reader can only see that if both are here.
        assert!(said.contains("41231 executions in 597s"), "{said}");
        assert!(said.contains("600s budget"), "{said}");
    }

    #[test]
    fn a_green_row_says_how_far_into_the_program_the_campaign_got() {
        // The klend report of 2026-08-10 published 375 green rows from campaigns that never got
        // half their own actions to succeed. Nothing on the row said so, and coverage is the only
        // thing that distinguishes a property that held from one that was never exercised.
        let t = one_check();
        let (_, said) = annotated(PULSE, t.all(Outcome::Good, None));

        assert!(said.contains("6.1% of edges"), "{said}");
        assert!(said.contains("10.7% of branches"), "{said}");
        assert!(said.contains("44/92 of the harness's actions"), "{said}");
    }

    #[test]
    fn the_count_comes_from_the_line_a_stateless_campaign_actually_prints() {
        // This backend never passes `--stateful`, so no `Final stats` line is ever emitted and the
        // pulse is the only place an execution count exists.
        let t = one_check();
        let (_, said) = annotated(PULSE, t.all(Outcome::Good, None));

        assert!(said.contains("67798 executions"), "{said}");
    }

    #[test]
    fn only_the_last_pulse_describes_the_whole_campaign() {
        // Crucible prints one periodically; an earlier line is a snapshot of a shorter run.
        let early = PULSE.replace("edges: 4699/76548 (6.1%)", "edges: 12/76548 (0.0%)");
        let t = one_check();
        let (_, said) = annotated(&format!("{early}\n{PULSE}\n"), t.all(Outcome::Good, None));

        assert!(said.contains("6.1% of edges"), "{said}");
    }

    #[test]
    fn a_campaign_that_printed_no_pulse_still_says_what_it_spent() {
        // Coverage is an extra, not a precondition — a run that never got far enough to pulse must
        // still produce the accounting it does have.
        let t = one_check();
        let (_, said) = annotated(FINAL_STATS, t.all(Outcome::Good, None));

        assert!(said.contains("41231 executions in 597s"), "{said}");
        assert!(!said.contains("edges"), "{said}");
    }

    #[test]
    fn a_reach_field_that_does_not_parse_is_left_out_rather_than_guessed() {
        // These are console fields, not a schema. Half a coverage claim is worse than none.
        let t = one_check();
        let mangled = PULSE.replace("branches: 4077/38274", "branches: n/a");
        let (_, said) = annotated(&mangled, t.all(Outcome::Good, None));

        assert!(said.contains("67798 executions"), "the count still lands: {said}");
        assert!(!said.contains("edges"), "{said}");
    }

    #[test]
    fn the_row_names_the_component_whose_campaign_it_was() {
        // The report's groups are synthesized across components, so nothing else on the row says
        // which commentary explains it.
        let t = one_check();
        let (_, said) = annotated(FINAL_STATS, t.all(Outcome::Good, None));
        assert!(said.starts_with("[Oracle-Driven Refresh]"), "{said}");
    }

    #[test]
    fn a_counterexample_is_left_exactly_as_the_campaign_reported_it() {
        // Accounting must never end up inside the evidence. The live console shows the first line
        // of a detail and a findings write-up is handed the whole of it, so a counterexample with
        // run accounting glued to it costs the author the line that matters and the write-up its
        // proof of concept.
        let t = one_check();
        let crash = "crash abc: [refresh_is_value_neutral] value moved by 3\nsequence: …";
        let annotated =
            Campaign::of("Oracle-Driven Refresh", 600, Duration::from_secs(597), FINAL_STATS)
                .annotate(&t, t.all(Outcome::Bad, Some(crash.into())));
        let got = verdicts_of(&annotated);

        assert_eq!(got[0].1, Outcome::Bad);
        assert_eq!(got[0].2, crash, "the evidence is untouched");
        assert!(accounting_of(&annotated)[0].ends_with("of a 600s budget"));
    }

    #[test]
    fn a_campaign_that_reported_no_count_still_says_how_long_it_ran() {
        // The count is a line of console output, not a contract. The wall clock always exists, so
        // the note degrades to it instead of leaving the row silent again.
        let t = one_check();
        let (_, said) = annotated("[FUZZ] nothing to report\n", t.all(Outcome::Good, None));

        assert!(said.contains("campaign spent 597s of a 600s budget"), "{said}");
        assert!(!said.contains("executions"), "{said}");
    }

    #[test]
    fn the_count_is_read_off_the_word_it_labels() {
        // Positional parsing would keep working right up until Crucible adds a field, and then
        // report a pool size as an iteration count.
        assert_eq!(reported_iterations(FINAL_STATS), Some(41231));
        assert_eq!(
            reported_iterations("[STATEFUL] Final stats: 7 crashes, 900 iterations, pool: 3"),
            Some(900),
        );
        assert_eq!(reported_iterations("[STATEFUL] Final stats: pool: 3"), None);
        assert_eq!(reported_iterations(""), None);
    }

    #[test]
    fn a_build_failure_is_left_exactly_as_it_was() {
        // Nothing was fuzzed, so there is no campaign to account for.
        let c = Campaign::of("Oracle", 600, Duration::from_secs(1), "");
        let built = ValidateOutcome::BuildFailed { errors: "E0432".into() };
        let out = c.annotate(&one_check(), built);
        assert!(matches!(out, ValidateOutcome::BuildFailed { errors } if errors == "E0432"));
    }
}
