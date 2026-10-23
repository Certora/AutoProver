# Benchmarking the backend against klend's hand-written spec

A third benchmark for the CVLR backend, to answer a question the other two cannot: on a large
production program, how close does a generated spec get to one that experts wrote by hand?

The vault tutorial (`docs/autoprover-benchmark-2026-10-02.md`) has a hand-written spec, but it is small,
and it is the shape the corpus is made of. Stake (`docs/stake-benchmark.md`) was chosen to be distant
from the corpus, and it has no hand-written spec. klend has both size and a hand-written spec. The
measure is **parity**: does AutoProver state the properties the experts stated, prove them, and need
the same abstractions to do it?

**This is a public document.** It states only what Kamino and Certora have published about the
verification. Every fact about the hand-written spec below comes from the public sources listed at
the end. The checkout, the input commit, and any comparison against parts of the spec that have not
been published are kept in private notes, not here.

## The program

klend (Kamino Lend, [`Kamino-Finance/klend`](https://github.com/Kamino-Finance/klend)): an Anchor 0.29
lending program with 63 instructions and ~36k lines of program source. It has reserves with cToken
collateral, multi-asset obligations, liquidation and auto-deleverage, elevation groups, a withdraw
queue, borrow orders, and fixed-term borrowing with rollover. It is the largest target the backend
has been pointed at.

Its arithmetic is the main difficulty. Values are `Fraction`, a 128-bit fixed-point type from the
`fixed` crate. The interesting properties, solvency and share value, are nonlinear in it.

## The answer key

Certora formally verified Kamino Lend with the Certora Prover. Kamino lists the verification on its
[Formal Verification](https://kamino.com/docs/security/formal-verification) page and publishes the
[report](https://github.com/Kamino-Finance/audits/blob/main/kamino_lend_certora.pdf) (February 2025,
scope `programs/klend/src/*` at commit `5af6cfc`). The report states ten properties, each with its
rules, statuses and assumptions:

| | property |
|---|---|
| P-01 | A healthy account cannot become unhealthy by user operation |
| P-02 | An account with no collateral must have no borrows |
| P-03 | A healthy user cannot lose collateral |
| P-04 | Vault solvency: deposited and withdrawn liquidity cover a reserve's collateral shares |
| P-05 | Reserve solvency: a reserve's total assets cover its collateral shares |
| P-06 | The supply/collateral ratio decreases only on liquidation of bad debt (`socialize_loss`) |
| P-07 | Liquidations of healthy positions must revert |
| P-08 | Integrity of the flow of funds in liquidation |
| P-09 | Liquidation reduces LTV |
| P-10 | Liquidation is profitable |

P-01 to P-05 are inductive invariants. Each has a base case (for example `init_obligation`) and one
step rule per operation, so a single property appears as many rules.

The report also states the abstractions the verification needed. These are what the abstraction
grade below compares against:

- **`Fraction` approximated** by a narrower fixed-point type (`FixedU64<14>` in place of the
  128-bit, 60-fractional-bit original), for tractability, while still catching rounding errors.
- **Loops unrolled twice.** Obligations are bounded to two deposits and one borrow, and account
  vectors to size 2.
- **`calculate_liquidation` replaced** by one that returns arbitrary values (an over-approximation).
  The elevation-group debt trackers and `post_deposit_obligation_invariants` are replaced with
  empty functions. Asset-tier lookups return empty.

The report lists findings Kamino **acknowledged** rather than fixed. A generated counterexample at
one of those sites is a correct result, not a fixture bug.

**The answer key is older than the input.** The report covers code from February 2025, and the
input (below) is from mid-2026. Features added since then (borrow orders, the withdraw queue,
rollover) were reviewed in Certora's later
[audit reports](https://github.com/Kamino-Finance/audits), but those reports publish no formal
properties. In this document, those features are graded only as *properties outside the answer key*.

## The input

The input is unmodified klend: a development snapshot at program version 1.23.0, between public
releases 1.23.0 and 1.24.0. It is on `solana-program ~1.17.18`, Anchor 0.29 from crates.io, and
`fixed` 1.23.1, with Rust 1.74.1 per `rust-toolchain.toml`. It contains no verification code. Any
munge, mock or build change the Prover needs is AutoProver's to make.

**The design document** (`docs/klend-protocol.md` in the benchmark checkout, ~144 KB) is built only
from Kamino's own documentation. It has 15 pages of kamino.com/docs: borrowing concepts,
liquidations, fees and the interest-rate model, fixed rates and rollover, borrow orders, the
withdrawal queue and withdrawal caps, the reserve, risk and market parameter references, and the
glossary. It also has the repository's own `docs/liquidations.md`.

**The documentation must contain nothing from the verification, or the measurement is void.** The
following are excluded:

- kamino.com's *Formal Verification* page. It restates the proved invariants, the answer key's
  headline properties.
- kamino.com's *Security Audits* page.
- The published reports, and Certora's
  [blog post](https://www.certora.com/blog/securing-kamino-lending) on the verification.
- An earlier klend design document used by the Crucible runs. It is of unknown authorship.

The web pages were fetched on 2026-10-22, and the code is older than that. A generated property
about a feature the code does not have yet is a documentation-skew artifact, not an authoring
error.

## Platform support: klend needed a generation AutoProver did not have

AutoProver supports two platform generations, `solana-program` 2.x and the split 3.x crates
(`docs/cvlr-sdk3-support.md`). klend links `solana-program` 1.17. `select_generation` finds the
`solana-program` witness on the wrong major version, and the scaffold refuses the project with
*move the project to solana-program 2.2*. That is the right refusal, and it is not doable for klend.

The nearest CVLR line is `cvlr-solana` 0.4.x, which requires `solana-program ^1.18`. A probe
build established that this works: klend moved to 1.18, with `cvlr-solana` 0.4.5 on the shared
`cvlr` 0.6.1 core and the Anchor and `fixed` forks, builds with platform tools v1.53. The plan, and
the probe, are in `docs/cvlr-solana-1-18-support.md`. All three of what the benchmark needed are now
in place:

1. **A 1.18 generation in AutoProver** (G1–G5, G7 there), on `cvlr-solana` 0.4.6.
2. **A compile gate that can compile klend** (G6). No host toolchain can: from rustc 1.77 on, the
   `farms` dependency's account-size assertions fail on x86_64's `u128` alignment, and klend's own
   1.74.1 predates the lockfile format current cargo writes. The gate now checks the SBF target.
3. **A 1.17 → 1.18 move of the input.** A 1.17 program cannot be built with v1.53 at all
   (`solana-frozen-abi` 1.17 pins an `ahash` that current rustc rejects). This is a
   build-infrastructure change, not spec authoring. It is applied by hand as one labelled commit,
   and the results report that AutoProver was given it. The same commit takes out of the workspace
   a member that is not part of the program and depends on a git repository the public cannot
   read.

Whether the platform move is in scope for AutoProver, or a precondition given to it, makes these
different experiments, like stake's bincode question. **Decided: a precondition.**

**The 1.18 line has no SPL Token CPI mocks.** A token CPI left alone reaches
`invoke_signed_unchecked`, which the core summaries make opaque. Every property in the answer key is
about klend's own accounting, so that does not affect parity. A generated property about token
balances needs a munge that calls `cvlr-solana` 0.4's own token summaries in place of the CPI, and
it is triaged with the other properties outside the answer key.

## Run 1 (2026-10-22): the budget ran out before the answer key's ground

**Not gradable for parity.** No unit that covers P-01 to P-10 was authored. What the run did
produce, and why it stopped there, is below; the grading in the next section waits for a run that
reaches those units.

**Configuration.** `console-solana` on the input above (with the 1.18 precondition commit), the
design document as `system_doc`, a $600 run budget (`--budget-total 600`, no per-phase caps), a
cache namespace so the run can be resumed, and the defaults otherwise: 4 concurrent agents,
confined builds, both CVLR corpora.

**What it cost.** 8h49m wall clock. The run's own accounting reached the $600 limit; the per-call
log lines price to about $537, so they read about 10% low here.

| Phase | Wall clock | Spend (from the logs) |
|---|---|---|
| Start-up: two cold `cargo metadata` reads | ~10 min | — |
| System analysis | 20m | $7 |
| Property extraction, 13 components | ~2h | $145 |
| Property hoisting | 7m | (in extraction) |
| Formalization, 14 units | ~6.5h | ~$385 |

Analysis split the program into 13 components: market and reserve administration, fees, reserve
liquidity, refresh and oracle pricing, obligation lifecycle, collateral, borrowing, liquidations,
flash loans, fixed-rate orders and rollover, the withdrawal queue, and obligation self-service.
Extraction stated 196 properties. Hoisting moved 138 of them into a protocol-wide unit as 36
properties, 17 of them new. Preflight selected the 1.18 generation, scaffolded both forks, and
passed the SBF compile gate in 27s.

**What it produced.**

| Unit | Properties | Outcome | Rules |
|---|---|---|---|
| Protocol & Referral Fees | 12 | delivered | 15 |
| Reserve Admin & Lifecycle | 21 | delivered | 22 |
| Market Admin & Configuration | 15 | partial, unverified | 22 |
| Protocol invariants | 36 | partial, unverified | 16 |
| Reserve Liquidity Deposits & Withdrawals | 11 | partial, unverified | 4 |
| Reserve Refresh / Oracle Pricing | 9 | partial, unverified | 2 |
| The other 8 units, among them obligation lifecycle, collateral, borrowing and liquidations | 92 | started after the budget was spent | 0 |

The nearest the run came to the answer key is the protocol-wide unit's partial harness: deposit and
redeem accounting, exchange-rate preservation across fee withdrawals, flash-loan supply neutrality.
That is P-04 to P-06 territory, but unverified, so it is not graded.

**Why the budget ran out.**

- **Units were slow and expensive.** The four that ran first took 3.75 to 6 hours and $55 to $126
  each, against under an hour per unit on the vault tutorial. Prover jobs on klend often ran 20
  minutes or more.
- **The Prover crashed on 10 of 56 jobs.** Nearly all were `TAC translation error: TACHeapAllocator
  ran out of memory of 32768 bytes`, across several units: klend's handlers allocate more than the
  Prover's modelled heap. One was `PointerDomain error: TODO(5): support sol_alloc_free`. Both are
  internal Prover errors, reported to the author with the alert text, and each cost the author
  rounds routing around a code path rather than specifying it.
- **The queue is first-in, first-out, four at a time.** Formalization starts units in component
  order (the protocol-wide unit first) and starts the next only when one finishes. The answer-key
  units were seventh to tenth. They started after the pool was spent and ended at once, about $1
  each. A per-unit share of the budget, or an order the run can be given, would have spent the
  money where the measurement is.

**Also seen.** The Prover reported Anchor's account validation (`AccountLoader::try_accounts`,
`Signer::try_accounts`, `Sysvar::from_account_info`) as neither inlined nor summarized, so treated as
external. A rule that passes over a handler does not, by that, cover klend's account checks, and
grading a handler-level property has to say whether its answer depends on them.

**Infrastructure this run needed.** Cold `cargo metadata` of klend downloads about 1.5 GB (git
dependencies dominate) and took 435s, over the 300s limit preflight then had; the limit is now
900s. A confined run reads into a fresh per-run cargo home, so each run pays this download two or
three times at start-up.

**Next.**

1. Find whether the Prover's modelled heap can be raised, and report both crashes. Every unit gets
   cheaper if the heap crash goes.
2. Resume the run (same cache namespace, so analysis and extraction are not paid again) with a
   larger budget. Getting the answer-key units (obligation lifecycle, collateral, borrowing,
   liquidations) authored before the budget is spent needs the run to be told their order, which it
   cannot be yet. From what the first units cost, each needs $60 to $120.
3. Then grade against P-01 to P-10 as below.

## Grading it

Decided before the run, so the result stays falsifiable:

1. **Property coverage** against P-01 to P-10, scored per *property*, not per rule. The inductive
   properties expand into one rule per operation, so rule counts would let either spec win on
   bookkeeping. Each property gets one grade: **matched**, **weaker** (same intent, narrower: fewer
   operations, extra assumptions), **stronger**, or **absent**.
2. **Verdict agreement** on matched properties. The report records each one as verified, some
   after a fix. A generated counterexample on a matched property is a fixture or munge defect, a
   regression since February 2025, or an acknowledged finding. Triage says which.
3. **Non-vacuity.** Rules must clear `rule_sanity`. An abstraction whose contract is never checked
   counts against the generated spec, even if every rule over it passes.
4. **Abstractions.** Did the run find the seams the report names: narrowing or replacing
   `Fraction`, over-approximating `calculate_liquidation`, and bounding obligations and loops? Of
   everything here, this is the most informative measurement. It is stake's "does it find the
   bincode seam unprompted" question, on a target with a published answer.
5. **Properties outside the answer key**, including everything about the features added since
   February 2025. Triage each as real, vacuous, or wrong. They are neither credited nor penalized
   automatically. A real one with no counterpart in the answer key is the best result this
   benchmark can produce.
6. **Cost and wall-clock**, reported with the phase breakdown, as for stake.

The Crucible runs on klend (2026-08) are not a baseline. They used a different snapshot, a
different backend and a different design document.

## Public sources

- Kamino, [Formal Verification](https://kamino.com/docs/security/formal-verification) and
  [Security Audits](https://kamino.com/docs/security/audits).
- Certora, [Kamino Lending Security Assessment & Formal Verification Report](https://github.com/Kamino-Finance/audits/blob/main/kamino_lend_certora.pdf)
  (February 2025; also at [certora.com](https://certora.com/reports/kamino-lending-security-report)).
- Certora's later klend audit reports, `kamino_lend_certora_1.13.0.pdf` through `_1.17.0.pdf` in
  [`Kamino-Finance/audits`](https://github.com/Kamino-Finance/audits).
- Certora, [Securing Kamino Lending](https://www.certora.com/blog/securing-kamino-lending)
  (March 2025).
- klend's `security.txt` (`programs/klend/src/lib.rs`), which lists Certora among the auditors.
