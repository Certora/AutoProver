# Proposal: per-check fuzz targets

**Status:** implemented (see §9 for what changed on the way). **Affects:** `rust/crucible-app` (targets, generation, authoring contract),
`rust/autoprover-sdk` (one enum renamed), `composer/rustapp` (that rename, one call site).
**Supersedes** the grouping rule in [crucible.md §8](./crucible.md).

A Crucible component was one campaign covering all of its checks. This made the *check* the unit a
campaign runs — always, with no second grouping — and, while the seam was open, replaced
`Exploration` with the one bit the host actually knows. §9 records what the implementation changed
about the plan below.

---

## 1. The problem

The wire contract makes a promise the wheel cannot keep. `Exploration::ToBudget` is documented as

> Explore every covered check to the full budget, whatever is found on the way.

and [crucible.md §8](./crucible.md) spells out what the wheel does with it: when a finding lands on
one of the target's own checks, "that check `BAD`; the rest stay `GOOD` (subject to the tally)".

The rest do not stay `GOOD` on their own evidence. They stay `GOOD` because nothing refuted them in
a campaign whose exploration was shaped by a *different* check. Three mechanisms, none of which the
tally detects:

1. **An input stops at its first violation.** The remaining actions do not run —
   `2 executed, 4 skipped ... (stopped on violation)`. Any state reachable only *after* the
   violating action is unreachable for every check in the target.
2. **The refuting input leaves the corpus.** It becomes an objective, so the prefix that reached the
   interesting state stops being mutated on behalf of anything else.
3. **`record_violation` is first-wins.** Within one invariant pass only the source-order-first
   violated check is recorded; simultaneous violations of later checks are dropped silently.

The tally does not catch this because it counts *evaluations*, not evaluations *in states where the
check could fail*. A check evaluated 65,536 times in shallow states looks identical to one that was
genuinely exercised.

### Evidence (klend, 2026-08-20, cache namespace `klend-0819`)

235 checks across 13 components — 13 campaigns. Re-running one component's target with 10× the time
on 4 cores:

| | executions | crashes | distinct checks refuted |
|---|---|---|---|
| pipeline (60s, 1 core) | 4,788 | — | 1 |
| rerun (600s, 4 cores) | **166,080** | 8 | **1 — the same one** |

35× the executions surfaced no additional check. All 8 crashes were
`[obligation_orders_can_be_armed_immediately_before_accept]`.

The masked case is concrete. Checks appear in the target in source order:

```
184  borrow_order_can_be_armed_while_transfer_in_progress
216  obligation_orders_can_be_armed_immediately_before_accept   ← fires at state != 0
248  referrer_binding_transplanted_to_the_new_owner...          ← needs a COMPLETED transfer
271  happy_path_transfer_succeeds_and_preserves_the_whole_position
```

`referrer_binding_transplanted` needs `initiate → approve → accept` to land. Every input that arms
an obligation order anywhere in that prefix dies at the arming step, so `accept` never runs. The
check was evaluated 65,536 times, always in shallow states, and reported `GOOD`.

The downstream cost is visible in the report: of 26 findings, **22 rest on the author's source
reading with no counterexample**, and 18 of those are checks carrying no assertion at all. When a
target yields one refutation regardless of budget, an author who can see three more defects in the
source has no way to report them except by declaring them.

### Foundry does not have this problem

`invariant_*` is structurally identical — a stateful fuzzer generating call sequences and checking
an assertion after every step — but `forge test` runs each test function as its own isolated test
with its own campaign and returns a status per function, which is exactly what the backend consumes
(`test_results: dict[str, _ForgeTestEntry]`). Its unit of execution *is* its unit of reporting.
Crucible's unit of execution is a Cargo feature (a whole component) while its unit of reporting is a
tagged assertion; the two differ by ~18×. This proposal closes that gap. Per-check campaigns are not
an exotic tax — they are what the comparable tool does by default.

---

## 2. What changes

### 2.1 Checks become individually addressable

Today a section is one authored fn holding every check as inline `fuzz_assert!` calls. The only
handle on an individual check is the `[tag]` string inside its assertion message — which is also how
verdicts are attributed. There is no way to build a target containing a subset.

The authored section gains one `pub fn` per check, named for the check:

```rust
// c_obligation_ownership_transfer.rs
use super::*;

pub fn c_referrer_binding_transplanted_to_the_new_owner(fixture: &Fixture) {
    for (addr, ob) in fixture.obligations() {
        ...
        fuzz_assert_eq!(ob.referrer, owner_meta_referrer, "[referrer_binding_transplanted...] ...");
    }
}
```

