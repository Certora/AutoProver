# Unexercised checks: why a clean campaign is not a passing check

A Crucible campaign that finds nothing marks every check it covers `GOOD`. That claims more than the
run established, and this note says exactly how much more, what it looks like in practice, and the
proposed fix — which needs no change to the crucible repo, because the macro a call site expands is
decided by name resolution, and the wheel writes every line that resolution reads. The gap is marked
in the code at the one place that makes the claim (`app.rs`, `KNOWN GAP`), and listed as the open
follow-up in [author-determined-checks.md](author-determined-checks.md).

## The inference

`fuzz_assert!` does nothing on success — it calls `record_violation` only when its condition is
false. So the whole signal a campaign produces is *violations*, and silence is the only other state.

That licenses exactly one conclusion: **no counterexample was found among the states explored.**
`Outcome::Good` says something stronger — "The property holds" — and getting from one to the other
needs a premise nobody checks: that the assertion was evaluated at all. `triage.rs` states the
premise out loud:

> A campaign that ran `ToBudget` **explored every check it covers** whatever it found on the way, so
> an unnamed check held and is `GOOD`.

## What that looks like

Most Solana invariants are guarded, because the account may not exist yet:

```rust
fn invariants(f: &mut Fixture) {
    if let Some(vault) = f.read_vault() {                       // ← the guard
        fuzz_assert!(
            f.lamports_of(&f.vault_pda) >= vault.balance,
            "[vault_lamports_cover_balance] lamports={} < balance={}", …
        );
    }
}
```

Suppose every `action_initialize` is rejected — the fixture derives the PDA with a seed the program
does not expect, or passes an authority that never signs. The vault is never created, `read_vault()`
is `None` on all 4479 evaluations, and the `fuzz_assert!` inside never executes once. No violation,
exit 0, `GOOD`.

This is not a corner case. The 2026-08-12 vault run's own crash sequences are full of
`withdraw_large_with_fee -> OK (tx rejected: 6001)` and `reinitialize -> OK (tx rejected: 0)`:
actions being rejected while the campaign continues is the norm. Any invariant guarded on such an
action having succeeded has this shape.

**The two cases are indistinguishable from outside:**

| | evaluated 4479×, always held | evaluated 0× |
| --- | --- | --- |
| violations | none | none |
| exit code | 0 | 0 |
| executions / edges / branches | healthy | healthy |
| verdict | `GOOD` | `GOOD` |

The coverage numbers actively mislead here: they measure the *program's* edges, not whether the
assertion ran. A campaign can drive 11/11 actions and cover 20% of branches with an invariant that
is a no-op.

**It is already accepted as a principle.** `triage.rs` refuses precisely this inference one case
over — a campaign stopped by `UntilFirstFinding` reports `UNKNOWN`, because `GOOD` "would be a claim
about a space nothing searched." A guarded, never-evaluated assertion *is* a claim about a space
nothing searched. The distinction currently drawn is *when the campaign stopped*, when the principle
it rests on is *whether the check was exercised*.

**Foundry does not have this problem**, which is worth knowing before treating a fix as
gold-plating. `composer/foundry/runner.py` takes the test names from forge's own results ("the
publish gate validates the declared property→test mapping against this ground truth instead of
trusting the agent's transcription"), and `report.py` builds verdicts *out of* that set. A test that
does not exist gets no verdict and cannot be claimed. Crucible is the outlier, only because its
checker emits crashes rather than an execution record.

## The fix: interpose on `fuzz_assert!` from the crate root

The evidence that closes the gap is a per-tag evaluation count: an increment inside the macro cannot
be forgotten, never parses source text, and turns a silent campaign's claim from "no violation" into
"no violation across N evaluations" — where `N = 0` is exactly the case the verdict must stop
calling `GOOD`. Earlier drafts of this note assumed the increment therefore had to live in the
crucible repo. It does not.

