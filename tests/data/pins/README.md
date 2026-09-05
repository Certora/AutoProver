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
