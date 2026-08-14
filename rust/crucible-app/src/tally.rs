//! What a campaign said it evaluated, and the gate that refuses `GOOD` without it.
//!
//! `fuzz_assert*` is silent on success, so a campaign's own output cannot distinguish a property
//! that held from one whose assertion never ran — a guard that never opens, an unreachable site,
//! or an assertion never written all exit clean (docs/crucible-unexercised-checks.md). The crate
//! root closes the gap by interposing counting wrappers on the assertion macros
//! (`tally_macros.j2`): each site prints
//! `[FUZZ_TALLY] site: <file>:<line>, evaluated: <n>, tag: <tag>` at every power-of-two count.
//! This module reads those lines back and downgrades any `GOOD` nothing vouches for.

use std::collections::BTreeMap;

use autoprover_sdk::outcome::{Check, Outcome, Target, ValidateOutcome, Verdict};

use crate::campaign::field;

/// Evaluations per tag: each site's largest count, summed over the sites sharing a tag.
///
/// Largest per site because a site prints its running count at powers of two, so its lines are
/// snapshots of one counter and only the last describes the campaign; summed across sites because
/// two sites asserting the same tag are distinct evidence (the site is on the line for exactly
/// this — without it, same-tag sites' interleaved prints could not be told apart). Every count is
/// therefore a lower bound within 2× of the truth, which is enough: the gate consumes "more than
/// zero" and the row reports "at least N".
///
/// The tag is free text (a property title may hold spaces), so it is the line's last field and is
/// read as a suffix rather than a token. A message with no `[<tag>]` prefix tallies as `?`, which
/// aggregates like any other tag and matches no property title, so those sites gate nothing.
pub(crate) fn evaluations(out: &str) -> BTreeMap<String, u64> {
    let mut sites: BTreeMap<(String, String), u64> = BTreeMap::new();
    for line in out.lines().filter(|l| l.contains("[FUZZ_TALLY]")) {
        let Some(site) = field(line, "site") else { continue };
        let Some(n) = field(line, "evaluated").and_then(|f| f.parse::<u64>().ok()) else {
            continue;
        };
        let Some((_, tag)) = line.split_once("tag: ") else { continue };
        let seen = sites.entry((tag.trim().to_string(), site.to_string())).or_default();
        *seen = (*seen).max(n);
    }
    let mut tags: BTreeMap<String, u64> = BTreeMap::new();
    for ((tag, _), n) in sites {
        *tags.entry(tag).or_default() += n;
    }
    tags
}

/// Why a check whose campaign found nothing still has no verdict.
fn never_evaluated(c: &Check) -> String {
    let tags: Vec<String> = c.properties.iter().map(|t| format!("`[{t}]`")).collect();
    format!(
        "the campaign never evaluated a {}-tagged assertion, so this check was not exercised: its \
         guard never opened, the assertion is unreachable, or it was never written. Finding no \
         violation is a fact about the states explored, not about a check nothing ran \
         (docs/crucible-unexercised-checks.md).",
        tags.join("/"),
    )
}

