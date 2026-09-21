# Pinned runs

Fixtures for `--properties` (see `composer/pipeline/pinned.py`): a recorded analysis plus the
properties extracted from it, so a run can re-enter the pipeline at formalization without paying
for the two phases that dominate a real target's cost.

**These are expected to be temporary.** They exist because the CVLR backend is not yet reliable
enough to reach formalization cheaply on a real program; once it is, a gate can afford to run the
whole pipeline and these files should go.

## `spl-stake-pool.pin.json`

| | |
|---|---|
| target | `Certora/solana-program-stake-pool-audit`, baseline branch |
| commit | `22834f8fee10484e8393a1be7a051035193ed312` |
| taken from | run 6, 2026-09-04, thread `cvlr_c30779839d90` |
| contents | 10 components, 219 properties |

Recovered from the langgraph checkpoint database rather than written by `--pin-to`, because the
writer did not exist when the run happened. Two things make the reconstruction trustworthy rather
than plausible, and both are worth repeating if another fixture is ever recovered the same way:

* **The component mapping is computed, not guessed.** Checkpoint threads are keyed by a digest, and
  `composer.pipeline.keys.component_digest` is the function the pipeline itself keys them with, so
  the digest→component mapping is the pipeline's own. All ten matched.
* **The property order was checked against ground truth.** Properties arrive per bug-analysis
  round and the final list is their union; taking first-seen order across rounds reproduces
  *exactly* the file the artifact store wrote independently for `Pool_Initialization`
  (`cvlr_pool_initialization.properties.json`, the first 10 under the run's `--max-properties 10`
  cap). The union also totals 219, which is the count the run logged. Order matters because
  `--max-properties` selects a prefix.

### Which component to pin

`Pool_Initialization` is the wrong default despite being component 0. The normative hand-written
verification of this program (`certora-cvlr-kb`, project `stake-pool`, spec branch
`certora-ci/sep-2025`) has 23 rules and **verifies no Initialize rule at all**, and a probe
confirmed why: with the accounts unconstrained, no successful `Initialize` execution exists in the
Prover's model, so every success-path rule comes back `SANITY_FAILED`. A gate pinned there can fail
for reasons that are not our fault, which is the opposite of what a gate is for.

`Admin_Fee_Configuration` is the better target: it covers `set_fee`, which the normative
verification does cover, so its rules have a known-good counterpart to be read against.

## `vault.pin.json` and `vault-deposits.pin.json`

| | |
|---|---|
| target | `test_scenarios/solana_vault_idl`, in this repo |
| taken from | run of 2026-09-18, thread `cvlr_d71e0ec8c99f` |
| contents | `vault.pin.json`: 3 components, 30 properties. `vault-deposits.pin.json`: the `Deposits` component alone, 9 properties |

They exist for one experiment: whether `optimistic_loop` earns its place as a default
([cvlr-todo.md](../../../docs/cvlr-todo.md) U9). That run's `Deposits` unit could not discharge an
unwinding condition at any bound and gave up the handler, so the comparison is the same nine
properties formalized twice, with the assumption off and on. The trimmed pin is the cheap half of
it: the two components it drops took 1h09 and 1h24 of formalization between them.

Trimmed by deleting a key from `properties`, which is all a narrower pin is. Narrowing *within* a
component is possible the same way and was deliberately not done — the author's strategy responds to
the whole batch it is given, and a unit handed two properties may never attempt the handler at all,
which is the thing being measured.

### Why these cannot be regenerated, only recovered

Recovered from the run's checkpoints after the original file was lost, by the method above, with the
same two guards: the digest→component mapping is `composer.pipeline.keys.component_digest`'s, and
every pinned property is accounted for by what the unit did with it — 9 = 6 published + 3 skipped,
9 = 4 + 5, 12 = 10 + 2, against the `property_rules` and `skipped` the three formalization threads
recorded. The rebuilt file also came out at the byte size of the original.

**Re-running `--pin-to` does not reproduce a pin, and the attempt is what established it.** The same
scenario, the same document, the same flags, three days later: the analysis returned **two**
components — one of them `Vault Lifecycle & Deposits` — where the pinned run returned three. A fresh
pin is a different decomposition over different properties, so a comparison against one measures the
re-decomposition as much as the thing under test. This is the concrete form of the warning at the top
of this file, and it is why these are checked in rather than treated as reproducible.

### The `optimistic_loop` probe run off this pin

Four submissions of **one** rule, to answer [cvlr-todo.md](../../../docs/cvlr-todo.md) U9 without
paying for a pipeline run. The harness is the one the pinned `Deposits` run delivered; the rule is
the handler rule that run could not state, appended by hand:

```rust
#[rule]
pub fn rule_probe_deposit_handler_credits_exactly_amount() {
    deposit_env!(accounts, ix, bumps);          // the harness's own validated-Deposit macro
    let amount: u64 = nondet();
    cvlr_assume!(cvlr::is_u64(amount));
    let before = ix.vault.balance;
    clog!(before, amount);

    let ctx = Context::new(&crate::ID, &mut ix, &[], bumps);
    crate::vault_program::deposit(ctx, amount).unwrap();

    let after = ix.vault.balance;
    clog!(after);
    cvlr_assert!(NativeInt::from(after) == NativeInt::from(before) + NativeInt::from(amount));
}
```

Everything else — the summaries file, `rule_sanity`, the solver flags, the build script — is the
delivered conf unchanged. Only `loop_iter` and `optimistic_loop` move, plus `rule`.

| `loop_iter` | `optimistic_loop` | rule | verdict | job |
|---|---|---|---|---|
| 2 | false | assertion | VIOLATED — *Unwinding condition in a loop* | [e189209b](https://prover.certora.com/output/37632/e189209b18574daf8e43b76379d07152?anonymousKey=60bdeadf32512dfed0512ebea8f9f826bffd252d) |
| 8 | false | assertion | VIOLATED — identical trace, advice now says "higher than 8" | [8becab60](https://prover.certora.com/output/37632/8becab609cad469fbf4d1e5f9862717c?anonymousKey=3147ecd6da20f3a496332663f4d48aa25b02a8ee) |
| 2 | **true** | assertion | **VERIFIED** | [4675b617](https://prover.certora.com/output/37632/4675b617bdb74f8bbc71fca2032ab706?anonymousKey=8845c4958ed2dc876bdd5a26cf0e62f5a81c5caf) |
| 2 | true | `cvlr_satisfy!(true)` in place of the assertion | VERIFIED — the path is live, so the row above is not a vacuity | [662f673b](https://prover.certora.com/output/37632/662f673b62f54a7cb9b649297f28217c?anonymousKey=65990366cd7d52f0fb88dc83d1cf89482fa740c9) |

The satisfy row is the one not to drop if this is ever re-run: without it the VERIFIED above is
indistinguishable from a dead path, and a dead path is the likelier explanation a reader will reach
for. U9 carries what the four rows mean.

Submitted with `composer.certora_env.import_prover_entry("solana")` from the pinned run's own
`.cvlr_work/build`, which already holds the compiled workspace and the generated summaries — no
pipeline, no model calls, about 20 seconds of prover time each.
