# Supporting `solana-program` 1.18

The CVLR backend supports two Solana platform generations, `solana-program` 2.x and the split
`solana-*` 3.x crates (`docs/cvlr-sdk3-support.md`). Programs written before 2.0 are on neither,
and the scaffold refuses them. klend is one of them, and it is the third CVLR benchmark
(`docs/klend-benchmark.md`). This document plans a third generation, `solana-program` 1.18, on the
`cvlr-solana` 0.4 line.

G1, G2, G5, G6 and G7 are implemented. The probe below establishes that the combination builds.

## Where things stand

| | What | State |
|---|---|---|
| G1 | A 1.18 reference set | **done** |
| G2 | Telling 1.17 from 1.18 | **done**: 1.x keyed on major.minor |
| G3 | Document `cvlr-solana` 0.4.x | **done**: 0.4.6, pinned by its `v0.4` commit ([cvlr-solana#23](https://github.com/Certora/cvlr-solana/pull/23)) |
| G4 | Scope the API corpus to 1.18 | **done**: generated and ingested locally |
| G5 | Knowledge text: what the manual gets wrong on 1.18 | **done** |
| G6 | Run the compile gate against the SBF target | **done** |
| G7 | Tests | **done**, except the end-to-end, which waits on G6 |
| — | `cvlr-spl-token` for 1.18 | **deferred**, see *Token CPIs* |
| — | Moving the klend input to 1.18 | outside AutoProver, see *The input* |

Order: G1, G2, G5 and G7 together, then G6, then G3 and G4.

## What the probe established

The probe was a scratch copy of the klend benchmark input (program version 1.23.0), built step by
step with platform tools v1.53, the release AutoProver uses (`PLATFORM_TOOLS_VERSION`), and
`cargo certora-sbf`.

| Step | Result |
|---|---|
| Input as it is | Cargo cannot load the workspace. A member outside the program depends on a git repository the public cannot read |
| …with that member out of the workspace | `ahash` 0.7.6 and 0.8.5 fail: `unknown feature stdsimd`, removed from rustc |
| …with `cargo update ahash` | Refused. `solana-frozen-abi` 1.17.18 requires `ahash =0.8.5`. **A 1.17 program cannot be built with v1.53 at all** |
| Workspace `solana-*` moved from `~1.17.18` to `~1.18` (resolves 1.18.26, `ahash` 0.7.8 and 0.8.12) | **Builds**, 1m09s |
| Plus `cvlr` 0.6.1 (the documented commit `465a022`), `cvlr-solana` **0.4.5**, Anchor fork `certora-v0.29.0`, `fixed` fork `certora-v1.23.1`, and a one-rule harness | **Builds**, 50s |
| Host `cargo check -F certora`, rustc 1.90.0 (`HOST_RUST_TOOLCHAIN`) | Fails in `farms`, a dependency: see G6 |
| Host `cargo check`, rustc 1.74.1 (klend's pin) | Cannot read the lockfile. Cargo 1.89 rewrote it to version 4 |
| `cargo check -F certora --target sbpf-solana-solana`, platform-tools v1.53 rustc and cargo | **Passes**, 2m08s cold |

**`cvlr-solana` 0.4.5 runs on the 0.6.1 core.** Its `cvlr-*` requirements are `>=0.4.0, <1`, so the
graph resolves one copy of each core crate, the patched 0.6.1. The three generations share one
core, as 2.x and SDK 3 already do, and the core's API documentation and the parametric-rule layer
(`cvlr-spec`) come with it.

**0.4.5 is the end of the 1.18 line.** `cvlr-solana` 0.4.5 (January 2026, branch `v0.4` at
`c814f76`, tag `v0.4.5`) requires `solana-program ^1.18`. 0.5.0 is the next release, and 0.4.5 is
its ancestor.

## AutoProver changes

### G1. A 1.18 reference set

A third `ChainReference` in `composer/spec/cvlr/reference.py`, `SOLANA_V1_18`:

- `core=_CORE`, the shared `cvlr` 0.6.1.
- `chain_crate=CrateRelease("cvlr-solana", "0.4.5")` from crates.io, until G3 replaces it with a
  documented commit.
- **No `models`.** The 0.4 line has no stake model, and its token helpers are in the chain crate
  itself. `withholding("cvlr-solana-stake")` refuses on this set, which is correct: it has nothing
  to withhold.
- `forks=(ANCHOR_FORK, FIXED_FORK)`. Both already cover klend's Anchor 0.29.0 and `fixed` 1.23.1.
- `platform`: label `solana-program 1.18`, `sdk_crates` and `witnesses` both
  `CrateRequirement("solana-program", "1.18")`.
- **No `path_aliases`.** 1.18 is the monolith, whose spelling is the canonical one the starting
  tuning files use. That includes `solana_program::program::invoke_signed_unchecked`, which the
  core summaries already list.

`REFERENCE_SET["solana"]` becomes `(SOLANA_V3, SOLANA_V2, SOLANA_V1_18)`, newest first.

Checked on the host (rustc 1.90.0): a crate declaring exactly `SOLANA_V1_18.cargo_dependencies()`
with its commit patches resolves one copy of each `cvlr` crate and compiles in 17s.

### G2. Telling 1.17 from 1.18

`scaffold._generation` keys a version on its major component, so 1.17 and 1.18 would look like one
generation. A 1.17 target would be selected for the 1.18 set. Cargo would then fail to resolve
`cvlr-solana`'s `^1.18` against the target's `~1.17.18`, with an error naming neither generation.

**Decided:** key a 1.x version on major.minor. Before 2.0, Solana's minor releases behaved as
breaking ones, and the probe's `ahash` pin is an example. A 1.17 target is then refused at
preflight, with a message listing the three supported generations, `solana-program 1.18` among
them. 2.x and 3.x keep the major-only key: the
2.x witness `solana-account-info` is `2.2.1` under the `solana-program` 2.2 that every 2.x target
links, so a stricter key would refuse them.

### G3. Document `cvlr-solana` 0.4.x

The published 0.4.5 has no doc comments, so its API corpus entries would be signatures only. That
is the crate authors call most. The 2.x and SDK 3 lines were documented in `Certora/cvlr-solana`
`d69566c` (#21).

**The port is mostly mechanical.** The chain crate's API differs between 0.4.5 and 0.5.0 by six
lines in `token.rs`. Applying the `cvlr-solana` part of `d69566c` (11 files, ~290 lines) to 0.4.5
applies seven of nine source files cleanly. By hand:

- `cvlr-solana/Cargo.toml`: keep 0.4.5's dependencies (`solana-program 1.18`, `spl-token 4`,
  `spl-token-2022 3`). Take 0.5.1's `rt` feature fix too.
- `token.rs`: 0.4.5 does not export `impl_nondet_mint!`, and it instantiates
  `cvlr_nondet_token_mint` and `cvlr_nondet_token_2022_mint`, which 0.5 dropped. Those two
  functions need their own docs. The exported macro's docs do not apply.
- Three cross-references to `cvlr-spl-token` (in `lib.rs`, `token.rs` and `macros.rs`) name a
  crate the 0.4 line does not have.
- The CI docs check, from the same commit.

Release it on `v0.4` as 0.4.6, and pin its release commit in `SOLANA_V1_18` with a `GitCommit`
until it is published. That is how 0.5.1 and 0.6.0 are read (A5 in `docs/cvlr-sdk3-support.md`).

**Done locally**, on branch `eric/v0.4-docs` off `origin/v0.4`: the port (`5bc229d`) and the 0.4.6
bump with a changelog entry (`a5032ba`). The hand edits were the ones listed. The private
`impl_nondet_mint!` now forwards doc attributes, so the two mint functions carry their own docs.
CI's checks pass: `cargo doc -D warnings -D missing_docs` with all features (so `rt` builds),
clippy and fmt. Pushed to `Certora/cvlr-solana` and opened as
[#23](https://github.com/Certora/cvlr-solana/pull/23), squash-merged into `v0.4` as `b1988a4` (the
same tree as `a5032ba`). `SOLANA_V1_18` pins `b1988a4`, and a crate declaring the set resolves and
compiles it from GitHub.

### G4. Scope the API corpus to 1.18

`composer/scripts/cvlr_api_docs.py` documents every generation in `REFERENCE_SET`, each in its own
probe crate, and the API tools already filter to the run's pins (A6). Once G1 and G3 are in:
regenerate, check that `cvlr-solana` 0.4.6 appears as its own surface beside 0.5.1 and 0.6.0, read
the `--undocumented` report, and republish (`scripts/populate_cvlr_rag.sh`).

The probe crate builds against `solana-program` 1.18 on the host toolchain. If it meets G6's
problem, document it on the SBF target instead.

**Checked** with `--crate-source` on the 0.4 checkout: the 1.18 probe builds on the host,
`cvlr-solana 0.4.6` is its own surface (34 items) beside 0.5.1 (33) and 0.6.0 (32), the
`--undocumented` report lists 0 of 380 items, and all 19 doc examples compile. Ingested
into the local corpus: 399 sections (35 of them `cvlr-solana` 0.4.6), and a lookup filtered to the
1.18 pins returns the 0.4.6 surface and its token summaries. The import ran from the project venv:
the script's `uv run --isolated` spent 30 minutes downloading CUDA torch and was stopped.

### G5. Knowledge text: what the manual gets wrong on 1.18

The manual (`cvlr_kb`) targets `cvlr-solana` ≥ 0.5, and it sends authors to `cvlr_spl_token` for
token CPIs. On 1.18 that crate does not exist. An author following the manual writes an import the
compile gate rejects, and spends attempts on it.

**What 1.18 does have.** 0.4.5's `cvlr_solana::token` has the balance-moving summaries the 0.5
line moved into `cvlr-spl-token`: `spl_token_transfer`, `spl_token_2022_transfer`, `spl_mint_to`,
`spl_burn` and `spl_close_account`, with `spl_token_account_get_amount` and `spl_mint_get_supply` to
read balances back. They take `AccountInfo`s, not an `Instruction`. What 1.18 lacks is
`cvlr-spl-token`'s CPI mocks: the `cvlr_invoke_*` functions the Prover substitutes for `invoke` and
`invoke_signed` by instruction discriminant. So a token CPI left alone reaches
`invoke_signed_unchecked`, which the core summaries make opaque: no balance moves and the result is
arbitrary. A property that needs the transfer is still provable, through the author prompt's
existing `swap_import` route: the stand-in for the program's token call calls the summary.

**Implemented** as `ChainReference.caveats`, text a generation carries because the manual says
otherwise. `CvlrDeps` takes it from the run's reference set, and `cvlr_research.j2` renders it,
under *Where the manual is wrong for this build*, in both the author's and the judge's system
prompts. It is empty on 2.x and SDK 3, so their prompts are unchanged. The manual itself stays
single-generation, as A6 decided.

### G6. Run the compile gate against the SBF target

**Decided.** It is needed for klend whatever the SDK.

`HOST_RUST_TOOLCHAIN` (1.90.0) cannot `cargo check` klend. `farms` asserts its account sizes with
`static_assertions` and `bytemuck`'s `derive(Pod)` padding check, and rustc 1.77 raised `u128`
alignment to 16 on x86_64. The same structs have alignment 8 on SBF, which is what the program is
built for and what the Prover analyzes. No single host toolchain works: 1.74.1 predates the
lockfile cargo writes, and anything from 1.77 on breaks the layout (`docs/who-edits-the-program.md`
§18.3).

So check the target the Prover reads, with the compiler that builds it.

**Implemented** as `composer.cargo.sbf.sbf_check`, a `CrossCheck` that a `CargoSession` runs in
place of its host check (`check_with`). It runs the platform-tools `cargo` directly, with no rustup
link, since confinement cannot write `RUSTUP_HOME`. It passes `--release --target
sbpf-solana-solana`, and the environment `cargo certora-sbf --no-rustup` gives its own cargo:
`RUSTC`, and `CC`, `AR`, `OBJDUMP` and `OBJCOPY` from the platform-tools LLVM. The session's
`warm` now fetches with that cargo by default, because a confined check is offline. Preflight
(`gate_workspace`) and the shared build tree (`pipeline.py`) use it, so the author's `cargo_check`
and the pre-submission gate do too. A missing platform-tools release fails preflight by name. The
workspace read (`cargo metadata`) and the `cargo certora-sbf` launcher stay on `HOST_RUST_TOOLCHAIN`.

**Matching the build matters.** On the probe, measured by hand:

| Check | klend |
|---|---|
| debug profile, no build environment, cold | 132s |
| `--release` with the build's environment, cold | 28s (host check: 25.6s, §18.3) |
| …after an edit to the program | 4.2s |
| …with nothing changed | 0.3s |

Without the build's environment, the build after a check recompiled 16 crates. `blake3`'s build
script reruns when `CC` changes, and everything above it followed. With it, the build after a
check recompiled nothing, and after an edit, only the program. In the debug profile the check
shares no host units with the release build.

Confined under the launcher sandbox, preflight's gate on a fresh 2.x workspace passes in 9.3s. The
platform-tools root was already granted to builds.

This **applies to every target**, not only 1.18. It also removes the class of failure where the
host compiles code the SBF build does not, or the reverse.

### G7. Tests

Done:

- `select_generation` picks `SOLANA_V1_18` for a 1.18 workspace and none for a 1.17 one. Preflight
  refuses 1.17 naming all three generations. The scaffold refuses a 1.17 project on the 1.18 set
  with *move the project to solana-program 1.18*. Existing 2.x and SDK 3 selections are unchanged
  (`test_cvlr_scaffold.py`, `test_cvlr_entry.py`).
- The 1.18 set shares the core, declares only `cvlr` and `cvlr-solana`, has no aliases, and refuses
  `withholding` (`test_cvlr_reference.py`).
- The caveats reach both system prompts on 1.18 and neither on the other lines
  (`test_cvlr_knowledge.py`).
- `tests/data/solana_1_18_sbf_symbols.txt`: 21 platform symbols from the probe build, with a
  second rule calling each platform function the tuning files address. Every one is matched by a
  directive that names it, not only by a blanket, except two `Debug::fmt` impls
  (`test_cvlr_env_paths.py`).

There is no fast test that the set resolves. The other sets have none either. Their resolution is
exercised by the API-docs probe crates, which is G4.

Open:

- An expensive end-to-end on a 1.18 target, after G6: scaffold, compile gate, build, one rule
  submitted.

## Token CPIs: `cvlr-spl-token` for 1.18 is deferred

The 0.4 line has no SPL Token CPI mocks. They were added after 0.4.5 and then moved into
`cvlr-spl-token`, which exists only from 0.5. It does have the balance-moving summaries (G5).

**klend's benchmark does not need it.** Every property in the published answer key
(`docs/klend-benchmark.md`) is about klend's own accounting: reserve liquidity against the
collateral supply, obligation deposits, borrows and LTV, and liquidation amounts. None reads a
token account balance. With token CPIs opaque (G5), those properties are stated and proved the same
way.

**What it would cost a run:** a generated property about a token movement ("a deposit moves
exactly `amount` into the vault") needs a munge: a `swap_import` stand-in for the program's token
call that calls the `cvlr_solana::token` summary. On 0.5 the Prover substitutes the mock itself.
That is one editor round per call site, not a property that cannot be proved. These properties are
outside the answer key, so they are triaged, not scored.

**Revisit if** the first klend run spends a significant part of its budget on token-balance
properties. The port looks small: about 120 lines of CPI mocks on `cvlr-solana`'s token module,
which differs by six lines between 0.4.5 and 0.5.0. But it is a new crate on the 0.4 line, and its
CPI substitution would need checking on 1.18.

## The input

Outside AutoProver, but a precondition for the klend run, and to be reported with its results. On
the benchmark input, one labelled build-infrastructure commit:

- Take the member that needs the unreadable git repository out of the workspace. It is not part of
  the program.
- Move workspace `solana-*` from `~1.17.18` to `~1.18`, and re-resolve the lockfile, which brings
  `ahash` 0.7.8 and 0.8.12.

The lockfile comes out as version 4, which klend's own pinned cargo (1.74.1) cannot read. AutoProver
never runs that cargo, so this only affects people building the input by hand.