/// `outcome` with every `GOOD` made conditional on its check's tag having been evaluated at all.
///
/// "No violation" is a fact about the states the campaign explored; `GOOD` per check also claims
/// the check was *exercised*, and the tally is the campaign's only evidence of that. A `GOOD`
/// whose property titles no tally vouches for becomes `UNKNOWN` saying so; one with evidence keeps
/// its outcome and gains the count, because a green row's strength is otherwise invisible (the
/// same argument as [`Campaign::annotate`](crate::campaign::Campaign::annotate)). Everything
/// else — a finding, an error, a build failure — already carries its own evidence and passes
/// through untouched.
///
/// Rebuilt through [`Target::verdicts`] for the same reason as `annotate`: the verdict set's
/// payload is private, so the set can neither gain a check the target does not cover nor lose one
/// it does.
pub(crate) fn gate(
    target: &Target, evals: &BTreeMap<String, u64>, outcome: ValidateOutcome,
) -> ValidateOutcome {
    let ValidateOutcome::Verdicts { verdicts } = &outcome else { return outcome };
    let by_name: Vec<(&str, &Verdict)> = verdicts.iter().collect();
    target.verdicts(|c| {
        let found = by_name.iter().find(|(n, _)| *n == c.name).map(|(_, v)| (*v).clone());
        let mut v = found.unwrap_or_else(|| Verdict::with_outcome(Outcome::Unknown));
        if v.outcome != Outcome::Good {
            return v;
        }
        let evaluated: u64 = c.properties.iter().filter_map(|t| evals.get(t)).sum();
        if evaluated == 0 {
            return Verdict::detailed(Outcome::Unknown, never_evaluated(c));
        }
        let vouched = format!(
            "its {} assertion was evaluated at least {evaluated} times",
            c.properties.iter().map(|t| format!("`[{t}]`")).collect::<Vec<_>>().join("/"),
        );
        v.detail = Some(match v.detail {
            Some(d) => format!("{d}\n\n{vouched}"),
            None => vouched,
        });
        v
    })
}

#[cfg(test)]
mod tests {
    //! A clean campaign's `GOOD` is only as good as the evidence its checks ran — the gap the
    //! 2026-08-12 vault run's guarded invariants made concrete (a fixture whose init is rejected
    //! evaluates nothing and exits 0; docs/crucible-unexercised-checks.md).
    use super::*;
    use crate::testkit::{prop, target_over, verdicts_of};
    use crate::triage::attribute_findings;

    /// The output shape a campaign with the interposed wrappers prints: pulses interleaved with
    /// per-site tally snapshots, two sites sharing the `lamports_conserved` tag, and one site
    /// whose message carried no tag.
    const OUT: &str = "\
        [FUZZ] Running with 600s timeout\n\
        [FUZZ_TALLY] site: src/c_vault.rs:6, evaluated: 1, tag: vault lamports cover balance\n\
        [FUZZ_TALLY] site: src/c_vault.rs:6, evaluated: 2048, tag: vault lamports cover balance\n\
        [FUZZ_PULSE] run time: 3m-5s, clients: 1, corpus: 1839, executions: 67798\n\
        [FUZZ_TALLY] site: src/c_vault.rs:6, evaluated: 4096, tag: vault lamports cover balance\n\
        [FUZZ_TALLY] site: src/c_lamports.rs:4, evaluated: 512, tag: lamports_conserved\n\
        [FUZZ_TALLY] site: src/c_lamports.rs:9, evaluated: 128, tag: lamports_conserved\n\
        [FUZZ_TALLY] site: src/c_vault.rs:19, evaluated: 64, tag: ?\n\
        [FUZZ] done\n";

    #[test]
    fn a_sites_lines_are_snapshots_and_two_sites_sharing_a_tag_are_summed() {
        let evals = evaluations(OUT);
        // Only the last snapshot describes the campaign — 1 + 2048 + 4096 would count one
        // counter three times.
        assert_eq!(evals.get("vault lamports cover balance"), Some(&4096));
        // Two sites are distinct evidence; the site field is what keeps them apart.
        assert_eq!(evals.get("lamports_conserved"), Some(&640));
        // An untagged site aggregates under `?`, a tag no property claims.
        assert_eq!(evals.get("?"), Some(&64));
    }

    #[test]
    fn a_mangled_line_is_skipped_rather_than_guessed() {
        // These are console lines, not a schema — a line missing a field must not become a claim.
        let evals = evaluations(
            "[FUZZ_TALLY] site: src/c_a.rs:1, evaluated: n/a, tag: t\n\
             [FUZZ_TALLY] evaluated: 8, tag: no_site\n\
             [FUZZ_TALLY] site: src/c_a.rs:2, evaluated: 8\n\
             [FUZZ_TALLY] site: src/c_a.rs:3, evaluated: 16, tag: kept\n",
        );
        assert_eq!(evals.into_iter().collect::<Vec<_>>(), vec![("kept".to_string(), 16)]);
    }

