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

Stopped deliberately at **~$57 of a $75 ceiling**, mid-authoring, with everything resumable:
`--cache-ns stake-benchmark` holds the analysis and all 94 extracted properties, `.cvlr_work/build`
holds the drafts and munges. A continuation re-pays for authoring iterations only.

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

**What a resumed run should do first.** The vacuity is the live question, and the judge already
named the experiment: add `clog!`s to the owner comparison so the counterexample is readable, and
pin the owner to a concrete distinct key rather than assuming a disequality over an opaque 32-byte
value. Do that before authoring anything new — a second batch of rules built on the same
`assume_stake_shape` would inherit the same weakness.