The check name is already `c_<property slug>` and already unique within a run, so the fn name is
determined, not invented. Shared helpers stay in the section as ordinary private fns.

This is the change that makes everything else mechanical, and it has a payoff of its own: verdict
attribution stops depending on parsing a tag out of a message string. A per-check target has exactly
one check, so its verdict needs no attribution at all.

### 2.2 Every check is its own target

`Check.target = None` already means "its own target", and `targets_of()` already partitions on it.
The wheel stops collapsing:

```rust
fn target_for(&self, input: &AuthorInput, _check: &str) -> Option<String> {
    let Authored::Component { .. } = input.authored else { return None };
    None   // every check is its own target
}
```

**The host needs no change at all** for this part — it already runs each distinct target once.

There is deliberately no second granularity. An earlier draft kept a coarse mode for the authoring
loop; §7 records why that was wrong.

### 2.3 `Exploration` becomes `Stakes`, and the budget goes back behind the wheel's own arg

The host's decision is one bit, and it already computes it under that name:

```python
partial = self.checks is not None                    # the author asked for a subset; never stamps
covered = targets_of(wanted, Exploration.UNTIL_FIRST_FINDING if partial else Exploration.TO_BUDGET)
```

It then translates that bit into a fuzzer verb before putting it on the wire. `Exploration`'s two
values are Crucible's two CLI modes (`--stop-on-crash` or not), it has exactly one consumer, and its
job — telling the reader how to interpret a *negative* result — is already done by the `Outcome`
vocabulary for any checker that can distinguish "proved" from "not refuted within budget". A
symbolic prover has nothing coherent to do with "explore every covered check to the full budget": it
does not explore, its budget is a per-rule solver timeout, and an unrefuted rule is a positive claim
rather than a budget-relative absence. Where its negatives *are* budget-relative it returns
`TIMEOUT`, which is its own `Outcome`.

So the seam should carry the bit the host has, named for what it means:

```rust
/// What rides on this invocation's answer — set from what the host will do with it, never inferred
/// from the shape of the check set.
pub enum Stakes {
    /// The author is iterating; these verdicts will not be reported. A backend may answer as
    /// cheaply as it can, and what its unrefuted checks then say is its own to decide.
    Feedback,
    /// These verdicts stamp the publish gate. The default, because a run that quietly stops short is
    /// the failure mode worth defaulting away from.
    #[default]
    OfRecord,
}
```

Each backend maps it: Crucible to `--stop-on-crash` plus a short budget, Foundry to `--fail-fast`, a
prover to a cheaper solver configuration. The existing `triage.rs` logic is unchanged — it switches
on the same one bit, under a name that survives a second backend.

**The budget does not cross the seam.** `fuzz_timeout` is already the Crucible wheel's own declared
arg (`--fuzz-timeout`), so the wheel derives its per-target budget from that arg plus `Stakes`. An
earlier draft had the host set a per-target budget; that would have put a fuzzer quantity in a
shared type for the second time in the same document.

### 2.4 Generation

`Cargo.toml` gains a feature per check, and the root emits a per-check entry that shares the
component module. Only one feature is ever enabled per build, as now:

```rust
#[cfg(any(feature = "c_component", feature = "c_check_x", feature = "c_check_y"))]
mod c_component;

#[cfg(feature = "c_check_x")]
#[invariant_test]
fn c_check_x(f: &mut Fixture) { c_component::c_check_x(f) }
```

The component-wide feature stays, with a narrower job: it is no longer a target, only the warm-up
campaign of §2.5. Templates touched: `cargo_toml.j2`, `root_layout.j2`, `section_entry.j2`,
`section_file.j2`.

### 2.5 Corpus seeding recovers what isolation costs

Per-check targets lose something real: a deep state one check's mutations discovered is no longer
available to its siblings. Recover it by running the component-wide campaign first as a warm-up and
seeding every per-check campaign from its corpus:

```
crucible run <program> c_component  --corpus-out <ns>/corpus/c_component --timeout <warmup>
crucible run <program> c_check_x    --corpus-in  <ns>/corpus/c_component --corpus-out <ns>/corpus/c_check_x ...
```

`crucible run` already has `--corpus-in` / `--corpus-out`, and §8 already routes a shared corpus
through `.certora_internal/crucible/corpus`. Each per-check campaign starts from states the fixture
is known to reach — and then explores *on behalf of its own check*.

