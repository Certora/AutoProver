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
