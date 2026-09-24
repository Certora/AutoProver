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
- **bincode**, not borsh derives and not `bytemuck`. `docs/the-state-behind-the-bytes.md` surveyed
  six seam shapes and bincode is not among them.
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

**The SDK 3.x gap.** A `cvlr-solana` release targeting the post-split platform generation unblocks
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

### Budget curtailment deletes rules whose verdicts are merely outstanding

The wrap-up order tells a unit to delete every rule that does not compile *and every rule whose
verdict it never saw*. Those are not the same thing. A unit cut while its jobs are still in flight
has seen no verdicts, so it deletes work that compiled and was successfully submitted:

| unit | at its peak | as delivered |
|---|---|---|
| Delegation & Activation | 1,380 lines, 32 rules | 330 lines, **0 rules** |
| Splitting & Merging | 2,185 lines, 53 rules | 482 lines, **1 rule** |
| Authorization & Lockup | 1,769 lines, 50 rules | 59 lines, **0 rules** |
| Account Initialization | 1,097 lines, 23 rules | 706 lines, 11 rules |

Filed as [[U21]].

### The carry-forward cache then enshrines the curtailed draft

`_remember_attempt` runs in a `finally`, so it caches the *last* state — which after curtailment is
the stripped one. At the end of this run the resume cache held 1,609 lines and 12 rules, having
overwritten the 6,176 lines seeded into it that morning. The mechanism built to preserve work
preserved the worst version of it. Filed as [[U22]].

Nothing was lost, because the checkpointer is durable and `ap-trail recover-drafts --draft largest`
restored 6,463 lines and 158 rules — more than the run started with. That the workaround exists is
not a reason to leave the defect.

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

The related `rule_probe_hashset_*` and `rule_diag_signer_set*` rules all came back *violated on an
assertion the prover generated* — the Prover's own internal assertion, not a property failure. That
cluster, and `rule_diag_signer_set_membership` being the rule that pinned the 100-minute job in
*both* runs, is the next thread to pull.

**What a resumed run should do first.** The vacuity is the live question, and the judge already
named the experiment: add `clog!`s to the owner comparison so the counterexample is readable, and
pin the owner to a concrete distinct key rather than assuming a disequality over an opaque 32-byte
value. Do that before authoring anything new — a second batch of rules built on the same
`assume_stake_shape` would inherit the same weakness.
