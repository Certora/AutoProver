# Supporting Solana SDK 3

Current Solana programs build against the split `solana-*` crates at major version 3: Solana SDK 3,
where `solana-program` is no longer the platform crate. The CVLR backend supports only the
`solana-program` 2.x generation, and `plan_scaffold` refuses anything newer
(`docs/stake-benchmark.md`, *The blocker*). The stake benchmark has been run on a year-old snapshot
(`ff864a9`, branch `sdk-2.2`) for that reason. Every current Foundation program would hit the same
refusal, `solana-program/token-wrap` included.

This document covers what has to change, in CVLR and in AutoProver, for a run on current stake
`main` and on SDK 3 programs generally. The changes are sized against a probe that has already
verified an SDK 3 program end to end.

## What the probe established

Stake `main` (`bed72d0`, 2026-10-01) builds against `solana-account-info` 3.1.1, `solana-pubkey`
4.4, `solana-clock` 4.0 and `solana-program-entrypoint` 3.1, with no `solana-program` at all. The
probe set it up through AutoProver's own scaffold, build and submission path, with four
hand-made substitutions:

- an SDK 3 reference set, defined in the probe script;
- the unreleased SDK 3 `cvlr-solana`, vendored into the project and patched in;
- platform-tools v1.53;
- one `Cargo.toml` edit, described under A4.

