# Unexercised checks: why a clean campaign is not a passing check

A Crucible campaign that finds nothing marks every check it covers `GOOD`. That claims more than the
run established, and this note says exactly how much more, what it looks like in practice, and what
would close it. The gap is marked in the code at the one place that makes the claim (`app.rs`,
`KNOWN GAP`), and listed as the open follow-up in [author-determined-checks.md](author-determined-checks.md).

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

## Fixes

### 1. Make the precondition reportable (preferred)

Hoist the guard into the macro so the runtime sees both halves instead of a collapsed `bool`:

```rust
fuzz_assert_when!(
    f.read_vault(),                                      // precondition, visible to the runtime
    |v| f.lamports_of(&f.vault_pda) >= v.balance,        // the property
    "[vault_lamports_cover_balance] …"
);
```

Three outcomes per tag instead of two — violated / meaningfully held / precondition unmet — so a
campaign reports `evaluated=4479, meaningful=0` and the verdict becomes `UNKNOWN` with a detail
saying which. This also catches the case a bare hit-counter misses: the assertion runs, but its
precondition never opens.

### 2. Count evaluations inside `fuzz_assert!` (smaller)

A per-tag tally incremented by the macro, printed once at campaign end and parsed beside the
existing stats. `campaign.rs` already reads executions and edge/branch coverage off the
`[FUZZ_PULSE]` lines that `crucible-fuzz-macro` emits, so the channel exists — runtime prints, wheel
parses, verdict follows. `evaluated=0` ⇒ `UNKNOWN`.

The fiddly part: `fuzz_assert!` formats its message *only on failure*, so the `[tag]` is not
available as a counter key without paying `format!` on every evaluation. Either take the tag as a
string-literal argument (a `&'static str` key for free, but it changes the authoring surface), or
key by `(file!(), line!())` — free, with the wheel mapping line → tag against the section file it
just wrote. The latter touches source text only to *name* evidence the counter already established,
which is the reverse of parsing source to decide whether something is a check.

### 3. LCOV (interim, no crucible-repo change)

`--coverage` is already passed and the LCOV preserved per component. An assertion line never executed
did not run. It needs a tag→line map, so it inherits the source-parse fragility, and it is
line-granular rather than tag-granular — weaker than either option above, but available today.

## Rejected

- **An author-written `AtomicUsize` beside the assertion.** The model writes the instrumentation for
  the check the instrumentation is meant to police: an author who does not reach the assertion
  equally does not reach the counter, and a `0` is indistinguishable from a counter never added. It
  is a fine thing for a human to add while debugging one harness; it cannot be the gate. If the
  crucible repo is being changed anyway, the increment belongs in the macro, where it cannot be
  forgotten and is per-tag for free.
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