`fuzz_assert*` are ordinary `#[macro_export] macro_rules!` in `crucible-test-context`, re-exported
by `crucible_fuzzer` (seven of them: the bare one plus `_{eq,ne,lt,le,gt,ge}`), and authored code
reaches them only through globs — `use crucible_fuzzer::*;` in the fixture at crate root, then the
`use super::*;` that `section_file.j2` supplies. Both hops funnel through the crate root, which the
wheel renders (`root_text`). So the crate root can bind the name to a wrapper of its own, and every
call site in every section expands the wrapper — no authoring-surface change, and no author
compliance to police:

```rust
macro_rules! __tally_fuzz_assert {
    ($cond:expr, $fmt:literal $(, $args:expr)* $(,)?) => {{
        {
            static __EVALS: ::std::sync::atomic::AtomicU64 =
                ::std::sync::atomic::AtomicU64::new(0);
            let __n = __EVALS.fetch_add(1, ::std::sync::atomic::Ordering::Relaxed) + 1;
            if __n.is_power_of_two() {
                let __tag = $fmt.split_once('[').and_then(|(_, r)| r.split_once(']'))
                    .map(|(t, _)| t).unwrap_or("?");
                ::std::println!("[FUZZ_TALLY] tag: {__tag}, evaluated: {__n}");
            }
        }
        ::crucible_fuzzer::fuzz_assert!($cond, $fmt $(, $args)*);
    }};
    // + a bare-condition arm (keyed by file!/line!, no tag) and a non-literal passthrough arm
}
pub(crate) use __tally_fuzz_assert as fuzz_assert;
```

The name-resolution detail is load-bearing, and was verified on 2026-08-13. A bare textual shadow —
`macro_rules! fuzz_assert` at crate root — is `E0659` in the sections, because `use super::*`
re-imports crucible's macro into path-based scope beside it. The private name plus explicit
re-export works because an explicit binding shadows a glob within its module: the crate root's
namespace then holds exactly one `fuzz_assert` — ours — and the section's `use super::*` imports
exactly that one. The delegation is path-qualified, so the original macro still does everything it
did: the condition is evaluated once, the message still formats only on failure, `record_violation`
fires as before.

**The channel already exists.** Campaigns run in libafl's `InProcessExecutor` (`singlecore.rs`), so
a per-site `static` persists across every execution — the same fact that lets `take_violation`'s
thread-local work — and a `println!` from the harness lands in the output `validate` captures, which
is where `campaign.rs` already reads the `[FUZZ_PULSE]` lines. Same runtime-prints-wheel-parses
shape; one more label-parsed line format.

**The tag costs nothing per evaluation.** The fiddly part of the earlier counting proposal — the
`[tag]` lives in a message that is formatted only on failure — dissolves: the format string is a
`$fmt:literal`, so the wrapper holds it as a `&'static str` and slices the tag out of it *inside the
power-of-two branch only*. The hot path is one relaxed `fetch_add` and one branch; the printing is
≤ ~13 lines per site per campaign (counts print at powers of two, so the last line is within 2× of
the true count — and the verdict only needs "greater than zero").

**The verdict gate.** A small parser (label-based, the way `campaign.rs` reads pulse fields; max per
site, then summed per tag) turns the tally lines into `tag → evaluations`. Then the two places that
currently conclude `GOOD` on silence — the clean-exit path in `app.rs`, and `attribute_findings`'
unnamed-check-on-`ToBudget` branch — grant it per check only when some property title the check
claims has a tally above zero, and answer `UNKNOWN` otherwise, with a detail saying no
`[<title>]`-tagged assertion was ever evaluated. That is the verdict contract's own wording ("a
check with no evidence it ran comes back `ERROR` or `UNKNOWN`, never `GOOD`") finally enforced at
this seam. The tag convention itself is no new fragility: it is already the load-bearing convention
attribution places findings by.

That closes all three shapes at once: an assertion never written (no tally line carries its tag — a
case the LCOV option below cannot even see, having no line to find unexecuted), an assertion written
where the fuzzer cannot reach it, and a guard that never opened. It also collapses the distinction
the earlier `fuzz_assert_when!` proposal existed for: with the author's guard *outside* the macro —
the shape they already write — "evaluated" already means "the precondition opened", so the count is
the meaningful count.

