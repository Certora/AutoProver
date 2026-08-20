# Proposal: per-check fuzz targets

**Status:** proposal. **Affects:** `rust/crucible-app` (targets, generation, authoring contract),
`composer/rustapp` (one new wire field, one call site). **Supersedes** the grouping rule in
[crucible.md §8](./crucible.md).

A Crucible component today is one campaign covering all of its checks. This proposes making the
*check* the unit a campaign runs, with the granularity chosen by the host — the same way
`Exploration` is already chosen by the host.

---

## 1. The problem

The wire contract makes a promise the wheel cannot keep. `Exploration::ToBudget` is documented as

> Explore every covered check to the full budget, whatever is found on the way.

and [crucible.md §8](./crucible.md) spells out what the wheel does with it: when a finding lands on
one of the target's own checks, "that check `BAD`; the rest stay `GOOD` (subject to the tally)".

The rest do not stay `GOOD` on their own evidence. They stay `GOOD` because nothing refuted them in
a campaign whose exploration was shaped by a *different* check. Three mechanisms, none of which the
tally detects:

1. **An input stops at its first violation.** The harness runs the remaining actions not at all —
   `2 executed, 4 skipped ... (stopped on violation)`. Any state reachable only *after* the
   violating action is unreachable for every check in the target.
2. **The refuting input leaves the corpus.** It becomes an objective, so the prefix that reached
   the interesting state stops being mutated on behalf of anything else.
3. **`record_violation` is first-wins.** Within one invariant pass, only the source-order-first
   violated check is recorded; simultaneous violations of later checks are dropped silently.

The tally does not catch this because it counts *evaluations*, not evaluations *in states where the
check could fail*. A check evaluated 65,536 times in shallow states looks identical to one that was
genuinely exercised.

### Evidence (klend, 2026-08-20, cache namespace `klend-0819`)

235 checks across 13 components — 13 campaigns. I re-ran one component's target with 10× the time
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

---

## 2. What changes

Five parts. (1) and (2) are the substance; the rest follow.

### 2.1 Checks become individually addressable

Today a section is one authored fn holding every check as inline `fuzz_assert!` calls. The only
handle on an individual check is the `[tag]` string inside its assertion message — which is also
how verdicts are attributed. There is no way to build a target containing a subset.

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

### 2.2 Granularity joins Exploration on the wire

`Exploration` exists because how far a run must explore "follows from what the host will do with its
answer, which the wheel cannot see". Granularity is the same decision on a second axis, and belongs
in the same place:

```python
class Granularity(str, Enum):
    """How finely one invocation of the checker may be split — set from what the host will do with
    the answer, as `Exploration` is.

    A campaign covering many checks is cheaper while the author iterates and wrong once the verdicts
    are reported: the checks share one corpus, and the exploration that a refuted check shapes is
    not exploration on behalf of the rest."""

    #: One invocation per unit — the backend's natural grouping. What the author iterates against.
    GROUPED = "grouped"
    #: One invocation per check. Each check gets the whole budget and its own corpus.
    PER_CHECK = "per_check"
```

It travels the way `Exploration` does — chosen by the host, carried on `Target`, and passed to
`target_for`, whose signature already takes the check name:

```rust
fn target_for(&self, input: &AuthorInput, check: &str, grain: Granularity) -> Option<String> {
    let Authored::Component { .. } = input.authored else { return None };
    match grain {
        Granularity::Grouped => Some(harness_fn(input)),
        Granularity::PerCheck => None,   // None already means "its own target"
    }
}
```

`Check.target = None` already means the check is its own target, and `targets_of()` already
partitions on that. **The host needs no other change** — one new field, threaded through the single
`target_for` call site in `declared_checks`.

### 2.3 Who asks for what

| caller | Granularity | Exploration | why |
|---|---|---|---|
| `validate_spec` while authoring | `GROUPED` | `UNTIL_FIRST_FINDING` | fast feedback; the author is iterating |
| the stamping run that publishes verdicts | `PER_CHECK` | `TO_BUDGET` | a published `GOOD` must be the check's own evidence |

This keeps the authoring loop at today's cost. Only the run whose verdicts reach the report pays for
isolation — which is exactly the split `Exploration` already draws, and for the same reason.

### 2.4 Generation

Both shapes are generated; only one feature is ever enabled per build, as now. `Cargo.toml` gains a
feature per check alongside the per-component ones, and the root emits a per-check entry that shares
the component module:

```rust
#[cfg(any(feature = "c_component", feature = "c_check_x", feature = "c_check_y"))]
mod c_component;

#[cfg(feature = "c_check_x")]
#[invariant_test]
fn c_check_x(f: &mut Fixture) { c_component::c_check_x(f) }
```

Templates touched: `cargo_toml.j2`, `root_layout.j2`, `section_entry.j2`, `section_file.j2`.

### 2.5 Corpus seeding recovers what isolation costs

Per-check targets lose something real: a deep state that one check's mutations discovered is no
longer available to its siblings. Recover it by running the component's `GROUPED` campaign first as
a warm-up and seeding every per-check campaign from its corpus:

```
crucible run <program> c_component   --corpus-out <ns>/corpus/c_component --timeout <warmup>
crucible run <program> c_check_x     --corpus-in  <ns>/corpus/c_component --corpus-out <ns>/corpus/c_check_x ...
```