    #[test]
    fn a_log_without_tallies_reports_nothing() {
        assert!(evaluations("[FUZZ] 4000 execs/s\n[FUZZ] done\n").is_empty());
    }

    /// One component's two checks, by the titles its assertions are tagged with.
    fn two_checks() -> Target {
        target_over(
            "c_lamport_custody",
            &[prop("lamports_conserved", "conserved"), prop("fees_capped", "fees")],
        )
    }

    #[test]
    fn a_good_nothing_vouches_for_becomes_unknown_saying_why() {
        // The KNOWN GAP case: clean exit, so the campaign concluded GOOD for both checks — but
        // only one tag was ever evaluated.
        let t = two_checks();
        let evals = evaluations("[FUZZ_TALLY] site: s.rs:1, evaluated: 512, tag: lamports_conserved\n");
        let got = verdicts_of(&gate(&t, &evals, t.all(Outcome::Good, None)));

        assert_eq!(got[0].1, Outcome::Good);
        assert!(got[0].2.contains("evaluated at least 512 times"), "{}", got[0].2);
        assert_eq!(got[1].1, Outcome::Unknown);
        assert!(got[1].2.contains("`[fees_capped]`"), "{}", got[1].2);
        assert!(got[1].2.contains("was not exercised"), "{}", got[1].2);
    }

    #[test]
    fn an_untagged_tally_vouches_for_no_check() {
        // `?` is what a message with no `[<tag>]` prefix tallies as. It proves an assertion ran,
        // but not WHICH property's — so it must not green a row.
        let t = two_checks();
        let evals = evaluations("[FUZZ_TALLY] site: s.rs:1, evaluated: 512, tag: ?\n");
        let got = verdicts_of(&gate(&t, &evals, t.all(Outcome::Good, None)));
        assert!(got.iter().all(|(_, o, _)| *o == Outcome::Unknown), "{got:?}");
    }

    #[test]
    fn a_finding_and_an_error_keep_their_own_evidence() {
        // The gate answers for silence only. A BAD carries its counterexample and an ERROR its
        // failure — downgrading or annotating either would displace the line that matters.
        let t = two_checks();
        let bad = "crash abc: [lamports_conserved] drifted by 3";
        let attributed =
            attribute_findings(&t, &[], &[bad.to_string()]);
        let got = verdicts_of(&gate(&t, &BTreeMap::new(), attributed));
        assert_eq!(got[0].1, Outcome::Bad);
        assert_eq!(got[0].2, bad, "the counterexample is untouched");

        let errored = t.all(Outcome::Error, Some("campaign died".into()));
        let got = verdicts_of(&gate(&t, &BTreeMap::new(), errored));
        assert!(got.iter().all(|(_, o, d)| *o == Outcome::Error && d == "campaign died"), "{got:?}");
    }

    #[test]
    fn a_refuted_campaigns_clean_rows_still_need_their_tag_evaluated() {
        // attribute_findings marks the checks no finding names GOOD (the campaign ran to budget).
        // That GOOD carries the same exercise claim as a clean run's, so the same gate applies:
        // one finding against one check must not vouch for the other's assertion having run.
        let t = two_checks();
        let attributed = attribute_findings(
            &t,
            &[],
            &["crash abc: [lamports_conserved] drifted by 3".to_string()],
        );
        let got = verdicts_of(&gate(&t, &BTreeMap::new(), attributed));
        assert_eq!(got[0].1, Outcome::Bad);
        assert_eq!(got[1].1, Outcome::Unknown, "GOOD-by-silence, and nothing vouched for it");
    }

    #[test]
    fn a_build_failure_is_left_exactly_as_it_was() {
        // Nothing ran, so there is no tally to consult and nothing to gate.
        let built = ValidateOutcome::BuildFailed { errors: "E0432".into() };
        let out = gate(&two_checks(), &BTreeMap::new(), built);
        assert!(matches!(out, ValidateOutcome::BuildFailed { errors } if errors == "E0432"));
    }
}