---

## 3. Cost

Measured shape of a klend run: **18.1 checks per component** (max 24), and **2–11 `validate_spec`
calls per component** (mean ~5.8).

| | builds | campaigns | wall |
|---|---|---|---|
| authoring, full validate — today | 1 | 1 × 60s | ~1 min |
| authoring, full validate — proposed (`Feedback`, 10s budget) | ~18 | 18 × ≤10s | ~4 min |
| authoring, focused validate (1–2 checks) | 1–2 | 1–2 × ≤10s | **~20s** |
| gate, per component (`OfRecord`) | ~18 | 18 × 60s | ~18 min |
| gate, whole run (235 checks) | 235 | 235 × 60s | ~4 h serial, ~30 min at 8-way |

Two things keep the authoring loop affordable, and neither weakens a verdict:

- **`Feedback` buys a short budget**, not a contaminated one. Eighteen checks at 10s each still gives
  every check its own attention — strictly more than today's one 60s campaign where one check gets
  the attention and seventeen ride along.
- **A focused validate finally means something.** `validate_spec(checks=[...])` today filters what is
  *reported* while still running the whole component campaign. With per-check targets it filters what
  is *run*, so the common case — the author revising one or two checks — gets cheaper than today. No
  new mechanism; the existing argument just starts doing what it says.

The irreducible cost is builds: ~18 cargo invocations for a full authoring validate, ~4s each
incremental, and shortening the fuzz budget does not touch it. Only focused validates do.

The gate is 10–20× today's gate cost, and it buys the thing the contract already promises. If that
proves too expensive the tunable is `--fuzz-timeout`, not the granularity: a check with 20s of its
own attention is measured; a check sharing a campaign is not.

---

## 4. What it changes downstream

- **`GOOD` becomes meaningful.** Today it can mean "not refuted in a campaign steered by another
  check". After, it means "this check had its own budget and its own corpus and was not refuted".
- **Declared findings should fall.** Testable: several of klend's 22 source-reading declarations
  should become either real counterexamples or honest `UNKNOWN`s. Either beats a finding with no
  evidence.
- **Attribution simplifies.** The §8 table's first row collapses — a target has one check, so "the
  rest" does not exist. The cross-component and unknown-title rows stay.
- **Coverage debt gets visible.** A check whose own campaign never evaluated its assertion is
  unambiguously not-exercised, with nothing else to blame.

---

## 5. Migration

- **Cached specs go stale.** The authoring contract changes, so the author prompt changes, so cache
  keys change and old entries miss. No invalidation logic needed; note it so a rerun against a warm
  namespace is expected to re-author.
- **The `Stakes` rename** touches `autoprover-sdk` (`outcome.rs`), `crucible-app` (`app.rs`,
  `triage.rs`, `testkit.rs`) and `composer/rustapp` (`wire.py`, the one `session.py` call site). One
  consumer today, which is why it is cheap now and expensive after a second backend builds on the
  word.
- **Prompts and guidance**: `harness_cheat_sheet.j2`, `test_cheat_sheet.j2`, `author_component.j2`,
  `judge_guidance.j2` all describe the one-fn-per-section shape.
- **`check_syntax`** should require the per-check fn shape — it is the gate that can enforce the
  contract mechanically, before a campaign is spent.
- **Tests**: `layout.rs:238` pins the component grouping (`target_for(&unit, "c_fifo") ==
  Some("c_withdraw_queue")`) and inverts.
- **§8 of crucible.md** is rewritten around this; this document folds into it once shipped.

---

## 6. Acceptance

Re-run klend's `c_obligation_ownership_transfer` per-check against the delivered harness in
`klend-0819`. Two checks have a known-suspicious status — both reported `GOOD`, both declared
findings by the author on source reading:

- `borrow_order_can_be_armed_while_transfer_in_progress` — the author states the refuting sequence is
  `initiate → set_borrow_order`, and both actions exist in the fixture.
- `referrer_binding_transplanted_to_the_new_owner_by_ownership_transfer` — needs a completed
  three-step transfer.

Each must come back either **refuted with a witness** (the `GOOD` was a false negative and the
declared finding is confirmed) or **evaluated to budget and not refuted** (the declaration is wrong
and should be withdrawn). Today neither question can be answered, which is the defect.

Run-level: per-component refutation count should exceed 1 where the source supports it, and the
ratio of declared-without-counterexample findings should fall from 22/26.

---

## 7. Alternatives considered