All ten rules verified, with vacuity checked
([job](https://prover.certora.com/jobStatus/37632/e00276b2db2244a8a137a65ea960f4ac?anonymousKey=de5687bd696c5f5c16b21d39897a2667e16203db)):

| Rule | What it shows |
|---|---|
| `rule_owner_can_match`, `rule_owner_can_differ` | Nondet accounts work; `Pubkey` (now `solana_address::Address`) equality is satisfiable both ways |
| `rule_lamports_round_trip` | A lamport write through `try_borrow_mut_lamports` is tracked |
| `rule_clock_get_can_succeed`, `rule_rent_get_can_succeed`, `rule_clock_get_is_stable` | `Clock::get()` and `Rent::get()`, which are syscalls, are modelled |
| `rule_stake_history_entry_can_exist` | `StakeHistorySysvar` reads through the generic `sol_get_sysvar` syscall, and that is modelled too |
| `rule_lockup_in_force_before_its_epoch` | `solana_stake_interface` is analyzed, not treated as external |
| `rule_effective_stake_never_exceeds_delegation` | The integer stake math (`Delegation::stake_v2`) verifies |
| `rule_control` | Baseline |

The Prover needs no change for SDK 3. The run used the current conf: Prover `master` and
`-solanaTACSoundSignedMath` (U25, P11).

Two things about stake `main` itself bear on the benchmark:

- It reads `Clock` and `Rent` by syscall, not from accounts. The account-reading rewrite the
  expert review objected to (point 1 in `stake-benchmark.md`) has nothing to attach to there.
- Its warmup/cooldown math is integer-only (`stake_v2`, `warmup_cooldown_allowance.rs`). The `f64`
  path the earlier runs had to skip is now deprecated.

## CVLR changes

### C1. Release `cvlr-solana` for SDK 3

An SDK 3 port exists but is unreleased. It is on the `dev-spl-token` branch
(`89986c8`, "update solana dependencies to 3.0.0"), at workspace version `0.6.0-dev`, and is not
merged to `main`. It depends on `solana-account-info`, `solana-pubkey`, `solana-program-error`,
`solana-program-option` and `solana-clock` at 3.0, and on `cvlr` ≥ 0.6.

**The pins are compatible with SDK 3 programs that have moved further.** Stake `main` is on
`solana-pubkey` 4 and `solana-clock` 4, a major version past the port's pins, and the types still
unify. `solana-pubkey` 3 sits on `solana-address` 1.1, which re-exports `solana-address` 2. And
`solana-clock` 3.2 re-exports `solana-clock` 4's `Clock`. The probe compiled and verified with
the port unchanged.

**The public API is unchanged except for one removal.** The port deletes
`fun_acc_infos_with_mem_layout` and the `acc_infos_with_mem_layout` / `mem_layout_*` macros.
Nothing is lost by that: `upstream-defects.md` already records `fun_acc_infos_with_mem_layout` as
producing vacuous rules. Every other public item keeps its name and signature.

**Rebase onto `main` before releasing.** `main` has two commits the port lacks:

- the public-API documentation (#21, 2026-10-12), which the generated API corpus is built from (A6);
- the `rt` feature build fix (`543b985`).

Released from the branch as it stands, SDK 3 would lose the docs the SDK 2.2 line has just gained.

What AutoProver needs is a crates.io release, say `cvlr-solana` 0.6.0, built on SDK 3. Until then
A5 is the stopgap.

### Release lines

**One `cvlr-solana` cannot serve both generations.** A minimal SDK 2.2 program calling one of its
handlers on `cvlr_deserialize_nondet_accounts()` from the port fails to compile:

```text
expected reference `&[AccountInfo<'_>]`
   found reference `&[solana_account_info::AccountInfo<'_>; 16]`
```

The build then links both `solana-account-info` 2.3.0 and 3.1.1. Unlike `solana-address` and
`solana-clock`, `solana-account-info` 2.x does not re-export 3.x, so the two `AccountInfo`s are
unrelated types. The same holds in the other direction, which is the original refusal. So SDK 2
and SDK 3 targets need different releases, and AutoProver pins one per generation (A1).

**The two lines differ by one field.** The port's source also builds against the SDK 2 split
crates (`solana-account-info` 2.3, `solana-pubkey` 2.4, and so on) once one line in `layout.rs` is
changed. SDK 3 renamed `AccountInfo::rent_epoch` to `_unused`. With that change, the SDK 2.2
program above compiles, against a single `solana-account-info`. Two other differences are not
about the generation:

- The port's `rt` feature references `solana_sdk`, which it no longer depends on. `main` fixes
  this (`543b985`), and the rebase in C1 picks the fix up.
- The removed memory-layout helpers were a choice, not something SDK 3 forced. Both lines can
  leave them out.

**Recommended packaging: one branch per generation, as the repo already does.** `v0.4` (1.18) and
`v0.5` (2.2) are maintenance branches, and the version number identifies the generation.

- `main` becomes the SDK 3 line and releases 0.6.x.
- `v0.5` stays the SDK 2 line. Fixes and new helpers are cherry-picked onto it, which is
  mechanical while the difference is one field.

The single-branch alternative is renamed dependencies on both `solana-account-info` majors,
selected by mutually exclusive `sdk2` / `sdk3` features. Cargo features are meant to be additive,
so a build fails as soon as anything in the graph enables the other one. The version would also no
longer say which generation a release targets. Avoid it.

**`cvlr-spl-token` is the open question.** It wraps `spl-token` itself, and the SDK 2 and SDK 3
`spl-token` releases are different major versions with API changes of their own. Its two lines
may differ by more than a field, and its port (C2) is where the real per-generation maintenance
cost will show up. Nobody has measured it yet.

### C2. Port the model crates

The port comments `cvlr-solana-stake` and `cvlr-spl-token` out of the workspace.

- **`cvlr-spl-token`** blocks every SDK 3 program that touches SPL tokens. It needs a port to
  the SDK 3 `spl-token` line and a release alongside C1.
- **`cvlr-solana-stake`** does not block the stake benchmark, which withholds it from that target
  anyway. It is still needed for any other program that calls into stake.

Until C2 lands, an SDK 3 run has no program models. A4's reference set says so: its `models` is
empty, and a token program on SDK 3 is refused as missing the token model, rather than given the
SDK 2.2 one.

## AutoProver changes

### A1. Two Solana platform generations

`reference.py` names one `ChainReference` per chain. That was the design ("one CVLR line
supported at a time"), and it has to give way here. The vault tutorial, the corpus programs and
anything older stay on 2.x, while every current program is on SDK 3. Moving the single line to
SDK 3 would trade one set of refused targets for the other.

The change:

- `REFERENCE_SET` maps a chain to an ordered tuple of `ChainReference`s, one per generation.
- `reference_for(chain, workspace)` returns the one whose platform witness the target resolves,
  or `None` when none matches. `None` keeps today's refusal, which then names every supported
  generation instead of one.
- The SDK 3 reference:
  - `core` is `cvlr` 0.6.1, as now;
  - `chain_crate` is the C1 release;
  - `models` is empty until C2;
  - `forks` is empty (no Anchor fork applies);
  - its `PlatformGeneration` has label `solana-* 3.x`, witness `solana-account-info` 3.0 and SDK
    crate `solana-account-info` 3.
- `_check_platform` still applies, so the 2.x reference still refuses a 3.x target. With a match
  chosen first, the refusal is reached only when no generation fits.

Selection moves later. `entry.py` builds the backend with `reference_for(SOLANA.name)` before
anything has read the workspace, and the RAG tools (A6) are built just above that. Both have to
follow package selection, which is the first point where the resolved graph is known.
`CvlrBackend.reference`'s default of `SOLANA` goes, because a default generation is the bug this
section removes.

### A2. SDK 3 tuning-path aliases

Each alias below was checked against the probe binary's demangled symbols. Most are the 2.2 set.
These differ:

| Canonical | SDK 2.2 | SDK 3 |
|---|---|---|
| `solana_program::pubkey::Pubkey` | `solana_pubkey::Pubkey` | `solana_address::Address` |
| `solana_program::pubkey` | `solana_pubkey` | `solana_address` |
| `solana_program::program` (`invoke`, `invoke_signed`, `set_return_data`) | unaliased; real functions in the monolith | `solana_cpi` |
| `solana_program::program::invoke_signed_unchecked` | both spellings | `solana_cpi::invoke_signed_unchecked` only |
| `solana_program::instruction::get_stack_height` | unaliased | `solana_instruction::syscalls::get_stack_height` (the symbol was not present in stake; to be checked against a program that calls it) |
| `solana_program::poseidon` | unaliased; absent | `solana_poseidon` |

The `PathAlias` docstring's longest-canonical-first rule is what keeps the symbol aliases above
from being rewritten by the module aliases they sit inside.

**The never-inline blanket** `^solana_program::.*$` matches nothing on SDK 3, so the whole SDK is
analyzed. That is the behaviour `8d99f006` restored for 2.2, and the probe verified under it.
Leave it unaliased, as on 2.2.

**`solana_get_sysvar::get_sysvar`** is new. It is the wrapper `StakeHistorySysvar` and the other
generic sysvar reads go through, and nothing tunes it. The probe shows it does not need tuning:
the syscall underneath is modelled. Record that in a comment beside the aliases, so a later reader
does not add a summary for it.

### A3. Platform tools per generation, or one newer release

`PLATFORM_TOOLS_VERSION = "v1.43"` (rustc 1.79) cannot build stake `main`. Its cargo cannot parse
the edition-2024 manifests in the lockfile (`crypto-common` 0.2.2 is the first it meets). The
author only sees this as a misleading `--offline` error (memory note:
`platform-tools-cargo-1-79-edition2024`).

Certora platform-tools **v1.53** (rustc 1.89) builds stake `main`, and it also builds the SDK 2.2
vault tutorial. So the simpler change is one bump to v1.53, not a per-generation setting. The bump
also removes the `zeroize` downgrades 2.2 targets have needed under v1.43.

Before the bump: run the CVLR e2e test (`cvlr-e2e-not-in-ci`) and one vault rule set under v1.53,
because rustc 1.89 changes codegen, and with it the symbols the 2.2 aliases match. If the vault
regresses, make the release a field of `PlatformGeneration` instead.

The conf comment that says one release serves every target "because the reference set pins one
platform generation" has to be reworded either way.

### A4. Excluding the entrypoint when there is no `no-entrypoint`

The scaffold writes `certora = ["no-entrypoint", …]` when the package declares `no-entrypoint`,
which is how the program's own entrypoint and global allocator are kept out of the verification
build. Stake `main` declares no features at all, and calls `entrypoint!` unconditionally. (At
`ff864a9` it was gated behind an opt-in `bpf-entrypoint`, which is why the old snapshot never
showed this.)

With the entrypoint compiled in, every rule that allocates fails with Prover error 3309,
*Conflicting allocation/usage of heap memory*. The program's `BumpAllocator` writes the heap at an
absolute address, and `__rust_alloc` is in use too. Three of the probe's first ten rules failed
this way
([job](https://prover.certora.com/jobStatus/37632/6ccedb5b27a24dbdbc4ca0644a5464ed?anonymousKey=343439334d18c65f4c1e4c8710b8be4e78033026)).

`entrypoint!` installs its allocator only when the *calling* crate's `custom-heap` feature is
off. That is true of the 2.x macro and the 3.x one alike. Declaring `custom-heap = []` and adding
it to `certora` fixed all three.

The scaffold rule:

- When the package declares `no-entrypoint`, keep doing what it does now.
- Otherwise, when the package calls `entrypoint!` (from `solana_program_entrypoint` or
  `solana_program`), declare `custom-heap` if it is absent, and add it to `certora`.
- `custom-panic` is the matching lever for the panic handler. It has not been needed, so leave it
  out until a probe says otherwise.

This is generation-independent. A 2.x program without `no-entrypoint` has the same problem.

### A5. A development pin for the unreleased `cvlr-solana`, until C1

`_check_pins` refuses a CVLR crate declared as a git or path dependency, because its release
cannot be read. Until C1 is on crates.io, the SDK 3 reference set needs a way to name the branch.
Follow the `prover_version: master` precedent (U25):

- A `CrateRelease` variant for a git revision, pinned to a commit hash rather than a branch.
- The scaffold writes it as a `[patch.crates-io]` entry. The project still declares
  `cvlr-solana = "=0.6.0-dev"`, so `_check_pins`'s version reading holds.
- A `TODO(cvlr-todo U26)`, recorded as a ship blocker in `cvlr-todo.md`: replace the git pin with
  the C1 release before the Solana AutoProver ships.

The patch target has to be readable inside the build sandbox. The probe vendored the checkout
under the project and added `exclude = ["vendor-cvlr"]` to the workspace, so that the nested
CVLR workspace did not become a member. A git source fetched by cargo's own warm step avoids both
problems, which is why the pin is a git revision and not a vendored copy.

### A6. Scope the API corpus to the run's generation

`cvlr_api_kb` is generated from rustdoc over the releases the reference set pins, and its tools
tell the author it is authoritative at those releases. The version is already a level in every
header path (`CVLR API > cvlr-solana > 0.5.0 > …`), but the tools ignore it:

- `cvlr_api_surface` returns the first surface it finds "whatever the version is";
- `cvlr_api_lookup` and `cvlr_api_search` return hits from every version.

With one generation in the database that was harmless. With two, an SDK 3 run would be told, with
full authority, about `cvlr-solana` 0.5.0, removed helpers included.

The change:

- `cvlr_api_docs` generates every generation's crates. The producer loops over
  `REFERENCE_SET["solana"]` instead of calling `reference_for` once. Crates the generations share
  (`cvlr` and its sub-crates) appear once.
- The three tools are bound to the run's `ChainReference`. They drop rows whose crate and version
  are not among its `crates()`, and `cvlr_api_surface` picks the row at the pinned version.
- A test runs a fake corpus holding two versions of one crate, and checks that each tool sees
  only the pinned one.

The manual (`cvlr_kb`) stays as it is. It is prose, and allowed to lag. Its examples spell
`solana_program::` imports, and the tools already tell the author that the API corpus overrides
it.

### A7. Knowledge and prompt text

Little of it depends on the generation.

- The munge editor's examples (`use anchor_lang::solana_program::program::invoke`) are Anchor
  ones, and stay correct for Anchor targets.
- `cvlr_property_generation_system_prompt.j2` says "`invoke` is `solana_program`'s". It should
  say "the platform's" (`solana_cpi`'s on SDK 3).
- Recipe K3 (sysvars read directly) applies to programs that read sysvars from accounts. It
  should say that SDK 3 programs usually call `Sysvar::get()`, which is modelled and needs no
  recipe.

### A8. The stake benchmark

- Point the benchmark at stake `main` once A1–A5 are in. Record in `stake-benchmark.md` that the
  `sdk-2.2` snapshot is retired, and why.
- Point 1 of the expert review is moot on `main` (see *What the probe established*). Re-check
  point 2 on the new run: the stake math is now integer, so its design properties should be
  provable rather than skipped.
- `main` is a new project root, so the `sdk-2.2` cache namespaces do not carry over. Discovery,
  analysis and extraction are paid again.
- The scaffold now forwards a `certora` feature into `solana-stake-interface`, which is a
  workspace member on `main`. So `StakeStateV2` can be munged, which the snapshot could not do.

## Order

1. **C1 request** to the CVLR owners: rebase the port onto `main`, then release it. Ask for the
   C2 ports at the same time.
2. **A3**, with the vault check. It is independent of everything else, and unblocks the build.
3. **A4.** Also independent, and testable on the probe worktree.
4. **A1 + A2 + A5** together. A1 is the reference-set change, A2 the SDK 3 aliases it carries,
   and A5 the pin that makes A1 usable before C1.
5. **A6.** Required before the first SDK 3 run, because without it the author is misinformed.
6. **A7, A8**, then the stake run on `main`.

When C1 releases, A5's pin becomes an ordinary `CrateRelease` and U26 closes.

## Reproducing the probe

The probe worktree is `~/src/stake-benchmark/main-probe` (detached at `bed72d0`), with the port
vendored at `vendor-cvlr/` and the scaffold already applied. The script, the rules and both runs'
logs are in `~/src/stake-benchmark/sdk3-probe/`. Run the script from the AutoProver checkout with
`COMPOSER_SANDBOX_PROVIDER=none env -u CERTORA uv run --no-sync python <path>/run_probe.py`. It
defines the SDK 3 reference set inline, which is the starting point for A1 and A2.
