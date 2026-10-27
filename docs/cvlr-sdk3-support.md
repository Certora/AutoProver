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

## Where things stand (2026-10-27)

Every CVLR release AutoProver pins. All are on crates.io.

| Release | Repository | Branch, PR | Release commit | State |
|---|---|---|---|---|
| `cvlr` 0.6.2, 13 crates | `Certora/cvlr` | [#52](https://github.com/Certora/cvlr/pull/52) | `0ef58e4` (squash of `de8dec0`): `main` (API docs #51, [#53](https://github.com/Certora/cvlr/pull/53)'s `.cvlr_rules` and `cvlr-rule-manifest`) released | merged; tags pushed; **published** 2026-10-27 |
| `cvlr-solana` 0.5.1, 3 crates (SDK 2) | `Certora/cvlr-solana` | `eric/release-0.5.1`, [#22](https://github.com/Certora/cvlr-solana/pull/22) | `2e53877`: `main` (API docs #21, `rt` fix) released | merged (`a9898ba`, a merge commit); tags pushed; `v0.5` moved up to it; **published** 2026-10-27 |
| `cvlr-solana` 0.6.0, 3 crates (SDK 3) | `Certora/cvlr-solana` | `eric/release-0.6.0` (the port, C1 and C2, on 0.5.1), [#24](https://github.com/Certora/cvlr-solana/pull/24) | `13bb381` | merged (`e92bccd`, a merge commit); tags pushed; **published** 2026-10-27 |

Each release commit is what `cargo-release` would produce: versions, CHANGELOG and README. Each
passes the repository's CI checks and `cargo publish --dry-run --workspace`. `cvlr-solana` PRs are
merged with a merge commit, so `main` contains the release commit its tags and AutoProver's pin
name: #22 went in as `a9898ba`, and #24 as `e92bccd`. `Certora/cvlr` does not allow merge
commits, so #52 is squash-merged. The squashed commit has the release commit's tree under a new
hash: the `cvlr` tags are recreated on it, after checking the two trees match, and then pushed.

`cvlr` needs nothing for SDK 3. The core crates do not depend on the Solana SDK, and both
`cvlr-solana` lines require their `cvlr-*` crates at `<1` only (0.6.0 at `>=0.6`, 0.5.1 at
`>=0.4`), so any 0.6.x core serves both. AutoProver needs 0.6.2 for two other reasons: the
published 0.6.1 has no API docs (`cvlr-todo.md` U28), and `verify_rules` reads rule names from the
`.cvlr_rules` section that #53 added. 0.6.2 has both, and `cvlr-rule-manifest` is among its
crates.

### Publishing, and git dependencies until then

**Members of `certora/cvlr-publishers` can publish.** The GitHub team is a crates.io owner of
every crate in both repositories, beside `1arie1` and `chandrakananandi`. Neither repository
publishes from CI, so a release is published by hand, from its tag.

**Until a release is published, AutoProver reads it from its release commit** (A5). The
reference set pins the crates.io version and names the commit, and the scaffold writes a
`[patch.crates-io]` entry per crate. No release is read this way now. The core (until 0.6.2),
0.5.1 (from `2e53877`) and 0.6.0 (from `13bb381`) were, until they were published.

**Fetching works before the PRs merge.** Cargo, starting from an empty cargo home, resolved
`cvlr-solana` 0.5.1 from GitHub at `2e53877` before #22 merged; the pushed branch makes the commit
reachable.

**The pin has to follow the release commit.** A review change to a release commit gives it a new
hash, and the pin in `reference.py` moves with it. Pushing the tags makes the commits permanent.

**The exit is publication.** Once a release is on crates.io, its `GitCommit` is dropped and
nothing else changes. U26 in `cvlr-todo.md` tracked this as a ship blocker; it closed with 0.6.0.

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

Nothing the probe exercised needs a Prover change. The run used the current conf: Prover
`master` and `-solanaTACSoundSignedMath` (U25, P11). One Prover change is needed outside what the
probe covers: SPL Token CPI substitution does not recognize SDK 3's `invoke` (P1, below).

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

What AutoProver needs is a release built on SDK 3: `cvlr-solana` 0.6.0.

**Status: published as 0.6.0** (#24, merged as `e92bccd`). Branch `eric/sdk3` of
`Certora/cvlr-solana` is the port cherry-picked onto `main` (`c61ee6f`), the model crates (C2,
`61e8661`) and a release-notes commit, rebased onto the 0.5.1 release. The cherry-pick was resolved
this way:

- `main`'s documentation is kept.
- `fun_acc_infos_with_mem_layout` is dropped, as the port did. The `acc_infos_with_mem_layout!`
  alias stays: it only calls `cvlr_deserialize_nondet_accounts()`, and existing specs use it.
- The `rt` feature uses `solana-account` 3 instead of `solana-sdk`.
- `invoke!` and `invoke_signed!` now expand to `solana_cpi::invoke_signed_unchecked`, through a
  hidden re-export. The port had left them naming `solana_program`, which no SDK 3 program has.

CI's checks pass on the branch: `fmt`, `clippy -D warnings`, `doc -D missing_docs`, and the tests.
The stake probe verifies all ten rules against it
([job](https://prover.certora.com/jobStatus/37632/357dc4fa69324cb6be0509048e0c7b9d?anonymousKey=f559cc790f6f81ec87f20767473839f283569fcc)).
The 0.6.0 release commit is `13bb381`, merged by #24 and published. The release notes say that SPL
Token CPI substitution does not yet apply to SDK 3 programs (P1).

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

This is done. 0.5.1 is `main`'s last SDK 2 release (#22), `v0.5` has moved up to it, and 0.6.0 is
on `main` (#24).

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

**Status: ported on `eric/sdk3` (`61e8661`) and released with it at 0.6.0 (published), but only
compiled, never verified against a program.** A1's reference set can include both models.

- `cvlr-spl-token` builds on `spl-token` 9. Its re-export of `spl_token::instruction`, which
  `spl-token` 9 deprecates, is kept because the crate stands in for `spl_token`.
- `cvlr-solana-stake` takes its types from `solana-stake-interface` 5, the line stake `main` uses.
  It reads the clock account through `SysvarSerialize`, so its behaviour is unchanged.
  - It still applies the program's former rent rule, `Meta::rent_exempt_reserve`. The current
    program reads `Rent` instead. The crate docs list this as a known divergence.
  - Interface 5 is a choice: `solana-stake-interface` had four majors on SDK 3 in 2026, and a
    program on another major has different `StakeStateV2` types from the model's.

The token model also depends on P1: until the Prover recognizes SDK 3's `invoke`, the mocks are
never substituted.

## Prover change

### P1. CPI substitution matches only the monolith's `invoke`

The Prover's CPI analysis, which replaces SPL Token CPIs with `cvlr-spl-token`'s mocks, finds
calls by exact demangled name: `solana_program::program::invoke` and
`solana_program::program::invoke_signed` (EVMVerifier `src/main/kotlin/sbf/analysis/cpis/CpisAnalysis.kt`,
`INVOKE_FUNCTION_NAME` and `INVOKE_SIGNED_FUNCTION_NAME`, compared with `==`).

On SDK 3 there is no `solana_program`, and a program calls `solana_cpi::invoke` and
`solana_cpi::invoke_signed`. The analysis finds no invokes, so no CPI is substituted. Nothing
reports this. With `-solanaCpiAnalysis` on, a rule crossing a token CPI would neither use the mock
nor hit the analysis's "Unresolved CPI call" assertion. Such a rule could pass while checking
nothing about the transfer.

The fix is to recognize both spellings:

- the names `CpisAnalysis.kt` compares calls against;
- the `mockFor` names `CpisSubstitutionMap.kt` passes to `getCallInstructionToThis`.

The stake probe does not cover this: stake makes no token CPIs, and the probe never turns CPI
analysis on. A test needs an SDK 3 program that transfers tokens by CPI.

The mangled names the substitution map carries for the `cvlr_spl_token` mocks are only metadata.
Calls are made by demangled name, so a new `cvlr-spl-token` release does not need a Prover
change.

## AutoProver changes

### A1. Two Solana platform generations

**Done.** `REFERENCE_SET["solana"]` is `(SOLANA_V3, SOLANA_V2)`, newest first:

- `SOLANA_V3` is `cvlr-solana`, `cvlr-solana-stake` and `cvlr-spl-token` 0.6.0, on the same
  `cvlr` 0.6.2 core as `SOLANA_V2`.
- `scaffold.select_generation` picks the set whose platform witness the package links. It reads
  the witnesses the same way `_check_platform` does, and it requires a witness to be linked.
- `preflight.select_package` calls it at the point the run first reads the workspace, before any
  service starts. A package on neither generation is refused there, with what it builds and both
  supported generations in the message.
- `entry.py` gives the backend `selected.reference`, and `CvlrBackend.reference` has no default.
- The API-docs producer documents every generation, each in its own probe crate, and merges by
  crate name and version. A `--crate-source` checkout reaches only a generation that pins its
  version.

Checked on real projects:

- Selection picks SDK 3 for stake `main`, and SDK 2 for the stake snapshot and the vault.
- The real `prepare_workspace` and the confined compile gate pass on a fresh stake `main`
  worktree, with `cvlr-spl-token` 0.6.0 in the graph.

What follows is the original plan.

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
  - `core` is `cvlr` 0.6.2, shared with 2.x;
  - `chain_crate` is `cvlr-solana` 0.6.0;
  - `models` are `cvlr-solana-stake` and `cvlr-spl-token` 0.6.0;
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

**Done**, in `SOLANA_V3.platform.path_aliases`. `tests/data/stake_sdk3_sbf_symbols.txt` holds stake
`main`'s demangled platform symbols, built with v1.53. The tests check the aliases against that
fixture, as they check the 2.x aliases against the vault's. Eight aliases are exercised there. The
seven stake does not exercise are listed in the test. The starting inlining file reaches
`Clock::get`, `Rent::get` and `set_return_data` only through the SDK 3 spellings.


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

### A3. One newer platform-tools release

`PLATFORM_TOOLS_VERSION = "v1.43"` (rustc 1.79) cannot build stake `main`. Its cargo cannot parse
the edition-2024 manifests in the lockfile (`crypto-common` 0.2.2 is the first it meets). The
author only sees this as a misleading `--offline` error (memory note:
`platform-tools-cargo-1-79-edition2024`).

**Done: one bump to v1.53** (rustc and cargo 1.89), which builds stake `main` and the SDK 2.2
vault. One release still serves every target, so there is no per-generation setting. What
changed:

- `PLATFORM_TOOLS_VERSION` and `scripts/Dockerfile.solana`'s `SOLANA_TOOLS_VERSION`, which the image
  drift test keeps equal. `cargo-certora-sbf` 0.3.5, the image's pin, drives v1.53.
- **The vault symbol fixture is regenerated under v1.53.** rustc 1.89 inlines six functions that
  v1.43 kept as symbols: the four `AccountInfo::try_borrow_*`, plus two in Anchor. The tuning
  files mark the four `#[inline]` anyway, so the Prover analyzes the same code, now inside the
  callers. Every alias still names a path the binary defines, and the dialect still costs no
  coverage. Only the counts in the coverage test moved: core 6 → 15 symbols, was 6 → 19.
- **The loop-bound test recognizes v1.53's disassembly.** The new disassembler writes a backward
  branch as `jge r2, 0x3, -0xa`, not `if … goto -0xa`. The test looked for the old spelling, so it
  reported that the loop was optimized away. It was not. The test also called `write_submission`
  without the `into` argument #258 added, so it failed under v1.43 too. And it now accepts that the two arms'
  build scripts have different paths, since each is named after its submission. It checks that
  their contents match instead. It passes under v1.53, with real Prover jobs on both arms.

**The expensive build-and-prove tests under v1.53:**

- Passed: the end-to-end gate, Anchor reach, both preflight tests, and the loop bound.
- **Failing under v1.43 as well, so not caused by the bump:** `test_cvlr_dropped_writes`. Its
  Prover job ends `FAILED` with no results under both releases.

### A3b. One host Rust toolchain, whatever the target pins

**Done.** Every target we run pins its own Rust in `rust-toolchain.toml`:

| Target | Pins |
|---|---|
| the vault | 1.89.0 |
| stake, `sdk-2.2` snapshot | 1.86.0 |
| stake `main` | 1.93.0 |
| SolanaExamples | 1.79 |
| klend | 1.74.1 |

**None of them would run in the Solana image.** The image installs 1.90.0, its rustup home is
root-owned, and the container runs as the host's UID. A pin the image lacks cannot be installed,
so cargo does not start (`Permission denied` under `/opt/autoprove/rustup/tmp`). It never showed up
on a development machine, where rustup installs whatever a project pins.

**CVLR now runs every cargo command with `HOST_RUST_TOOLCHAIN` (1.90.0)**, through
`RUSTUP_TOOLCHAIN`, which rustup reads ahead of the pin. That covers the workspace read, the warm
step, the confined checks and builds (through the sandbox policy's environment), and the build
script the Prover CLI reruns (through its command file).

The pin configures the project's own builds. The verified program is compiled by the platform
tools' rustc whatever the pin says. So the host toolchain only runs `cargo metadata`, `cargo check`
and build scripts.

The image drift test keeps the constant equal to the Dockerfile's `RUST_VERSION`. Other users of
`CargoSession`, such as Crucible's Solana toolchain, still follow the project's pin.

### A4. Excluding the entrypoint when there is no `no-entrypoint`

**Done.** The `certora` feature turns on the entrypoint switch:

- `no-entrypoint`, when the package declares it;
- otherwise `custom-heap`, when the package's sources invoke `entrypoint!` or
  `entrypoint_no_alloc!`. The scaffold declares `custom-heap = []` if the package does not.

Both macros install the allocator through `custom_heap_default!`, in `solana-program` 2.x and
`solana-program-entrypoint` 3.x alike.

The SDK 3 stake probe passes with nothing set by hand
([job](https://prover.certora.com/jobStatus/37632/7864326f83ea451badcdd40895e2f87d?anonymousKey=469262b3bd888f4e1df8fd3f3487aba74c8614d9)):

- a fresh worktree of `main`;
- the scaffold adding `custom-heap` itself;
- `cvlr-solana` 0.6.0 read from `f054348` (A5);
- a confined v1.53 build with the host toolchain (A3, A3b).

All ten rules verify.

A project that already has a `certora` feature is left as it is. The scaffold does not extend an
existing feature list.

What follows is the original analysis.

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

### A5. Reading an unpublished release from its commit

**Done, for the 2.x line.** A release can be cut and tagged well before it is published. Until it
is, the reference set names the commit the
release was cut from:

- **The reference set.** `CrateRelease.unpublished` is a `GitCommit`: a repository and a commit
  hash, never a tag or a branch, since either can move.
- **The project's dependency.** The project still declares the crates.io spelling (`=0.5.1`), so
  `_check_pins` reads the version as it always has.
- **The patch.** The scaffold adds one `[patch.crates-io.<crate>]` table per pinned crate the
  project is given. Cargo applies a patch only when the commit's version meets the requirement,
  so a wrong commit fails the build instead of standing in for the release.
- **A project that already patches the crate** is checked against the resolved graph. The pinned
  commit is satisfied; anything else is refused.
- **The API-docs producer** patches its probe crate the same way. A `--crate-source` checkout
  replaces the commit for the crates it defines. The manifest's source line records the commit.
- **The ship blocker.** U26 in `cvlr-todo.md` is the exit: once a release is published, drop its
  `GitCommit` and nothing else changes. Every release has taken it: the core (0.6.2, U28), 0.5.1
  and 0.6.0. The tests exercise the path through `read_from_commit` in `tests/conftest.py`.

`cvlr-solana` 0.5.1 was pinned this way (`2e53877`) until it was published. A sandboxed scaffold,
warm and SBF build of the SDK 2.2 stake snapshot, with the pin pointed at a local `file://` clone,
resolved `cvlr-solana` 0.5.1 from that commit. A second run of the scaffold changed nothing. The SDK
3 reference set (A1) pinned the 0.6.0 release commit (`13bb381`) the same way until 0.6.0 was
published.

Both release commits are pushed (see *Where things stand*). Cargo fetches the 0.5.1 commit from
GitHub with an empty cargo home.

### A6. Scope the API corpus to the run's generation

**Done.**

- **The tools are scoped.** `cvlr_api_search`, `cvlr_api_lookup` and `cvlr_api_surface` read
  through an `ApiCorpus`, which holds the run's pins: crate to version, from
  `selected.reference.crates()`. A row for a crate the run pins at another version is dropped,
  and the search and lookup ask the database for extra rows to make up for it. A crate the run
  does not pin, such as the core's sub-crates, is shown at any version: the generations share it.
- **The scope reaches the tools.** `cvlr_research.corpus_tools` takes the pins (`pinned=`) and
  hands them to each corpus's factory. `entry.py` passes the selected generation's. The manual
  corpus ignores them.
- **The producer documents both generations** (A1). When a probe crate needs the network to
  resolve a pinned commit, it now fetches the whole graph before running rustdoc offline.
- **Generated for real:** 362 sections. There is one surface per crate and version: the core once
  at 0.6.1, and the three `cvlr-solana` crates at both 0.5.1 and 0.6.0. There are no duplicate
  header paths. `fun_acc_infos_with_mem_layout` appears only under 0.5.1.

**Not ingested yet.** The corpus in the database is still the earlier single-generation build,
until `scripts/populate_cvlr_rag.sh` is rerun.

What follows is the original plan.

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

**Done.**

- Where a prompt, or `swap_import`'s tool description, said `invoke` is `solana_program`'s, it now
  says it is the SDK's: `solana_program`'s, or `solana_cpi`'s on SDK 3. That covers the
  property-generation prompt, the munge editor's prompt, `editor.py` and `munge.py`. The vacuity
  prompt describes handlers written against either SDK. The Anchor examples are unchanged, since
  they are right for Anchor.
- **Recipe K3:**
  - It gives the SDK 3 spellings: `from_account_info` is on `SysvarSerialize`, and the address is
    `Clock::id()`.
  - It notes that syscall reads, `StakeHistorySysvar` included, need no account.
  - It says what to do when the program recomputes the rent reserve at each instruction instead of
    storing it. Stake on SDK 3 does that, and the integer stand-in for `minimum_balance` is then the
    way to state the property. SDK 3's `solana-rent` still computes `minimum_balance` in `f64`, so
    the float caveat stands.

What follows is the original plan.

Little of it depends on the generation.

- The munge editor's examples (`use anchor_lang::solana_program::program::invoke`) are Anchor
  ones, and stay correct for Anchor targets.
- `cvlr_property_generation_system_prompt.j2` says "`invoke` is `solana_program`'s". It should
  say "the platform's" (`solana_cpi`'s on SDK 3).
- Recipe K3 (sysvars read directly) applies to programs that read sysvars from accounts. It
  should say that SDK 3 programs usually call `Sysvar::get()`, which is modelled and needs no
  recipe.

### A8. The stake benchmark

**Set up; not launched.** `stake-benchmark.md` records the move to `main`, the snapshot's
retirement, and what changes for each review point. The run is in its own worktree on branch
`eric/autoprover-main`, and `run-main.sh` takes the budget as a required argument.

What follows is the original plan.

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

**Done:**

- C1 and C2, ported and released as commits.
- 0.5.1 (#22) and `cvlr` 0.6.2 (#52), merged and published; both are pinned from crates.io.
- Every AutoProver item: A1 to A7, with A3b, and A8's setup.
- The two-generation API corpus, generated and ingested.

**Next:** the stake run on `main` (`~/src/stake-benchmark/run-main.sh <budget>`), once a budget is
chosen.

**Alongside, outside AutoProver:**

- **P1** goes to the Prover team. Nothing on the AutoProver side waits for it, but token programs
  on SDK 3 do.

## Reproducing the probe

The probe worktree is `~/src/stake-benchmark/main-probe` (detached at `bed72d0`), with the port
vendored at `vendor-cvlr/` and the scaffold already applied. The script, the rules and both runs'
logs are in `~/src/stake-benchmark/sdk3-probe/`. Run the script from the AutoProver checkout with
`COMPOSER_SANDBOX_PROVIDER=none env -u CERTORA uv run --no-sync python <path>/run_probe.py`. It
defines the SDK 3 reference set inline, which is the starting point for A1 and A2.
