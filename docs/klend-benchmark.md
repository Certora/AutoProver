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
- **The Prover crashed on 10 of 56 jobs**, each failing every rule the job carried. Eight were
  `TAC translation error: TACHeapAllocator ran out of memory of 32768 bytes` (two units), and two
  were `PointerDomain error: TODO(5): support sol_alloc_free` (two others). Both are internal
  Prover errors, reported to the author with the alert text, and each cost the author rounds
  routing around a code path rather than specifying it. What they are is below.
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

### What the failures turned out to be (2026-10-23)

Investigated in the Prover's source, and recorded as upstream defects P16 to P19 and T9
(`docs/upstream-defects.md`).

- **The heap crash is a translation-time limit, not program memory.** The Prover gives each rule a
  32 KB bump allocator over allocation *sites* in the inlined program, frees nothing, and charges
  512 bytes for an allocation of unknown size. Every `msg!` or `xmsg!` with arguments is one of
  those, and klend's program has 435 logging calls, many of them formatted. One crashed job's sources, rebuilt and resubmitted, failed
  as submitted and succeeded with `-solanaHeapSize 262144`, in 3.5 minutes (P17).
  Since [EVMVerifier#8773](https://github.com/Certora/EVMVerifier/pull/8773), on the Prover's master
  branch, the overflowing rule's error names the option and a size to pass.
- **One rule's crash failed the whole job.** The translator's internal errors escape the per-rule
  error handling, so a single overflowing rule discarded every verdict in its unit (P16). That,
  more than the crash itself, is what made each failure expensive. Fixed on the Prover's master
  branch, which AutoProver's jobs run, by [EVMVerifier#8771](https://github.com/Certora/EVMVerifier/pull/8771).
- **`sol_alloc_free` is the allocator on a current toolchain.** rustc 1.89 renames the allocator
  shims under `__rustc::`, the Prover does not recognize the realloc one, and AutoProver's starting
  inlining file matches none of them. The exact trigger is not pinned (P18, P19, T9). The Prover's
  master branch models the syscall and recognizes the renamed shims
  ([EVMVerifier#8776](https://github.com/Certora/EVMVerifier/pull/8776),
  [EVMVerifier#8777](https://github.com/Certora/EVMVerifier/pull/8777)), and the inlining file now
  accepts the prefix.
- **With the heap fixed, the next walls are visible.** The resubmitted job's rules ran: 16 verified,
  7 violated (3 of them loop-unwinding checks at `loop_iter` 2), and 7 per-rule errors. Six of those
  are `[3001] access to an indeterminate stack location` in the `uint` crate under `Fraction`'s
  wide-integer arithmetic, and one asks for `-solanaStackSize 16384`.
- **The long jobs are probably AutoProver's own limits.** The submitted conf carries
  `global_timeout` 1500 and `smt_timeout` 6000, which the Prover turns into a 25-to-30-minute
  ceiling. Not yet investigated.

### What to try in AutoProver before the next run

Each of these is a change to AutoProver, not to the input, so the benchmark stays a measurement of
what AutoProver does with klend as published.

1. **Pass `-solanaHeapSize`** (262144 measured) in the backend's default prover args
   (`composer/spec/cvlr/conf.py`). It widens only the address range allocations are drawn from, so
   it costs no soundness, and it removed the crash outright. Done.
2. **Fix the allocator directives** in `cvlr_inlining_core.txt`: accept the `__rustc::` prefix and
   add `__rust_realloc` (T9). Done. The Prover's master branch keeps the analysis out of the System
   allocator by itself now, so the directives are a second line.
3. **Find out what the long jobs are.** Read the timed-out jobs' outputs for which rules hit the
   ceiling and why. Then decide whether the ceiling should move, whether those rules should be
   split, or whether the author should be told to restate them, before raising any limit. Now U40.
4. **Consider `-solanaStackSize 16384` as the default**, after checking it does not slow the vault
   and stake fixtures, since raising it changes the stack model for every target.
5. **Let a run order, or budget, its authoring units.** Formalization is first-in first-out, four at
   a time, and the pool is shared, so whatever comes first takes the budget. Either a per-unit share
   of the pool or an order the run can be given would put the money where the measurement is. For
   this benchmark the answer-key units would go first. That is a choice about where to spend, not a
   hint about what to find, so it does not touch what is measured. Now U39.
6. **Make logging free for the Prover where the author asks.** A verification-only edit that makes
   the program's logging macros expand to nothing removes every formatting allocation along with
   the strings it builds. With the heap raised it is no longer needed to avoid the crash, but it
   shrinks what the Prover translates, and it is a munge the editor can already make. Worth a
   recipe if the heap change alone leaves translation slow. Run 2b showed it is: now U38.
8. **Share the dependency cache between runs.** Every confined run downloads about 1.5 GB of
   klend's dependencies up to three times, roughly 7 minutes each time: package selection,
   preflight and the shared build tree each have their own cargo home. One home per run would
   remove two of those reads, and a cache shared between runs most of the third. Recorded as U35
   in [cvlr-todo.md](./cvlr-todo.md).

**Not on this list: the `Fraction` abstraction.** The `[3001]` errors are in exactly the arithmetic
the published report approximates with `FixedU64<14>`. Telling AutoProver to do that would hand it
the answer to grading criterion 4, whether it finds the seams the report names. If the next run
stalls on `[3001]`, that is the measurement, and it gets reported as one.

**Then: a second run, not a resumption of run 1.** The formalization phase has changed a great
deal since run 1, and a resume would restore run 1's partial drafts into the new harness layout. The
cache key does not see code changes, so nothing would refuse it. Instead, run 1's analysis and
extraction are written out with `--pin-to` (replayed from its cache, at no cost) and run 2 starts
at formalization from that file with `--properties`, on a fresh copy of the input and a new cache
namespace. The file keeps only the components the answer key's properties live in: obligation
lifecycle, collateral, borrowing and liquidations (P-01 to P-03, P-06 to P-10), and reserve
deposits and withdrawals (P-04, P-05). That is item 5 by selection rather than by a new option: it
chooses where to spend, not what to find. Hoisting runs again over those five, so the protocol-wide
unit is rebuilt from them.

## Run 2 (2026-10-28): stopped on a silent Prover gap

Started as planned above, with a $500 budget. Hoisting folded 59 of the five units' properties into
23 protocol-wide ones for about $1.50. The first four Prover jobs ran without the heap or allocator
crashes; the first finished in about two minutes. Each of the three that finished reported
`__lshrti3`, the u128 logical right shift, as neither inlined nor summarized. Its result is written through memory,
so the program's unsigned fixed-point arithmetic was being checked against values the call never
wrote, while the jobs succeeded (upstream defect P20). The run was stopped after about $47, before
any unit finished, because verdicts over that arithmetic would not have been gradable. P20 is fixed
on the Prover's master branch by
[EVMVerifier#8792](https://github.com/Certora/EVMVerifier/pull/8792). The next run is run 2 again,
with a new cache namespace.

## Run 2b (2026-10-29): stable, but not yet a measurement

The same plan, restarted after P20's fix. It ran 11h 50m and used the full $500.

- **The Prover held.** Of 87 jobs, 75 succeeded, 6 halted at the 30-minute ceiling, and 1 failed
  with no reason in its output. None crashed, against 10 of 56 crashes in run 1.
- **One unit of six was delivered**, reserve deposits and withdrawals: 16 rules, 13 verified, over a
  domain the author bounded (amounts below 2^32) because the full-width conversions timed out. Four
  units were cut by the budget as unverified partials. The liquidation unit started with $0.91, as the
  answer-key units did in run 1.
- **The delivered unit's three findings did not survive triage.** Each was a violated rule over a
  function below the handler, from a pre-state the program does not produce. None is a defect, and
  none matches a published one.
- **Prover time set the pace.** It came to 24 hours across 75 calls, and units spent most of their
  wall clock waiting.

The run was not resumed. More budget would buy more rules of the same shape. The next run waits on
what it showed to be missing, recorded in [cvlr-todo.md](./cvlr-todo.md):

- U36: checking a finding's pre-state for reachability;
- U37: a recipe for inductive invariants;
- U38: a switch that makes logging and error formatting inert;
- U39: per-unit budgets or a run order;
- U40: what the long Prover jobs are, with U32.

Nothing is graded against P-01 to P-10 yet.

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