`crucible run` already has `--corpus-in` / `--corpus-out`, and §8 already routes a shared corpus
through `.certora_internal/crucible/corpus`. Each per-check campaign starts from states the fixture
is known to reach instead of from scratch — and unlike today, it then explores *on behalf of its own
check*.

---

## 3. Cost

Per-check campaigns at klend's scale (235 checks, 13 components, 60s budget):

| | builds | fuzz time (serial) | at 8-way |
|---|---|---|---|
| today | 13 | ~13 min | — |
| per-check | 235 | ~4 h | ~30 min |

Incremental rebuild of the root with deps cached is ~4s, so the build cost is ~20 min and
parallelises. The authoring loop is unaffected (it stays `GROUPED`).

This is 10–20× today's *gate* cost, and it buys the thing the contract already promises. If that
proves too expensive, the tunable is the per-check budget, not the granularity — a check with 20s of
its own attention is still measured; a check sharing a campaign is not.

An alternative that avoids the cost is discussed in §6.

---

## 4. What it changes downstream

- **`GOOD` becomes meaningful.** Today it can mean "not refuted in a campaign steered by another
  check". After, it means "this check had a full budget and its own corpus and was not refuted".
- **Declared findings should fall.** The prediction is testable: several of klend's 22
  source-reading declarations should either become real counterexamples or become honest
  `UNKNOWN`s. Either outcome is better than a finding with no evidence.
- **Attribution simplifies.** The §8 table's first row collapses: a `PER_CHECK` target has one
  check, so "the rest" does not exist. The cross-component and unknown-title rows stay as they are.
- **Coverage debt gets visible.** A check whose own campaign never evaluated its assertion is
  unambiguously not-exercised, with nothing to blame it on.

---

## 5. Migration

- **Cached specs go stale.** The authoring contract changes, so the author prompt changes, so cache
  keys change and old entries miss. No invalidation logic needed; state it in the changelog so a
  rerun against a warm namespace is expected to re-author.
- **Prompts and guidance**: `harness_cheat_sheet.j2`, `test_cheat_sheet.j2`, `author_component.j2`,
  `judge_guidance.j2` all describe the one-fn-per-section shape.
- **`check_syntax`** must accept (and ideally require) the per-check fn shape — it is the gate that
  can enforce the contract mechanically, before a campaign is spent.
- **Tests**: `layout.rs:238` pins the component grouping (`target_for(&unit, "c_fifo") ==
  Some("c_withdraw_queue")`) and becomes a `Granularity::Grouped` case, with a `PerCheck` sibling.
- **§8 of crucible.md** is rewritten around this; this document folds into it once shipped.

---

## 6. Acceptance

Re-run klend's `c_obligation_ownership_transfer` component per-check against the delivered harness
in `klend-0819`. Two checks have a known-suspicious status — both reported `GOOD`, both declared as
findings by the author on source reading:

- `borrow_order_can_be_armed_while_transfer_in_progress` — the author states the refuting sequence is
  `initiate → set_borrow_order`, and both actions exist in the fixture.
- `referrer_binding_transplanted_to_the_new_owner_by_ownership_transfer` — needs a completed
  three-step transfer.

Each must come back either **refuted with a witness** (the current `GOOD` was a false negative, and
the declared finding is confirmed) or **evaluated to budget and not refuted** (the declaration is
wrong and should be withdrawn). Today neither question can be answered, which is the defect.

Run-level metric: per-component refutation count should exceed 1 where the source supports it, and
the ratio of declared-without-counterexample findings should fall from 22/26.

---

## 7. Alternatives considered

**Continue past a violation and collect a witness per check.** Make `record_violation` a map, keep
executing the remaining actions, suppress a tag once witnessed. Cheaper than this proposal and fixes
all three mechanisms at once. **Rejected**: it changes what a Crucible test *is* — a test that runs
until something is wrong — and that semantics is Crucible's, not ours to redefine from the harness
we generate.

**Report-only fix**: stop reporting `GOOD` for a check that shares a target with a refuted sibling;
report "not independently measured". Correct as far as it goes and available immediately, but it
converts a false negative into an absence of information for every sibling of every finding.
**Rejected as an end state** — it describes the limitation instead of removing it. Worth doing only
if this proposal is deferred.

**One target per check, always** (no `GROUPED`). Simpler — one code path, no granularity field. But
it makes every authoring `validate_spec` N campaigns, and the author calls it repeatedly per
component. The authoring loop is where wall-clock is already the binding constraint.

---

## 8. Open questions

- **Budget policy at scale.** 235 checks × full budget is a real bill. Should the host scale the
  per-check budget by check count, cap the total, or spend more on checks whose tally shows shallow
  evaluation? The last is the most principled and the most work.
- **Warm-up length.** How long the `GROUPED` seeding campaign should run before the per-check fan-out
  — long enough to reach deep states, short enough not to dominate. Measure on klend.
- **Does `GROUPED` regress the authoring loop?** One fn per check means N passes over the same state
  per action instead of one. Expected to be lost in the noise — a LiteSVM transaction dominates a
  pass over a handful of accounts by orders of magnitude (klend ran 273 exec/s at 7.1 actions/exec)
  — but it should be measured, not assumed.
