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
/// `iterations` is what the fuzzer said it drew, and it is an `Option` because that is a line of
/// its console output rather than a contract: a run that ends some other way, or a future Crucible
/// that words its summary differently, still has to produce a verdict. What is always known is the
/// wall clock, so the note degrades to that rather than disappearing.
pub(crate) struct Campaign {
    component: String,
    /// What the host allowed this campaign, in seconds — `fuzz_timeout`. Reported beside what was
    /// spent because the *gap* is the interesting part: a campaign that stopped well inside its
    /// budget did not run out of time, it ran out of something else.
    budget_s: u64,
    elapsed: Duration,
    iterations: Option<u64>,
}

/// The count Crucible reports at the end of a campaign: `[STATEFUL] Final stats: 41231 iterations,
/// 88 novel states, 0 crashes, pool: …`. Read off the token before `iterations` rather than by
/// position, so the surrounding fields can change without this silently reporting the wrong number.
fn reported_iterations(out: &str) -> Option<u64> {
    out.lines().filter(|l| l.contains("Final stats:")).find_map(|l| {
        let tokens: Vec<&str> = l.split_whitespace().collect();
        let at = tokens.iter().position(|t| t.trim_end_matches(',') == "iterations")?;
        tokens.get(at.checked_sub(1)?)?.parse().ok()
    })
}

impl Campaign {
    pub(crate) fn of(component: &str, budget_s: u64, elapsed: Duration, out: &str) -> Self {
        Campaign {
            component: component.to_string(),
            budget_s,
            elapsed,
            iterations: reported_iterations(out),
        }
    }

    /// The line every verdict from this campaign ends with.
    fn note(&self) -> String {
        let spent = match self.iterations {
            Some(n) => format!("{n} iterations in {:.0}s", self.elapsed.as_secs_f64()),
            None => format!("{:.0}s", self.elapsed.as_secs_f64()),
        };
        format!("[{}] campaign spent {spent} of a {}s budget", self.component, self.budget_s)
    }

    /// `outcome` with this campaign's note on every verdict, and the wall clock it took.
    ///
    /// The note goes **last**. A `BAD` verdict's first line is the deciding signal — it is the only
    /// line the live console shows (`composer.rustapp.session._emit_verdict`) — so putting run
    /// accounting above a counterexample would cost the author the one line that matters while they
    /// are authoring.
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
            v.detail = Some(match v.detail {
                Some(d) => format!("{d}\n\n{note}"),
                None => note.clone(),
            });
            v.duration_seconds = Some(seconds);
            v
        })
    }
}

#[cfg(test)]
mod tests {
    //! What a green row has to say for itself. The klend report of 2026-08-10 carried 375 of them
    //! and not one said how hard anything had looked.
    use super::*;
    use crate::testkit::{prop, target_over, verdicts_of};

    /// The real tail of a clean campaign, chatter included.
    const FINAL_STATS: &str = "[FUZZ] 4000 execs/s\n\
        [STATEFUL] Final stats: 41231 iterations, 88 novel states, 0 crashes, pool: 256 (active: 12)\n";

    fn one_check() -> Target {
        target_over("c_oracle", &[prop("refresh_is_value_neutral", "neutral")])
    }

    fn annotated(out: &str, outcome: ValidateOutcome) -> (Outcome, String) {
        let t = one_check();
        let c = Campaign::of("Oracle-Driven Refresh", 600, Duration::from_secs(597), out);
        let got = verdicts_of(&c.annotate(&t, outcome));
        (got[0].1, got[0].2.clone())
    }

    #[test]
    fn a_green_row_says_what_the_campaign_spent_and_what_it_was_allowed() {
        let t = one_check();
        let (outcome, said) = annotated(FINAL_STATS, t.all(Outcome::Good, None));

        assert_eq!(outcome, Outcome::Good, "the verdict itself is untouched");
        // The gap between the two numbers is the point: a campaign that stopped well inside its
        // budget did not run out of time, and a reader can only see that if both are here.
        assert!(said.contains("41231 iterations in 597s"), "{said}");
        assert!(said.contains("600s budget"), "{said}");
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
    fn a_counterexample_keeps_the_first_line_it_had() {
        // The live console shows only the first line of a detail, so accounting must never displace
        // the finding — an author watching a run would lose the one line that tells them what broke.
        let t = one_check();
        let crash = "crash abc: [refresh_is_value_neutral] value moved by 3\nsequence: …";
        let (outcome, said) = annotated(FINAL_STATS, t.all(Outcome::Bad, Some(crash.into())));

        assert_eq!(outcome, Outcome::Bad);
        assert!(said.starts_with("crash abc:"), "{said}");
        assert!(said.ends_with("of a 600s budget"), "{said}");
    }

    #[test]
    fn a_campaign_that_reported_no_count_still_says_how_long_it_ran() {
        // The count is a line of console output, not a contract. The wall clock always exists, so
        // the note degrades to it instead of leaving the row silent again.
        let t = one_check();
        let (_, said) = annotated("[FUZZ] nothing to report\n", t.all(Outcome::Good, None));

        assert!(said.contains("campaign spent 597s of a 600s budget"), "{said}");
        assert!(!said.contains("iterations"), "{said}");
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
