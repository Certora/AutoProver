# Benchmarking the backend against Jupiter Lend's published verification

A fourth benchmark for the CVLR backend. Like klend (`docs/klend-benchmark.md`), it asks how close a
generated spec gets to one experts wrote, on a production lending program with a published formal
verification. Unlike klend, the program is small enough that one prioritized run reaches its core
property, so this benchmark is mostly about **depth**: on the property that matters most, does
AutoProver state it the way the experts did, prove it as generally, and miss nothing they found?

**This is a public document.** It states only what Jupiter, Fluid and Certora have published, and
what anyone can read in the public source. Every fact about the expert verification below comes
from the public reports listed at the end. Triage of the counterexamples the run produced is kept
in private notes, not here.

## The program

The **Liquidity** program of Jupiter Lend (powered by Fluid):
[`Instadapp/fluid-solana-programs`](https://github.com/Instadapp/fluid-solana-programs),
`programs/liquidity`. It is the single pool per token that Jupiter Lend's Lending (fTokens), Vaults
(collateralized borrowing) and Flashloan programs supply to and borrow from, through cross-program
calls. It is an Anchor 0.32 program, `solana-program` 2.3, with 24 instructions and about 4,500 lines
of source.

Its center is one instruction, `operate`, which supplies, withdraws, borrows and pays back in a single
call for one protocol and one token. Around it sit interest accrual through two exchange prices,
with-interest and interest-free positions on each side, utilization and a kinked rate curve,
withdrawal limits and debt ceilings that expand over time, payouts to claim accounts, and revenue
collection. The arithmetic is u128 with a u256 multiply-divide, not a fixed-point type, which makes
it far more tractable than klend.

The other programs in the repository were out of scope. Liquidity was chosen because its published
properties are the protocol's core accounting, and it calls no other Jupiter Lend program.

## The answer key

Certora formally verified Jupiter Lend twice. Jupiter publishes both reports in its docs repository
([`jup-ag/docs`](https://github.com/jup-ag/docs/tree/main/static/files/audits)):

- `lend-certora-fv.pdf`: *Formal Verification, Jupiter Lend*, September–November 2025 (work through
  December 1st), at commit `7069f55`.
- `lend-certora-fv-2.pdf`: the same title, January–March 2026, at commit `b4927b6`.

For Liquidity both reports state the same plan. The central property is a solvency invariant over
each token reserve:

```
TokenTotalSupply + TokenTotalClaim <= VaultBalance + TokenTotalBorrow
```

Supply and borrow are each the interest-free total plus the with-interest raw total times its
exchange price, all scaled by 1e12. Around it:

| Report 1 | Report 2 | Property | Rules |
|---|---|---|---|
| P-01 | P-10 | **Solvency invariant**, preserved by `operate` (deposit, withdraw, borrow, payback), `update_exchange_price`, `claim` and `collect_revenue` | `liquidity_solvency_operate_{deposit,withdraw,borrow,payback}`, `liquidity_price_update_only_with_interest` (with lemmas), `liquidity_solvency_claim`, `liquidity_solvency_collect_revenue` |
| P-02 | P-11 | **Last-utilization invariant**: the stored utilization is the one the totals give, over the four `operate` paths and the price update | `liquidity_operate_{deposit,withdraw,borrow,payback}_utilization`, `liquidity_price_update_utilization` |
| P-03 | P-12 | **Operate integrity**: each total is the sum of the positions in its mode (P-3a supply, P-3b borrow), and the total claim amount is the sum of user claims (P-3c) | `operate_integrity_deposit_or_withdraw`, `operate_integrity_borrow_or_payback`, `operate_borrow_claim_integrity`, `operate_withdraw_claim_integrity` |

The published assumptions are what the abstraction grade below compares against:

- Over `operate`, prices are assumed already updated in that slot. The price update is proved
  separately.
- For the price update, interest-free supply and borrow are assumed to be zero, *"justified because
  only the Flashloan contract is set without interest"*, which repays within the transaction.
- `operate`'s amounts are taken as i64 rather than i128.
- The rate model's `calc_borrow_rate_from_utilization` is replaced by one returning arbitrary values.

**Findings in the answer key that touch Liquidity.** Report 1's M-03 (borrow price rounded down),
M-06 (utilization divided over rounded values) and M-08 (`calc_revenue` rounds total supply down)
are all fixed in the benchmarked code, as the source shows. Report 2's **M-04** is not. It is a
unit mismatch: the ratio of with-interest to interest-free supply compares raw (price-deflated)
amounts with face-value ones, which over-pays with-interest suppliers once prices move away from
1.0. The report records it as fixed in PR #168, after the benchmarked release. A correct generated
spec should therefore find M-04, outside the published assumption that interest-free amounts are
zero.

## The input

The input is public release 0.1.2 of `fluid-solana-programs` (`626b177`), published as
[`Certora/fluid-autoprover-snapshot`](https://github.com/Certora/fluid-autoprover-snapshot) at
`185d7ad`, one commit with two changes:

- **A design document**, `docs/liquidity-design.md` (~20 KB). It is built only from public Jupiter
  and Fluid documentation: Jupiter's developer and user docs for Lend, Fluid's public EVM
  documentation and specs for the Liquidity layer (the design is shared), and the repository's own
  README. Where the Solana program differs from the EVM design, the document says so, checked
  against the code. It ends with the guarantees the public docs say the design makes.
- **The README's audit link removed.** It led to the folder that holds the two Certora reports above.

Nothing else the run could read mentions the reports: not the design document, the code or the
README. The input contains no verification code.

## Platform: a workspace that resolves Anchor twice

The first cloud run stopped in preflight. Every program in the workspace pins `anchor-lang` 0.32.0,
but `pyth-solana-receiver-sdk`, which the `oracle` and `vaults` programs use, pulls in 0.31.1. The
scaffold redirects Anchor to Certora's fork through `[patch.crates-io]`, and it refused any fork crate
the workspace resolved more than once, since one patch entry replaces one release. It did not
check whether the target linked both copies. Liquidity links only 0.32.0.

The fix (31c81906) counts only the copies linked into the target. If the target links one copy, that
version is redirected and the others stay on crates.io, because cargo applies a patch only where its
version meets the requirement. The other copies are reported as not linked. A target that links two
copies still blocks. On this workspace the redirected graph has 0.31.1 from crates.io and 0.32.0 from
the fork.

The failure reached the user as a failed run with no reason. Only the executor log had it (U55).

## Run (2026-11-05): the solvency component, delivered

| | |
|---|---|
| Mode | prioritized |
| Wall clock | 10h 54m (preflight 2m, analysis 4m, extraction 6m, formalization 10h 40m) |
| Components / properties extracted | 8 / 99 |
| Pursued | **Operate & Inflow Verification**, 11 properties (88 deprioritized) |
| Prover jobs | 20, 8h 32m of Prover time; 6 halted at the 25-minute ceiling |
| Delivered | 53 rules in one spec file, one verification-only source edit |
| Spend | about $58 at list prices (Opus 5.5 1.07M output, 55.8M cache reads, 4.7M cache writes) |

**The ranking pick was right.** The ranker chose the component whose claim reads "the pool stays
solvent through operate: vault plus borrower debt covers supply and claims, because only real inflows
are credited, totals track positions, …". That is the answer key's P-01 and P-03. On klend's first
prioritized run (2026-11-04), the pick was a narrow four-property claim.

**AutoProver's invariant is the published one.** Its solvency predicate is, term for term, the
reports' P-01: face-value supply plus claims on one side, vault balance plus face-value borrow on the
other, scaled by 1e12 with no division. It was not given the reports. Its design document has no
solvency statement; the nearest is the revenue formula (vault balance + total borrow − claims −
total supply), which has the same terms.

**Six halts, five in a row, then a quiet narrowing.** Six jobs halted: the 9th, then the 12th to
the 16th in a row. The rules cut off were `operate` rules, among them payback tracking and
withdraw-and-borrow in one call, including the case that merges both payouts into one transfer.
Each halted job still returned the verdicts that finished (U32), and the Prover's note said to split
or simplify the rules that timed out. The author edited them in place for about 2.5 hours. It ended
the halts by deleting the three withdraw-and-borrow rules; the payback rule later verified. Their
properties still mapped to other rules, so nothing was reported skipped, and the report reads as
complete with that path unverified (U53, U54).

**The source edit.** The u256 multiply-divide in `calculate_exchange_prices` and the SPL transfers
were routed through wrappers under the unit's feature and replaced: the multiply-divide by an exact
`floor(a·b/c)` stand-in (its loops do not unroll at `loop_iter` 2), the transfers by a recorder. The
published verification checks the u256 library separately (report 2, P-09). AutoProver does not, and
says so in the report.

## Grading it

The same scheme as klend.

### 1. Property coverage against the published Liquidity properties

| Published property | Grade | AutoProver |
|---|---|---|
| P-01 solvency over `operate` deposit / withdraw / borrow / payback | **weaker** | `rule_solvency_operate_*`, split by position mode (with-interest / interest-free). As published, prices are assumed already accrued. Beyond that, the prices are pinned at 1.25 and 1.5, amounts are bounded (below 2^32 or 2^40, totals below 2^50), and a position is assumed no larger than its bucket's total. |
| P-01 solvency over `claim` | matched | `rule_claim_moves_total_and_balance_together` |
| P-01 solvency over `collect_revenue` | matched | `rule_solvency_collect_revenue`, stated over `calc_revenue` rather than the handler |
| P-01 solvency over `update_exchange_price` | **weaker** | Under the published assumption (no interest-free amounts), proved only at concrete pools: raw with-interest supply 1, 1e6 or 1e12, borrow rate 100%, fee 10%, one day elapsed, prices at 1.0. The reports state no restriction on prices, rate, fee or elapsed time beyond the interest-free one. |
| P-02 last-utilization invariant | **weaker** | `rule_operate_stores_utilization_and_rate`: one `operate` path (deposit), same second; also checks the stored borrow rate. The published property covers four paths and the price update. |
| P-03a/b totals equal the sum of positions | matched | As per-operation deltas: `rule_operate_{deposit,payback}_total_tracks_position`, four `rule_total_*_update` rules, and two rules that the delta lands in the position's mode bucket |
| P-03c total claims equal the sum of user claims | matched | `rule_operate_{withdraw,borrow}_claim_bookkeeping`, `rule_claim_sum_invariant_*` |

Nothing published is absent. Two properties are weaker on domain, and one on the paths it covers.

### 2. Verdict agreement

Every rule listed in the table above verifies, as the published rules do.

### 3. M-04: missed

The benchmarked code has M-04, and the run did not find it. Every accrual rule assumed both
exchange prices at their initial value of 1.0, stated as a bound for tractability. At 1.0 a raw
amount equals its face value, so the mismatch cannot show. The `operate` rules pinned prices away
from 1.0, but they run in the same second as the last accrual, so they never reach the ratio. The
published assumption (no interest-free amounts) also excludes M-04; report 2 records it against its
`liquidity_price_update` rule. Lifting the price pin is the missing step (U52).

### 4. Non-vacuity

Not yet measured for this run.

### 5. Abstractions, against the published ones

| Published | AutoProver |
|---|---|
| Prices updated before `operate` | Same, in that second |
| Interest-free amounts zero for the price update | The same restriction, in the with-interest-only accrual rules |
| `operate` amounts as i64 | Bounded tighter (2^32 / 2^40) |
| Rate model returns arbitrary values | A flat 4% rate in most `operate` rules, and the real model in the utilization rule |
| (none published) | Withdrawal limit and debt-ceiling expansion turned off (`neutralize`), so no rule bounds what the limits allow |
| u256 multiply-divide verified separately (P-09) | Replaced by an exact stand-in, not verified |
| (not stated) | Accounts built by hand, so no Anchor constraint (`has_one`, vault ATA, the lock flag) is exercised |

### 6. Properties outside the answer key

All verified:

- **Inflow:** deposits and paybacks are credited only for tokens that arrived after a `pre_operate`
  snapshot by the same protocol in the same second, for each and combined.
- **Payouts:** a direct payout moves exactly the amount; a claim payout credits only the claim
  account; a payout never exceeds the vault's unclaimed balance and leaves the vault covering
  outstanding claims; a claim account closes only at zero.
- **Rounding direction** per operation (deposit floor, withdraw ceiling, borrow ceiling, payback
  floor), and **round trips** that cannot profit (deposit then withdraw, borrow then pay back).
- **Solvency over interest-mode changes** (`update_user_supply_config` and
  `update_user_borrow_config` switching a position's mode). The published plan reasons that admin
  functions leave the solvency terms unchanged, but a mode change moves amounts between buckets.
- **`operate` uses fresh state:** it stores the accrued prices, the timestamp, the utilization and
  the rate it computes.

The 88 deprioritized properties (withdrawal limits and debt ceilings, the rate model, auth and
guardian controls, configuration, exchange-price monotonicity) were not attempted, by design of the
mode.

### 7. Cost and wall clock

About $58 and 10h 54m, of which 10h 40m was one formalization unit and 8h 32m Prover time. Roughly a
quarter of the Prover time went to the five consecutive halted jobs.

## What came out of it

U51 to U55 in [cvlr-todo.md](./cvlr-todo.md):

- **U52** — a domain pin that makes two quantities coincide (a price at its precision) hides unit
  bugs. M-04 is the example.
- **U53** — repeated halts on the same rules should force the author to isolate, narrow or account
  for them.
- **U54** — a rule dropped after it reached the Prover should be accounted for, or reported as
  dropped.
- **U55** — a preflight refusal should reach the cloud UI.
- **U51** — a finding's severity should say whether it is reachable under the deployed
  configuration as well as under the code.

The Anchor double-resolution fix (31c81906) shipped during the benchmark.

## Public sources

- Jupiter Lend source: [`Instadapp/fluid-solana-programs`](https://github.com/Instadapp/fluid-solana-programs),
  release 0.1.2 (`626b177`); the benchmark input,
  [`Certora/fluid-autoprover-snapshot`](https://github.com/Certora/fluid-autoprover-snapshot).
- Certora, *Formal Verification, Jupiter Lend*, September–November 2025, and January–March 2026:
  `lend-certora-fv.pdf` and `lend-certora-fv-2.pdf` in
  [`jup-ag/docs/static/files/audits`](https://github.com/jup-ag/docs/tree/main/static/files/audits).
- Jupiter developer docs for Lend, [developers.jup.ag/docs/lend](https://developers.jup.ag/docs/lend),
  and user docs, [docs.jup.ag](https://docs.jup.ag/user-docs/earn/lend/liquidity-layer-and-risk-management).
- Fluid's EVM Liquidity specs, [`Instadapp/fluid-contracts-public`](https://github.com/Instadapp/fluid-contracts-public)
  (`contracts/liquidity/SPEC.md`, `contracts/libraries/SPEC-liquidityCalcs.md`), and
  [docs.fluid.instadapp.io](https://docs.fluid.instadapp.io).
