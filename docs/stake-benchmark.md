# Benchmarking the backend against the Solana stake program

A second benchmark for the CVLR backend, chosen to answer a question the first one cannot: have the
authoring prompts and the munge vocabulary overfit to the projects they were derived from?

The first benchmark is the public Certora vault tutorial — a real program with a hand-written spec,
so a generated spec can be diffed against it. That measures fidelity. It does not measure
generalization, because a vault that issues shares against a token balance is the shape the corpus
is made of. This one is chosen for distance instead.

## The program

[`solana-program/stake`](https://github.com/solana-program/stake) — the Solana stake program, as
maintained by Anza for the Core BPF migration. Public, Apache-2.0, ~2,600 lines of program source
across 18 instructions.

Four things make it distant from the corpus:

- **Native lamports, not SPL token balances.** No token account model applies.
- **An enum state machine** (`StakeStateV2`), not share accounting over a struct.
- **bincode**, not borsh derives and not `bytemuck`. A survey of six local CVLR projects found six
  serialization seams (borsh derives, accessor functions, Anchor loaders) and bincode among none.
- **Sysvars throughout** — `Clock`, `StakeHistory`, `Rent`.

The state seam is `get_stake_state` / `set_stake_state`, free functions in `program/src/processor.rs`
wrapping `bincode::serialize_into` and `deserialize_data`. Free functions are what `mock_fn` reaches,
so the seam is the tractable kind. **Whether the authoring loop finds it unprompted is the single
most informative measurement this benchmark offers**, and it is unanswerable on a target whose state
model is already supplied by a chain crate.

## Grading it

There is no hand-written spec, which is the point — a specced program has an answer key. What gets
scored instead, decided before the run so the result stays falsifiable:

1. **Machinery survival** — scaffold, munge selection, compile gate, submission, on an unseen shape.
2. **Non-vacuity** — rules that clear `rule_sanity` rather than verifying trivially.
3. **A rubric written in advance** from the documented protocol semantics: lamport conservation
   across `Split`/`Merge`/`MoveStake`/`MoveLamports`; `delegation.stake + rent_exempt_reserve <=
   lamports`; staker-versus-withdrawer authority separation; lockup enforcement on `Withdraw`.
4. **Seeded mutants** — flip the rent-exemption check in `Split`, drop a withdrawer check, and see
   whether the generated spec catches it.

One methodology decision has to be made before the run, not after: whether munging the bincode seam
by hand is in scope, or is part of what is being measured. Those are different experiments.

## The blocker: the CVLR line is behind the Solana SDK

> **Resolved 2026-10-14.** AutoProver supports both platform generations, and the benchmark has
> moved to stake `main`. See *Moving the benchmark to stake `main`* at the end of this document,
> and `docs/cvlr-sdk3-support.md`.

`plan_scaffold` refuses current `main` outright:

```
BLOCKED Cargo.toml: this project builds solana-account-info 3.1.1, but the CVLR releases the
reference set names are bound to solana-program 2.x (the last monolithic line) — and each
generation has its own AccountInfo type, so the pairing does not compile rather than merely
warning.
```

This is [`_check_platform`](../composer/spec/cvlr/scaffold.py) doing its job — one of the two cases
`docs/cvlr-backend-plan.md` §7.4.1 records as wanting a human rather than a model, because it is a
decision about how the project builds for everyone. A plan carrying a `Blocked` applies nothing.

The block names two resolutions. **Only one of them exists.** `cvlr-solana` 0.5.0's own manifest
declares `solana-program = "2.2"`, and 0.5.0 is the newest release on crates.io (2026-01-16), so
"pin the CVLR line that matches 3.1.1" is not available.

**This is not a stake-specific problem.** `solana-program/token-wrap`, the other live Foundation
program shortlisted for this slot, pins `solana-account-info = "3.1.1"` and would produce the same
refusal. The corpus never hit it because every project in it is on `solana-program` 2.x or 1.18.
Pointing the backend at any current Foundation program is what surfaced it, and it will block the
next one too.

## The workaround, and what it costs

> **Retired 2026-10-14.** The three runs recorded below used this snapshot, and their results stand
> as recorded. Later runs are on `main`, which is different code, so a comparison across the move
> is not like for like.

Check out the last commit before the program moved to SDK 3.0:

| | |
|---|---|
| local checkout | `~/src/solana-program-stake`, branch `sdk-2.2` |
| commit | `ff864a9` (2025-10-03), the parent of `cf77d09 program: bump deps to SDK 3.0 (#88)` |
| platform | `solana-program 2.2.1` — inside the reference set's generation |
| toolchain | 1.86.0, per that commit's `rust-toolchain.toml` (`main` pins 1.93.0) |

The scaffold plan is clean at that commit — no `Blocked`, twelve path aliases in force, and the
`invoke_signed_unchecked` dual spelling that only appears on 2.2 where `solana-program` still
defines its own alongside `solana-cpi`.

What the snapshot costs, to be stated in any write-up of the results:

- It is roughly a year and several hundred commits behind `main`.
- `solana-stake-interface` is a registry dependency at this commit rather than a workspace member,
  so the plan skips the feature forwarding that `main` would need. `StakeStateV2` therefore cannot
  be munged — it is not local code. The seam functions are, so this does not block the experiment.

All 18 instructions are present and the `Split`/`Merge`/`Withdraw` semantics under test have not
moved, so the benchmark measures what it is meant to. It is a snapshot, not current `main`, and
saying so is part of reporting the number.

## The first run, 2026-09-23

Stopped deliberately at **~$57 of a $75 ceiling**, mid-authoring. `--cache-ns stake-benchmark`
holds the analysis and all 94 extracted properties, so those are not re-paid for. The drafts were a
different story — see *Recovering the drafts* below.

| | |
|---|---|
| components identified / authored | 8 / 4 |
| properties extracted | 94 (Split+Merge 36, Delegation 28, Authorization 19, Init 11) |
| CVLR authored | 2,570 lines |
| prover jobs | 7 succeeded |
| LLM calls | 429 |

**The generalization question is answered, positively.** The backend handled a program shape the
corpus does not contain, and solved two obstacles nothing taught it:

- **Reachability.** Stake's handlers are *private associated functions* on `impl Processor`. Every
  corpus program exposes free `pub fn process_deposit(...)`, which a rule calls directly; these
  cannot be called at all. The editor reached for `extract_function` — the kind whose charter is
  exactly *"split so `X` is a function a rule can drive"* — keeping the original behind
  `cfg(not(feature = "unit_x"))` and adding a `pub` inner under the unit's feature.
- **bincode.** It did not mock the seam. It worked out that `StakeStateV2` is bincode-encoded with a
  leading little-endian `u32` tag and constrained the raw account bytes through `data_len()` and
  that tag — reaching the state machine *through* the encoding rather than replacing the serializer.
  Whether that is better than a `mock_fn` on `get_stake_state`/`set_stake_state` is open; it is
  certainly not what the corpus would predict.

Component decomposition tracked where invariants actually live rather than splitting evenly:
Split+Merge drew 36 properties, `MoveStake`/`MoveLamports` got their own unit, and the two thin
units (`deprecated_instructions` for the unimplemented `Redelegate`, `protocol_parameter_queries`
for `GetMinimumDelegation`) correctly drew none.

**The rule-quality question is answered negatively on first pass.** Rules compiled, built for SBF
and submitted; verdicts came back largely `SANITY_FAILED` — vacuous. The judge's diagnosis is worth
keeping: it identified `Pubkey` equality on SBF (`sol_memcmp_`) as the likely unconstrained
primitive, noted that the shared `assume_stake_shape` helper carries the same suspect assumption so
the *passing* rules may be passing for the wrong reason, and refused the laundering fix —
*"summarising them would produce a green rule that checked nothing"*. That is P5's failure mode
caught in the act, by the loop rather than by a human.

**Three plumbing defects, which is what an unseen target is for.** Two are fixed; one is not.

1. **`cargo --package <name>` is ambiguous** when the program under verification is also a published
   crate that its own dev-dependencies pull back in. Fixed — the compile checks now name the package
   by manifest path.
2. **A dangling symlink kills the run after extraction is paid for.** `program/tests/fixtures/*.so`
   is a git-tracked symlink the project's own test build populates; `SharedTree.materialize()` calls
   `shutil.copytree` without `symlinks=True`, dereferences it, and raises — at the first step of
   formalization, with every extraction agent already billed. **Not fixed**; the call is in the
   graphcore submodule. Worked around by populating the link's target.
3. **Withholding a specialization had no control.** Fixed — see below.

## Open, and not worked around

**The SDK 3.x gap.** *Closed 2026-10-14; see the last section.* A `cvlr-solana` release targeting the post-split platform generation unblocks
every current Foundation program at once. Until there is one, this workaround is the only route and
it expires as targets age out. This belongs with whoever owns CVLR releases.

**The dangling-symlink crash** (item 2 above) is unfixed and will bite the next target that ships
one. `symlinks=True` — or skipping links that do not resolve — is the fix, in graphcore.

**`cvlr-solana-stake` is withheld from this target, and that now has a control.** The crate ships
572 lines implementing `process_withdraw`, `process_split`, `process_merge`, `process_authorize`,
`process_delegate` and `process_deactivate` — a behavioral model of the exact instructions the
benchmark asks the backend to specify. For any other target it is dependency modeling, which is what
a chain specialization is for; here it is an answer key.
`ChainReference.withholding` narrows the reference set per run and `--withhold-crate` exposes it;
the runs recorded above used it, and the crate is absent from both manifests and from the resolved
graph. Narrowing is per-crate by necessity: `cvlr-spl-token` sits in the same tuple, and dropping
specializations wholesale would cost the token model on a target that needs it.

Excluding the crate removes the mechanical leak, not the semantic one — it is published on
crates.io, and the stake program's invariants are documented protocol semantics. That residue is
acceptable for this question, which is about whether the prompts transfer, not about what a base
model knows.

## Recovering the drafts

The first attempt to continue this run was wrong twice over, and both corrections are worth keeping.

**Rejoining the checkpointed thread does not work.** Passing the previous `thread_id` replays the
unit's message history into a fresh graph, which fails two ways: `author.py`'s `assert
res_state["failed"] is not None` holds for a graph that just ran and not for a resumed terminal one,
and the replayed history violates the API's rule about where system messages may sit. Eight units,
$2.12, no progress. The CVL author does not do this — it runs a *fresh* graph and seeds `curr_spec`
from a cache entry written in a `finally`. Porting that is what the CVLR author now does.

**The cache write was silently suppressed.** `cache_put` drops writes made under budget pressure, so
a rushed result is never served to a later run as finished. But `_remember_attempt` runs in a
`finally` reached by `BudgetExceeded`, where pressure is total — so the carry-forward wrote nothing
on the one path it exists for. A resume buffer is the exception to that guard and now says so
(`CacheKey(..., survives_budget_pressure=True)`).

**What was recoverable anyway.** The checkpointer is durable Postgres, so this run's drafts were
never actually lost — only unreachable. `ap-trail recover-drafts` walks them back into the cache:

| | on disk (`.unverified`) | in the checkpoints |
|---|---|---|
| Splitting & Merging | *nothing* | 2,185 lines, 53 rules |
| Authorization & Lockup | 471 lines | 1,769 lines, 50 rules |
| Delegation lifecycle | 275 lines | 1,371 lines, 32 rules |
| Account Initialization | 543 lines | 819 lines, 15 rules |
| four thin units | ~30 lines | ~30 lines |
| **total** | **1,317 lines** | **6,176 lines, 150 rules** |

Two things that gap is made of. The largest unit never reached disk at all. And a unit told to wrap
up deletes every rule whose verdict it never saw — which under a budget cut is most of them, so its
final draft understates its work; the delegation unit finished at 276 lines with no rules from a
peak of 1,371 with 32. `--draft largest` recovers the high-water mark for that reason, and is what
was used here: **6,176 lines seeded** for the next run.

## The second run, 2026-09-24

Resumed from the recovered drafts with a **$300** ceiling; spent **$282.89** and ended
`RUN FAILED: every component failed to generate or gave up` — every one of the eight units
curtailed by budget. 18 prover jobs (16 succeeded, 2 failed), 75 counterexamples with judge
analyses, and **12 rules** in the delivered harnesses.

**The resume mechanism itself worked.** Analysis and all 147 properties came back from cache in
seven seconds for $0, and each unit opened its buffer at exactly its recovered size — 2,185 /
1,769 / 1,371 / 819 lines. The unit thread ids were byte-identical to the previous run's, which is
what made the backfilled cache keys line up. Munges are deliberately *not* restored, so each unit
spent its first 10–40 minutes re-establishing them through the editor before anything compiled.

Three defects dominated the result. Two are ours and are fixed or filed; the third is the
interesting one.

### The prompt cache was set to 5 minutes, under a workload that waits hours

`builder_heavy()` in the author carried a comment reading *"long cache: a prover run can take many
minutes, and the author's context should still be warm on the other side of one"* — and never
passed `cache_level`, so it took the 5-minute default. Every prover wait evicted the context and
the next call re-wrote all of it:

```
$2.40  in=326,013  cache_read=0  cache_write=326,011  out=14,609  formalize-2
```

**122 calls cost $173 of the $283 — 61% of the budget in 15% of the calls.** Fixed; the author now
asks for `CacheLevel.LONG`, and a structural test pins the argument, because what went wrong was a
keyword quietly absent from one call while the comment beside it claimed otherwise.

The waits that caused it were real: one job ran **100 minutes** and another 66, against a median of
under two.

### Four units never got to start

The pipeline kept dispatching queued units after the run pool was exhausted. Withdrawals, Stake &
Lamport Movement, Deprecated Instructions and Protocol Parameter Queries each began, met the wrap-up
order immediately and finished in about thirty seconds having authored nothing — **63 properties
recorded as skipped for budget exhaustion without a line written**. The final report then counts
them as failed components beside the four that did real work. Filed as [[U21]].

### What the surviving units published, and why the rule counts are low

| unit | at its peak | as delivered |
|---|---|---|
| Delegation & Activation | 1,380 lines, 32 rules | 330 lines, 0 rules |
| Splitting & Merging | 2,185 lines, 53 rules | 482 lines, 1 rule |
| Authorization & Lockup | 1,769 lines, 50 rules | 59 lines, 0 rules |
| Account Initialization | 1,097 lines, 23 rules | 706 lines, 11 rules |

Those collapses are **not** budget damage, and reading them as such is a mistake this document made
in an earlier revision. Each unit withdrew its rules deliberately, on measurement, and said so:

> *Delegation:* "every rule that was written and run either failed for a reason traced to the
> harness rather than to the program, came back vacuous, or came back as a pointer-analysis error
> with no verdict at all… Nothing here should be read as evidence for or against any property of the
> stake program, and in particular none of the counterexamples seen is a finding."

> *Authorization & Lockup:* "No rules are published for this unit… This is a negative result, and
> the reason is specific and reproducible."

> *Splitting & Merging:* "One verified rule, and thirty-five recorded skips… the skips carry the
> substance: each names the specific mechanism that blocked it, the measurement that established it,
> and the next step."

Thirty-six of thirty-six properties accounted for in that last one. A loop that spends its prover
budget establishing *why* it cannot soundly specify a program, and then declines to publish rules it
cannot stand behind, is behaving correctly — that is P5's failure mode being avoided, not incurred.

### What the run actually established

Not rule counts. The agents spent their prover budget on **diagnostics**, which is what
[the previous run's judge](#open-and-not-worked-around) said a resumed run should do first, and the
answer came back.

The standing hypothesis was that `Pubkey` equality on SBF (`sol_memcmp_`) was the unconstrained
primitive behind the vacuity cluster. `rule_diag_pubkey_eq_stable` tested it directly by drawing two
nondet `Pubkey`s and checking three things at once. The witness reports `eq1 = 1, eq2 = 1, weq = 0,
rt = 0`:

- **Native `Pubkey` equality is stable and repeatable.** `eq1 == eq2` held. The hypothesis is
  **not** supported.
- The failure is in the *harness's own* four-`u64`-word decomposition. `rt = 0` says
  `words_pk(pk_words(&p)) != p` for a single key — the round trip does not hold.

The judge's reading: `pk_words` reassembles thirty-two individual byte loads through
`u64::from_le_bytes`, and *"the prover cannot relate the bytes written by `to_le_bytes` to the bytes
read back by `from_le_bytes` through that `[u8; 32]` buffer — the byte-level view of the key and the
aggregate view of the key are, to the analysis, two unrelated objects."* Once the round trip breaks,
the word comparison is four comparisons over unconstrained values and may disagree with the native
one freely.

Two things follow. The byte-decomposition idiom is unsound under this prover and should not be
reached for — that belongs in the authoring guidance. And the loop diagnosed a defect **it had
introduced itself** in the current revision, and said so rather than filing it against the program.

### Three modelling limits, each measured rather than guessed

The units' own reports carry the substance, and each names the measurement behind it.

**`HashSet<Pubkey>` is not soundly modelled.** The program builds the effective signer set as a
`std::collections::HashSet<Pubkey>` in `collect_signers`, and tests membership inside
`Authorized::authorize`, `Authorized::check` and `Meta::set_lockup` — all `solana-program` code, out
of reach of any munge. Program-free probes stopped on *"Unwinding condition in a loop"* inside
`hashbrown::raw::RawTable::reserve_rehash`. The unit walked the loop ladder properly: rung 1 does not
apply (the trip counts are `Pubkey`'s 32-byte width and hashbrown's group stride, with no
rule-visible length to bound), rung 2 does not apply (the loops are exactly the code the properties
are about, so a summary would havoc the answer), so it raised `loop_iter` to 6 — and the outcome got
*worse*, the reported iteration tracking the bound, which is the ladder's own signal for a trip count
the analysis cannot pin. Its own `for account in accounts` loop discharged at 2. Every one of the 19
Authorization & Lockup properties depends on signer-set membership, which is why that unit publishes
nothing.

**`bincode`-decoding `StakeStateV2` from a symbolic buffer is not soundly modelled.** A rule that
decoded an account and compared the decoded value *to itself* came back VIOLATED. This is why
`processor::get_stake_state` and `set_stake_state` were replaced by stand-ins — the previous run's
choice to reason *through* the encoding rather than mock the seam is, on this evidence, the weaker
of the two options.

**Copying a large aggregate through a `static mut` by value is imprecise** — a store/load round trip
of the modelled state, with no program call in between, did not hold.

Against those, one positive: **`rule_probe_pubkey_stable` VERIFIED** — a `Pubkey` equality read in
place out of account memory *is* modelled soundly. Taken with `rule_diag_pubkey_eq_stable` above,
the byte-decomposition is the defect and native equality is not.

The `rule_probe_hashset_*` and `rule_diag_signer_set*` rules came back *violated on an assertion the
prover generated* — the Prover's own internal assertion, not a property failure — which is the same
container limit seen from the other side. `rule_diag_signer_set_membership` pinned the longest job of
*both* runs, 100 minutes here.

### A note on the resume cache

The run ended with the cache holding its final drafts (1,609 lines, 12 rules), overwriting the 6,176
lines seeded that morning. Those final drafts are the considered ones, so this is not damage — but
the write is unconditional, so a run that achieves nothing and dies would degrade the cache the same
way. Filed as [[U22]]. `ap-trail recover-drafts --draft largest` restored 6,463 lines and 158 rules
from the checkpoints.

**What a resumed run should do first.** The vacuity is the live question, and the judge already
named the experiment: add `clog!`s to the owner comparison so the counterexample is readable, and
pin the owner to a concrete distinct key rather than assuming a disequality over an opaque 32-byte
value. Do that before authoring anything new — a second batch of rules built on the same
`assume_stake_shape` would inherit the same weakness.

## Expert review of the third run, 2026-10-12

The third run (2026-10-01/02, written up in the stake repo as
`docs/autoprover-benchmark-2026-10-02.md`) published 7 of 8 components: 111 rules, 69 of 119
properties covered, 50 skipped. Certora's Solana specialists reviewed the generated spec. They
raised three points, and each one holds up against the run's own record.

### 1. Rent and Clock are standard, and the spec rewrote the program to avoid them

> AP says it doesn't know how to handle Rent and Clock syscalls, and because of this, it does
> heavy rewriting of the client. To the point where lots of rules are mainly about the rewritten
> code itself and not the client code. … One can look at related SF projects (P-Token, Stake
> Pool) to see how we handle both.

**The cause is our tuning file, not the Prover.** The generated `cvlr_inlining.txt` never-inlines
every `solana-*` crate the program links: `solana_sysvar`, `solana_rent`, `solana_clock` and
`solana_stake_interface` among them. That blanket is AutoProver's. `644bf82f` widened the upstream
`^solana_program::.*$` to cover the crates the monolith split into, and `a18a7637` narrowed it to
the linked crates. The allowlist exempts `Clock::get`, `Rent::get` and `Rent::minimum_balance`,
but nothing exempts `solana_sysvar::Sysvar::from_account_info`. That default trait method is how
the handlers read both sysvars (`Clock::from_account_info(clock_info)?`), so the Prover treats it
as external. It returns an unconstrained `Result` and writes nothing, so a rule that reaches it
measured as never returning `Ok`.

The units then did what they had to. They split the handlers with `extract_function` so the
sysvar arrived as a parameter, and the rules entered below the handlers. That is the rewriting the
reviewers saw: 15 of the delivered munges, across six of the seven components, exist to move a
`Clock` or `Rent` read out of the way. Thirteen skips, also across six components, cite
`Sysvar::from_account_info` by name.

**What practitioners run.** Stake Pool is on the same platform generation (`solana-program`
2.2.1), and its `cvlr_inlining_core.txt` still carries only the upstream blanket. That blanket
matches almost nothing after the split, so `solana_sysvar` and `solana_rent` are analyzed. Its
specs call `Clock::get()`, `Rent::get()` and `Clock::from_account_info` directly and tie the
account to the syscall with one assume (`cvlr_initialize_rent_account`). P-Token does the same
with Pinocchio's `Rent`, and pins the parameters to mainnet defaults
(`cvlr_assume_rent_defaults`).

**One real limit remains.** `solana_rent::Rent::minimum_balance` computes in `f64`, and the
float builtins are summarized as `r0:num`, so two calls with the same length can disagree. The
practitioners' answer is to read `meta.rent_exempt_reserve` from state rather than recompute it.
The split unit's integer stand-in for `Rent` was a reasonable answer to a real problem; the
`from_account_info` workaround was not needed.

**The same blanket explains most of the other skips.** `Authorized::check`,
`Authorized::authorize`, `Meta::set_lockup`, `Lockup::is_in_force` and
`Delegation::stake_activating_and_deactivating` are all in `solana_stake_interface`, which the
blanket also made external. 29 of the 50 skips cite that crate, including 14 of Authorization &
Lockup's 15, and the skipped staking math (point 2).

### 2. The specs describe the code, not the design

> A good spec is one that holds for an entire class of implementations. … Solvency is a good spec
> because all protocols must remain solvent. But rules that check so and so fields are set
> correctly, or the program reverts under some condition are not very interesting. … Solvency is
> attempted, but only partially. Properties about math of staking and de-staking was skipped,
> because AP said it believed the certora prover cannot handle that (I am not so sure).

Three causes, in order of weight.

- **Extraction is per component.** Properties are extracted per component, and the components are
  instruction groups. A protocol-wide invariant has no single component to belong to. Solvency is
  one: every stake account keeps `lamports >= rent_exempt_reserve + delegation.stake` after every
  instruction. Lamport conservation across all eighteen instructions is another. Each component
  sees a slice of it, which is the "partially" the reviewers noticed.
- **The extraction prompt invites implementation checks.** `solana/property_prompt.j2` says to
  derive properties "from first principles", but its third category, attack vectors, is
  illustrated with "if the `authority` account is not constrained to sign…". Most of the 111 rules
  are that category: a field set, a check present, an error returned.
- **The backend guidance ranks implementation checks first.** `SOLANA_CVLR_GUIDANCE`
  (`composer/spec/cvlr/guidance.py`), which goes verbatim into the extractor's system prompt, calls
  account validation "the highest-value properties on this chain". It also excludes "rent
  economics", which keeps the rent-exempt reserve, half of the stake program's solvency condition,
  off the list.
- **The staking math was skipped for the blanket, not the arithmetic.** The skips name
  `stake_activating_and_deactivating` as external, which is point 1. The arithmetic does use
  `f64` (the warmup/cooldown rate, `state.rs:739-830`), but the properties worth stating don't
  need exact values. Effective stake never exceeds the delegation. Deactivation is monotone across
  epochs. Activating plus effective equals the delegated amount. Each holds whatever the float
  multiply returns, because the code clamps and saturates around it.

### 3. Revert rules should unwrap and assert the negation

> I see lots of rules with the following structure: … `assume(some_cond_on_pre_state); let res =
> process_fn(accounts, ...); cvlr_assert!(res.is_err());` In my experience, it is much better to
> unwrap() the call and assert the negation without assuming. This works better because the
> prover cuts out error paths resulting in smaller formulas eventually.

The delivered spec has 26 `cvlr_assert!(….is_err())` rules, 14 of them in Split & Merge. The
preferred form is the contrapositive: `process_fn(…).unwrap(); cvlr_assert!(!some_cond)`. A panic
in `unwrap` is assumed away, so every error path is dropped from the formula, and the rule states
the same thing: success implies the condition did not hold. Nothing in the CVLR authoring guidance
recommends either form. The one `is_err` example in `cvlr_baseline_facts.md` is there to illustrate
a different trap.

### What follows from this

| | fix | where |
|---|---|---|
| 1 | Stop never-inlining the split `solana-*` crates, matching what Stake Pool runs on the same generation; teach the sysvar idiom | **done**: the blanket is back to `^solana_program::.*$`; recipe K3 |
| 2 | Extract protocol-wide invariants once, across components (solvency, conservation, the stake math as bounds); steer the attack-vector category toward design consequences | property extraction prompts and pipeline |
| 3 | Teach the unwrap-and-negate form for revert properties | CVLR authoring guidance |

**The point 1 fix, checked on the Prover.** A hand-written probe against the pristine snapshot,
scaffolded with the corrected tuning files
([job](https://prover.certora.com/jobStatus/37632/ad46e66071c64143995d0ab7e6ef69de?anonymousKey=b839038d820c244c1782e546e039d03f551daa59)):

| rule | verdict |
|---|---|
| `Clock::from_account_info` can return `Ok` on the sysvar's account | VERIFIED |
| `Rent::from_account_info` can return `Ok` on the sysvar's account | VERIFIED |
| a successful `Clock::from_account_info` implies the canonical key | VERIFIED |
| `Lockup::is_in_force` is in force before the lockup epoch | VERIFIED |
| `Delegation::stake` never exceeds the delegated amount | SANITY_FAILED |

The first four were out of reach in the third run: the read never succeeded, and the address check
and the lockup decision were oracles.

**The stake math is checkable.** The fifth rule was vacuous because of its own setup, not the
arithmetic. It assumed `deactivation_epoch == u64::MAX`, and under the default conf a `u64` cannot
equal any constant at or above `2^63` (upstream-defects P11). Restated without that assumption, the
property verifies with its sanity check passing, in three forms: a stake that never deactivates, one
that deactivates after the target epoch, and an unconstrained activation and deactivation schedule
within the loop bound
([job](https://prover.certora.com/jobStatus/37632/5fe23edd4ee04ddcb45482ac5e8abc50?anonymousKey=d27dc644070d97886e2a8a22dd216cf70add168a)).
They verify under `-solanaTACSoundSignedMath` too
([job](https://prover.certora.com/jobStatus/37632/5a3d3e4b3dbf4db1a687b9d4b9f74b7e?anonymousKey=f9e0a89e6523c9e357bf9cf6445d7d611078d2ea)).
The warmup rate's `f64` multiply is summarized as unconstrained, and the bound holds anyway because
the code clamps around it. The reviewers were right to doubt the run's claim that the Prover cannot
handle this.

**P11 matters to this program beyond that one rule.** `u64::MAX` is the stake program's sentinel
for "not deactivating" and "bootstrap". The third run's Deactivation unit measured the harness side
of it and assigned the sentinel instead of assuming it (`deactivation.rs:73`). The program side
cannot be routed around: `is_bootstrap()` and `merge.rs:96` compare account-derived epochs against
`u64::MAX`, and under the default conf those branches are never taken.

None of this touches the third run's main finding: `process_withdraw` with the source equal to
the destination truncates the account through `realloc(0)`.

## Moving the benchmark to stake `main`, 2026-10-14

**Why now.** AutoProver now supports Solana's SDK 3 crates, which is what kept the benchmark on a
year-old snapshot. The plan and its evidence are in `docs/cvlr-sdk3-support.md`. In brief:

- **CVLR.** `cvlr-solana` 0.6.0 is the SDK 3 line. The port and both program models are on
  `Certora/cvlr-solana`'s `eric/sdk3`, released at `f054348`. Until it is published, AutoProver
  reads it from that commit (U26).
- **AutoProver:**
  - two Solana reference sets, chosen per target by the platform crate it links;
  - the SDK 3 tuning-path aliases;
  - platform-tools v1.53;
  - one host Rust toolchain whatever the target pins;
  - `custom-heap` for a program with no `no-entrypoint`;
  - an API corpus scoped to the run's generation.
- **Checked on stake `main` itself:**
  - a hand-written probe verifies all ten of its rules, with nothing set by hand;
  - the real preflight scaffolds the program and passes the confined compile gate.

**The target.** `solana-program/stake` `main` at `bed72d0` (2026-10-01), the commit every probe
used. It has the same 18 instructions as the snapshot.

**What changes for the expert review's points:**

- **Point 1 does not arise.** `main` reads `Clock` and `Rent` through the syscalls
  (`Clock::get()`, `Rent::get()`), and stake history through `StakeHistorySysvar`, never from
  accounts. The probe verified all three on the Prover. Nothing remains for a unit to rewrite
  around.
- **Point 2's stake math is integer arithmetic now.** `Delegation::stake_v2` and
  `warmup_cooldown_allowance.rs` replace the `f64` path, which is deprecated. The probe verified
  that effective stake never exceeds the delegation. The bounds the review asked for (effective
  ≤ delegated, deactivation monotone, activating + effective = delegated) have no float to
  excuse skipping them.
- **Solvency's rent half is stated differently.** `main` recomputes the rent-exempt reserve from
  `Rent` at each instruction. `meta.rent_exempt_reserve` is deprecated and set to a fixed pseudo
  value. So the reserve is a fresh `Rent::minimum_balance`, which is still `f64`, and recipe K3's
  integer stand-in is how to state it.
- **Point 3** is independent of the target.

**Other differences an author meets:**

- `solana-stake-interface` is a workspace member on `main`, so the scaffold forwards the `certora`
  feature into it, and `StakeStateV2` can be munged. On the snapshot it was a registry dependency.
- The program's entrypoint is unconditional, and the scaffold turns on `custom-heap` to keep its
  allocator out.
- `main` pins Rust 1.93.0. AutoProver runs its host commands with 1.90.0 and builds with
  platform-tools v1.53 (rustc 1.89), whatever the pin.

**What it costs.** `main` is a new project root, so no cache from the snapshot carries over.
Discovery, analysis and extraction are paid again, under the cache namespace `stake-main`.

**The run.** The worktree is `~/src/stake-benchmark/run-main`, on branch `eric/autoprover-main` of
the local stake checkout, clean at `bed72d0`; AutoProver scaffolds it itself. It withholds
`cvlr-solana-stake`, as every run here has. `run-main.sh <budget>` launches it on the host. The run
below went through the Solana container instead, on a copy of the worktree, so that it would double
as the alpha's container check.

## The fourth run, on `main`, 2026-10-16

`feature/solana` at `ed4ad526`, in the Solana container as the host UID, with a **$600** budget,
cache namespace `stake-main` and `--withhold-crate cvlr-solana-stake`. It ran for **20 hours 58
minutes** and exited 0. Priced from its token totals it cost **$454–525** (6.55M output tokens,
362M cache reads), and the run's own accounting reached the cap. The log, the extraction pin
(`pin-main-2026-10-16.json`), the deliverables and the failed jobs' archives are in
`~/src/stake-benchmark/run-main-2026-10-16-output/` and `run-main-2026-10-16.out`.

| unit | properties | rules | formalization |
|---|---|---|---|
| Protocol invariants (shared) | 16 of 19 | 117 | 8h 02m |
| Account Initialization | 5 of 5 | 10 | 2h 58m |
| Authority Management | 7 of 7 | 29 | 3h 54m |
| Lockup Management | 7 of 7 | 21 | 11h 08m |
| Delegation & Deactivation | 12 of 13 | 18 | 13h 50m |
| Split & Merge | 8 of 10 | 9 | 6h 38m |
| Stake & Lamport Movement | 9 of 9 | 16 | 10h 37m |
| Protocol Parameters / Queries | 5 of 5 | 11 | 15h 33m |
| Withdrawals | curtailed by budget | partial | 20h 30m |

Rules count once per property they cover; the report holds **145 distinct rules, all verified**,
no findings, 6 properties skipped with reasons and 8 program edits. The hoist folded 66 component
properties into the 19 protocol-wide ones before formalization. Formalization times include waiting
for a slot, since units ran four at a time, and Prover jobs were most of the rest.

**Graded against the criteria at the top:**

1. **Machinery survival: yes, on SDK 3.** Preflight read the workspace and passed the confined
   compile gate in 4 seconds. The scaffold forwarded `certora` into `solana-stake-interface`, and 4
   of the 8 units' program edits reach into the interface crate.
2. **Non-vacuity: not established.** Every rule passed its sanity check, but see *What to check
   before trusting the conservation rules* below.
3. **The rubric:** each item has a verified property. Lamport conservation is
   `lamports_conserved_by_every_handler` (18 rules); `delegation.stake + rent_exempt_reserve <=
   lamports` is `live_stake_accounts_rent_exempt_and_delegation_lamport_backed` (17 rules);
   staker-versus-withdrawer separation is Authority Management's
   `staker_reassignment_requires_staker_or_withdrawer_signature` and
   `withdrawer_reassignment_requires_withdrawer_signature`; lockup on `Withdraw` is
   `in_force_lockup_blocks_withdrawal_and_withdrawer_change`.
4. **Seeded mutants: not run.**

**On the expert review's points.** Point 1 did not arise, as predicted: no unit rewrote around
`Clock` or `Rent`. Point 2's bounds are there as integer properties:
`warmup_cooldown_schedule_never_overstates_effective_stake`, `deactivation_epoch_set_at_most_once`
and `delegate_never_grants_instant_effective_stake`. Point 3 is not graded here. The third run's main
finding does not recur: `main` rejects a source equal to the destination in every two-account
handler (`processor.rs:240`, `:502`, `:758`, `:934`), and the hoisted
`source_equal_to_destination_rejected_by_all_two_account_handlers` verifies.

**The six skips.** Each names what blocked it, not merely that it was hard. The Prover treats the
`sol_get_epoch_rewards_sysvar` syscall as external, so the rewards gate cannot be stated. Two
acceptance properties need authorization by signer-set membership, which the model leaves
unconstrained. Redelegate's "permanently dead" needs the dispatcher. Two Split & Merge properties
are about handler-level validation or side effects, with no function below the handler to restate
them on, which is the overflow advice's fallback working as intended.

### What the Prover did to the run

Of 93 Prover calls, **11 ended FAILED and 18 HALTED** at the 25-minute global timeout. At least two
halts were the authors' own probe jobs of four and five rules.

- **Four FAILED jobs were a Prover crash on `Clock::get()`**, "TAC Clock::get expected to extract 5
  TAC variables from call sol_get_clock_sysvar" (upstream-defects P15; a fix is in flight at
  [EVMVerifier#8762](https://github.com/Certora/EVMVerifier/pull/8762)): three Delegation &
  Deactivation jobs, one of them a resubmit, and Protocol Parameters / Queries. The Queries harness
  never calls `Clock::get()` itself, so the program's own calls trigger it too.
- **At least three were pointer-analysis failures in Lockup Management's rules**, `[3308] illegal
  dereference of an absolute address` and `[3003] dereference of a register with unknown provenance`,
  on `rule_custodian_cannot_delegate`, `rule_lockup_does_not_block_delegate` and
  `rule_custodian_cannot_withdraw`. These reasons are only in the archive's `Results.txt`, not its
  `alertReport.json`. [EVMVerifier#8765](https://github.com/Certora/EVMVerifier/pull/8765) has
  merged: a later Prover build writes them as `Solana Encoding` alerts, and `Results.txt` stays.
- **The authors saw neither.** A FAILED job reached the author as its status and a link, so Lockup
  resubmitted rules of the same kind three times. A `FAILED` or `HALTED` job now includes the
  `ERROR` entries from `Reports/alertReport.json` in that text (`hint` when it is non-empty,
  otherwise `message`). For the Clock crash the text says resubmitting the same draft will fail the
  same way. For a global-timeout halt, an `Out of Resources` error, it says to rerun the unfinished
  rules separately. Verdicts for rules that finished before the halt are not included. The CVL
  author path has the same gap, so that is deferred as parity with CVL
  ([cvlr-todo.md](./cvlr-todo.md) U32). On this run, `[3308]` / `[3003]` were only in `Results.txt`.
  [EVMVerifier#8765](https://github.com/Certora/EVMVerifier/pull/8765) has merged, so a later Prover
  build writes them as `Solana Encoding` alerts and the author text includes the hint. When every
  error alert is that type or `Out of Resources`, and at least one is the encoding alert, the advice
  says the prover could not encode the rules and that resubmitting fails until those rules or the
  program change. A `General` error alongside one is still an internal error.
- **9 "Cannot overflow: inferred pointer" stops** (P12), 5 of them in Withdrawals. The authors bound
  once and moved on, as the prompt now says. `rule_probe_withdraw_bounded` is the bound failing,
  which matches the probes in P12.
- 11 rules stopped on the loop-unwinding check, 5 of them Account Initialization's first submission.

The author prompt now says what cleared a timeout on this run. A halted job has no per-rule verdicts,
so the next submission drops the newest rule or submits that rule alone, and `loop_iter` and
summaries wait until that says which rule it was. Split & Merge halted four times on handler rules
(`8dcd1289` at `loop_iter` 4, `40c54aff` at 2, `e3a2f12e` at 3 with summaries, `ca8c602d` at 3 with
the assertions already weakened) and the other nine rules verified in minutes once those rules were
gone. Queries (`bbe56a8e`, `af531b4d`) and Lockup (one extra `split` rule on an otherwise verified
draft) were the same experiment. A `HashSet` insert, a bincode walk of a `[u8; 32]`, and a `Vec`
filled by an allocator the prover leaves external are named as loops whose iteration count moves
with the bound; the first draft asks for a loop-free replacement, a value stand-in, or the
program's own function below the handler. A `u64` multiplied into a `u128` is bounded below `2^32`
before the first submission, and a rule over several account-list lengths is one rule per length.
`loop_iter` moves only to a trip count the rule chose, as Authority Management did for
`collect_signers` over a 5-account list. The text is in
`composer/templates/cvlr_property_generation_prompt.j2` and on `AdjustProverConfig`.

### What to check before trusting the conservation rules

The curtailed Withdrawals unit wrote, in a skip reason, that **writes a handler makes through
`AccountInfo` are not observed by reads through the same handles afterwards**. It built three
positive controls, and all failed:

- a full-balance withdraw whose `resize(0)` left `data_len()` at 200;
- a partial withdraw that returned `Ok` with both balances unchanged;
- a `Deactivate` that never reached its assertion, stopping on `set_stake_state`'s loop bound.

If that were true in general, every post-equals-pre rule in this report would verify whatever the
handler did, and conservation is exactly that shape.

It is probably not general. The same unit had mocked `relocate_lamports` onto a recorder that
moves nothing, and `get_stake_state` onto a stand-in that reads no bytes, both to get past
timeouts and an overflow stop. That explains its lamport control, and possibly the others. But the
claim has not been checked, and the conservation rules in the other units have not been shown to
observe a write. **Before this run's conservation results are cited, one positive control per unit
is owed:** a rule that asserts a balance *does* change, which must be violated.

A component that edited the program is now one patch. The run writes
`certora/cvlr/components/<module>.patch`, the harness plus that unit's edits, and `git apply` lays
it onto the project as shipped. `harness.patch` stays the rules-only diff. This run's output is the
older pair: `harness.patch` applies to a clean `bed72d0` and does not build, because the harnesses
call functions only the program edits expose (`process_authorize*` made public, a new
`certora_entry`). The harness patch plus Authority Management's `cumulative_diff` built with
`cargo certora-sbf --features "certora unit_authority_management"`. The unit diffs do not stack:
after Authority Management, the other seven fail to apply.

### What it left for AutoProver

- **Verdicts from a `HALTED` job.** Deferred: parity with the CVL author path (cvlr-todo U32).
- U19's dangling fixture symlink does not exist on `main`, so this run did not exercise that fix.