**A second, coarser granularity for the authoring loop** (`Granularity::{Grouped, PerCheck}` chosen
by the host beside `Stakes`). Drafted, then rejected. Two grains mean two truths: the author would
iterate against grouped verdicts — the contaminated ones this proposal exists to remove — and only
the stamping run would be per-check, so checks green throughout authoring could flip to refuted at
the gate, discovered at the most expensive moment. That is the hazard the seam already warns about,
re-introduced on a second axis. It also saves less than it appears: the component feature has to
exist anyway for the §2.5 warm-up, so dropping the granularity removes a host-facing choice, not a
generated artifact. The authoring cost it was meant to solve is better handled by `Feedback`'s
budget and by focused validates (§3).

**Continue past a violation and collect a witness per check.** Make `record_violation` a map, keep
executing the remaining actions, suppress a tag once witnessed. Cheaper than this proposal and fixes
all three mechanisms of §1 at once. Rejected: it changes what a Crucible test *is* — a test that runs
until something is wrong — and that semantics is Crucible's, not ours to redefine from the harness we
generate.

**Report-only**: stop reporting `GOOD` for a check that shares a target with a refuted sibling;
report "not independently measured" instead. Correct as far as it goes and available immediately, but
it converts a false negative into an absence of information for every sibling of every finding.
Rejected as an end state; worth doing only if this proposal is deferred.

---

## 8. Open questions

- **Warm-up length.** How long the component-wide seeding campaign should run before the per-check
  fan-out — long enough to reach deep states, short enough not to dominate. Measure on klend.
- **`Feedback` budget.** 10s per check is a guess. It should be low enough that a full authoring
  validate stays in the low minutes and high enough that a green answer is worth anything.
- **Should the gate spend unevenly?** A check whose tally shows shallow evaluation may deserve more
  than one with millions of evaluations. The most principled option and the most work; not proposed
  here.
- **Does one fn per check cost anything at runtime?** Under per-check builds only one is compiled in,
  so there is no per-action multiplication — but the warm-up campaign compiles the component feature
  and does make N passes over the same state. Expected to be lost in the noise (a LiteSVM transaction
  dominates a pass over a handful of accounts; klend ran 273 exec/s at 7.1 actions/exec), but it
  should be measured.

---

## 9. What the implementation changed

Five things the plan above had wrong or did not know.

**The component feature is gone entirely**, not kept for the warm-up (§2.4 said it stays). Two
reasons converged: the **preflight entry already drives the real fixture with no assertions**, so it
*is* the exploration run — and an assertion-free campaign is strictly better at seeding, because
nothing truncates its inputs. And a component feature would have needed its entry to call every one
of its check fns, which breaks the moment the author declines a property and writes no fn for it. So
a component is now a module and nothing else.

**Corpus seeding was already there.** §2.5 proposed a warm-up feeding `--corpus-in`; in fact every
campaign already passes `--corpus-in CORPUS_DIR --corpus-out CORPUS_DIR` against one shared
directory, so per-check campaigns inherit whatever states earlier ones reached. The corpus entries
are action sequences against the same `Fixture`, so they are interchangeable across features. No
orchestration was needed and none was written.

**Check names are given to the author, not derived by them.** The plan assumed the check name was
`c_<property slug>`. It is not: the host takes check names verbatim from the author's `map_checks`,
and the prompt asked for `c_<property title>` — which coincides with the slug only when a title is
already an identifier. Under one campaign per component that mismatch was invisible; with a feature
per check it would have been a build failure. So each property now states its own fn name in the
prompt (from the slug, which the host guarantees is safe), and `triage::unbuildable` refuses a target
whose name is not one of them *before* a build is spent, naming the name that was expected.

**`CrateRootInput` gained `props`.** The crate root now names something per property, and that hook
re-emits byte-identically what the setup gate produced — so it needs the same property set that gate
was sent. One field, both sides.

**Naming caught up.** `feature_of` → `module_of` (a component names a module, not a feature),
`harness_fn` → `module_for`, and `SECTION_FN` — the single constant fn name the old contract turned
on — is deleted in favour of `CHECK_PREFIX`. That constant existed so the author could never name an
fn no build selects; what replaces it is the build itself, since the root generates an entry per
property that calls the fn by name and `E0425` names any the author did not write.

### Not done

The §6 acceptance test has **not** been run. It needs an expensive run, and the authoring-contract
change invalidates every cached spec, so `klend-0819` re-authors from the fixture down. Until it
does, this is verified by unit tests and by inspection of the generated crate, not by a campaign.