**Every hole points the safe way.** A section author can dodge the wrapper — an explicit
`use crucible_fuzzer::fuzz_assert;`, or a path-qualified `crucible_test_context::fuzz_assert!` — and
what that buys is a missing tally, which is an `UNKNOWN`, never a false `GOOD`; the section
normalizer (`as_module_body`) can strip the import spelling on top. An authored glob of
`crucible_fuzzer` inside a section is `E0659` at the gate, and an explicit import in the fixture
collides with the re-export as a duplicate binding — both loud, both the revise loop's to fix. And
if a future Crucible forked per execution, resetting the statics, a printed line still proves at
least one evaluation — the only fact the gate consumes.

One empirical check remains before relying on it: a real campaign with the wrapper in place, to
confirm no libafl mode redirects the harness's stdout somewhere `validate` does not capture. Every
observed mode prints `[FUZZ_PULSE]`/`[FUZZ_FINDING]` from the same process, so the risk is
theoretical.

## Where the increment belongs eventually

Upstream. If the crucible repo is being changed anyway, `fuzz_assert!` itself should record each
evaluation and the campaign should report the tally in its own end-of-run summary — per-tag,
unforgeable, and available to every crucible user rather than only to harnesses this wheel renders.
A `fuzz_assert_when!` that takes the precondition as a visible argument belongs there too, for the
genuinely conditional property whose author wants "evaluated" and "precondition opened" reported
separately. The interposition produces the same evidence today; the day crucible's macros record
evaluations, the wrapper block and its parser are deleted and the verdict gate stays.

## LCOV (cross-check)

`--coverage` is already passed and the LCOV preserved per component. An assertion line never
executed did not run — so LCOV answers the one question the tally cannot: whether a missing tally
means *no assertion exists for this tag* or *one exists and was never reached*, since a site that
never runs never announces itself and the two read identically in the tally. As a verdict source it
stays inferior — it needs a tag→line map and inherits the source-parse fragility, and it is
line-granular rather than tag-granular — but as triage for an `UNKNOWN` row it is already on disk.

## Rejected

- **An author-written `AtomicUsize` beside the assertion.** The model writes the instrumentation for
  the check the instrumentation is meant to police: an author who does not reach the assertion
  equally does not reach the counter, and a `0` is indistinguishable from a counter never added. It
  is a fine thing for a human to add while debugging one harness; it cannot be the gate. The
  increment belongs in the macro, where it cannot be forgotten and is per-tag for free — and the
  macro, it turns out, is reachable without the crucible repo (above).
- **Folding the guard into the condition** — `f.read_vault().map(…).unwrap_or(true)`. This makes
  things strictly worse: it *asserts* the vacuous case, converting an unevaluated assertion (which a
  counter catches) into a vacuously true one (which nothing catches). It also launders real evidence
  of a broken fixture — a `read_vault()` that is `None` all campaign means the PDA or the init is
  wrong — into a green row.
- **A judge-enforced "no conditional assertions" convention.** A syntactic rule belongs in
  `check_syntax` (pure, per-edit, deterministic) rather than an LLM, by the same argument that keeps
  check *existence* out of the judge's hands. But neither can enforce it soundly: early `return`,
  `let … else`, `match` arms, `continue` in a loop, or an assertion inside a helper all defeat a
  textual rule, which buys false confidence. And some guards are correct — "*if* the vault exists,
  lamports cover balance" is a genuinely conditional property. The goal is not to outlaw guards but
  to measure how often they opened.

## Out of reach

An assertion that *is* evaluated and is vacuous anyway:

```rust
let acct = f.svm.get_account(&f.vault_pda).unwrap_or_default();   // zeroed on miss
fuzz_assert!(acct.lamports >= vault.balance, "[…]");              // 0 >= 0, forever
```

It executes thousands of times and every counter is satisfied. No mechanism here reaches it; that is
the judge's, and it is the same residue Foundry carries with a tautological `assertTrue`.
