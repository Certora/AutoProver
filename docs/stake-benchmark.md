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

## Open, and not worked around

**The SDK 3.x gap.** A `cvlr-solana` release targeting the post-split platform generation unblocks
every current Foundation program at once. Until there is one, this workaround is the only route and
it expires as targets age out. This belongs with whoever owns CVLR releases.

**`cvlr-solana-stake` must be excluded from this target.** That crate ships 572 lines implementing
`process_withdraw`, `process_split`, `process_merge`, `process_authorize`, `process_delegate` and
`process_deactivate` — a behavioral model of the exact instructions the benchmark asks the backend
to specify. For any other target it is dependency modeling, which is what a chain specialization is
for. Here it is an answer key.

Nothing suppresses it today. [`_scaffold_pins`](../composer/spec/cvlr/scaffold.py) returns the whole
reference set for a greenfield project, so the crate lands in the manifest and in the `certora`
feature, and [`crate_mount.py`](../composer/spec/cvlr/crate_mount.py) makes its source readable by
the authoring agent. The seam for a fix already exists: `reference: ChainReference` is threaded as a
parameter through the scaffold, and the `specializations` docstring in
[`cvlr_reference.py`](../composer/spec/cvlr_reference.py) states the contract — *"a specialization
left out of this list is a crate the project cannot name."* What is missing is only the ability to
vary that list per run. Narrowing must be per-crate: `cvlr-spl-token` sits in the same tuple, and
dropping specializations wholesale would cost the token model on a target that needs it.

Excluding the crate removes the mechanical leak, not the semantic one — it is published on
crates.io, and the stake program's invariants are documented protocol semantics. That residue is
acceptable for this question, which is about whether the prompts transfer, not about what a base
model knows.
