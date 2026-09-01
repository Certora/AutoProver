# Upstream defects found while building the CVLR backend

Everything here was found by pointing the CVLR backend (`docs/cvlr-backend-plan.md`) at real Anchor
programs, and every entry is reproducible. Nothing here is filed anywhere yet — this document exists
so that routing is a decision somebody makes rather than one I guess at, since the two groups below
belong in different places and one of them cannot be public.

**Group P — the Solana Prover.** Closed source, so these can only be reported. Both are blocking:
together they mean an Anchor program's own code cannot currently be verified.

**Group T — the recommended starting template.** A public repository, and every one of these has a
concrete fix we have already verified locally. `composer/spec/cvlr/env_paths.py` works around T1 at
emit time; the rest are worked around by not reproducing them.

One note on naming, because it constrains how T2 is written up: this repository is public, so no
client, project or repository names appear in it. T2 is about a canonical file that carries one, and
the write-up gives the line number rather than the name.

| id | defect | severity |
|---|---|---|
| [P1](#p1) | `Box::new` of a stack-built struct rejected as an illegal stack-pointer store | blocking |
| [P2](#p2) | Un-inlining a call with no summary kills the job in 3.6s with no diagnostic | blocking |
| [P3](#p3) | `solanaOptimisticJoinWithStackPtr` is documented as a conf key and is not one | minor |
| [T1](#t1) | Tuning files are spelled for pre-2.2 `solana-program` paths | blocking |
| [T2](#t2) | A canonical tuning file names one specific on-chain program | hygiene |
| [T3](#t3) | `[package.metadata.certora] sources` omits `Cargo.toml` | major |
| [T4](#t4) | `README` names files the template does not ship | minor |
| [T5](#t5) | `certora/mod.rs` never declares `utils.rs` | minor |
| [T6](#t6) | `[workspace.dependencies]` pins CVLR by hand | minor |

---

## P1

### `Box::new` of a stack-built struct is rejected as an illegal store of a stack pointer

**Effect: no Anchor program can be verified on any path that can return an error.** Which is every
path through Anchor's dispatch, and every handler that uses `?` or `require!`.

Reproduced against a minimal Anchor 0.31.1 lamports vault
(`test_scenarios/solana_vault_idl` in this repository), harness
`tests/data/anchor_reach_probe.rs`, driver `tests/test_cvlr_anchor_reach.py`. certora-cli 8.18.0,
`emv-8.18.0.jar`, platform-tools v1.43.

The prover's own output:

```
[3006] illegal store of a stack pointer
source: .../certora-solana-platform-tools/out/rust/library/alloc/src/boxed.rs:218
  from: /CARGO_HOME/.../anchor-lang-0.31.1/src/error.rs:296
  from: /CARGO_HOME/.../anchor-lang-0.31.1/src/error.rs:21
help: A stack pointer escapes by being stored into an illegal memory segment (i.e., heap,
      account data, external memory, etc.)
Dev message(0x1c6b0): Pointer domain: stack is escaping:
      (Node605[Stack:RX (fwd=null),16256) is being stored into (Node620[Heap:X (fwd=null),112)
```

`error.rs:296` is the whole of the offending function:

```rust
impl From<AnchorError> for Error {
    fn from(ae: AnchorError) -> Self {
        Self::AnchorError(Box::new(ae))          // <- error.rs:296
    }
}
```

`error.rs:21` is `#[error_code(offset = 0)] pub enum ErrorCode`, whose generated
`impl From<ErrorCode> for Error` reaches the same construction.

This is not an unusual idiom. It is "build a value on the stack, move it to the heap" — what
`Box::new` is for — and the pointer analysis appears to treat the move's source address as an escape
rather than as a copy. The construct is byte-identical in anchor-lang 0.29.0, 0.30.1 and 0.31.1
(`src/error.rs:242` / `:296` / `:296`), so it is not a regression in any of them.

**Why the tuning files do not already prevent it.** `cvlr_inlining_anchor.txt` is a blanket
`#[inline(never)] ^.*anchor_lang.*$` plus a named allowlist, and lines 35 and 36 of that allowlist
force-*inline* exactly these two conversions — so their bodies are analyzed. Those lines exist so
that error *codes* stay observable, which is a real requirement; the two goals are in direct conflict
today.

**Graded reproducer, weakest tier first.** The boundary is sharp and worth recording precisely,
because it says the problem is narrower than "Anchor does not work":

| rule | traverses | result |
|---|---|---|
| `rule_vault_state_deserializes` | `Account::try_from`, borsh, arithmetic on real account data | **VERIFIED** |
| `rule_deposit_credits_exactly_the_amount` | one handler and its account-creating CPI | ERROR [3006] |
| `rule_dispatch_is_reachable` | `crate::entry`, full dispatch | ERROR [3006] |

The control tier also calls `Account::try_from`, and passes, because it uses `.unwrap()` — a panic,
assumed away under `-solanaAssertOnPanic false` — while the handler's `?` actually constructs an
`Error`. So account deserialization and post-state arithmetic over real account data both work. The
only thing that fails is error *construction*.

**Three workarounds attempted, none of which works.** Each was a separate cloud submission; details
under P2, P3 and below.

1. Removing the two `#[inline]` allowlist entries, so the blanket leaves the conversions
   un-inlined — see P2. Worse than the original.
2. `-solanaOptimisticJoinWithStackPtr true` — no effect, and its mechanism says why: the flag is
   about joining paths whose stack pointers diverge, and this is a store.
3. Summarizing both conversions in `cvlr_summaries_anchor.txt` (the layer the template ships empty),
   declaring `Error`'s layout — a 16-byte enum, so an 8-byte tag and a `ptr_heap` at offset 8. No
   effect, and the PTA node identifiers came back byte-identical to the baseline run
   (`Node605`/`Node620`), which indicates the summary was never applied rather than applied and
   insufficient. If points-to summaries are expected to work on a function that is also in the
   inlining allowlist, that is a fourth defect; if they are not, the interaction is worth
   documenting.

**What would resolve it**, in preference order from a caller's point of view: treat a
stack-to-heap *move* as distinct from a stack pointer *escaping*; or give the tuning files a way to
say "do not analyze this body, and here is its result" that works on a function in the allowlist; or
publish a supported way to keep error codes observable without analyzing the boxing.

---

## P2

### Un-inlining a call the prover has no summary for kills the job in 3.6 seconds with no diagnostic

Arguably worse than P1, because it discards the configuration instead of reporting a problem with it.

Reproducer: the P1 project, with lines 35 and 36 removed from `cvlr_inlining_anchor.txt` — that is,
the two error conversions left to the file's own blanket `#[inline(never)] ^.*anchor_lang.*$`.
Nothing else changed. **Reproduced twice.**

```
jobStatus: FAILED
errorString: null
startTime:  19:44:37.693
finishTime: 19:44:41.329      (3.6 seconds)
```

The cloud log ends at `Start <date>` with no further output, `resource_errors.json` is
`{"topics": []}`, and the local `certora_debug_log.txt` contains no error, warning or exception. The
CLI reports only that the job produced no results, so a caller sees a failure with no attributable
cause — and the natural next step, "the conf must be wrong", is wrong.

The edited file is well-formed: two directive lines removed, comments added using the file's own `;;`
syntax. The removal is exactly the change a reader of the file would make to stop a function being
analyzed.

**What would resolve it**: any diagnostic at all. Even "no summary for un-inlined function X" would
turn a dead end into a next step.

---

## P3

### `solanaOptimisticJoinWithStackPtr` is used as a conf key in practice and is not one

certora-cli 8.18.0 rejects it before upload:

```
Error when reading .../reach_probe_stackptr.conf:
solanaOptimisticJoinWithStackPtr appears in the conf file but is not a known attribute.
```

It works when passed through `prover_args` as `-solanaOptimisticJoinWithStackPtr true`.

Worth reporting because real project confs set it as a top-level conf key, so either they are
silently doing nothing on this CLI version, or the CLI dropped an attribute it used to accept. Either
answer is useful; a validator that names the key but not the alternative is a third possibility.

The same question applies to the five `solanaOptimistic*` memory-model flags and to
`solanaEnablePTAPseudoCanonicalize`, which appear as top-level conf keys in the same confs. We have
only tested the one.

---

## T1

### The tuning files are spelled for `solana-program` paths that no longer define anything

**Effect: fourteen directives silently do not apply on any modern target, and one of them is the
blanket that sets the never-inline default for the entire platform layer.** Nothing errors; the
prover simply runs a different configuration than the file describes.

`solana-program` stopped *defining* the platform types at **2.2** — not at 3.0. Checked against the
published sources: 1.17.18 and 1.18.26 carry a real `src/account_info.rs`; 2.2.1, 2.3.0 and 3.0.0 all
re-export, e.g. `pub use solana_account_info::{self as account_info, …}` at
`solana-program-2.3.0/src/lib.rs:587`. A demangled Rust symbol carries the *defining* crate's path,
so a directive anchored on `solana_program::account_info::AccountInfo` cannot match a symbol on any
target from 2.2 on.

Measured by building the P1 project for SBF and matching every directive against
`llvm-nm --defined-only … | rustfilt`:

| directive | names | symbol emitted |
|---|---|---|
| `cvlr_inlining_core.txt:7` | `^solana_program::.*$` — the never-inline blanket | matches **2** symbols in the whole platform layer |
| `cvlr_summaries_core.txt:95` | `solana_program::program::invoke_signed_unchecked` | `solana_cpi::invoke_signed_unchecked` |
| `cvlr_summaries_core.txt:60` | `…account_info::AccountInfo::realloc` | `solana_account_info::AccountInfo::realloc` |
| `cvlr_inlining_core.txt:116–122, 129` | the eight `AccountInfo` data / lamports accessors | `solana_account_info::AccountInfo::…` |
| `cvlr_inlining_core.txt:133–134, 138` | `Rent::minimum_balance`, `Sysvar for Rent`, `ProgramError::from` | `solana_rent::`, `solana_sysvar::`, `solana_program_error::` |
| `cvlr_inlining_anchor.txt:37` | `…From<solana_program::program_error::ProgramError>>::from` | `…From<solana_program_error::ProgramError>>::from` |

The blanket is the serious one. `cvlr_inlining_core.txt` is a suppression plus an allowlist of
exceptions; on a post-2.2 target the suppression covers almost nothing, so **the default for the
platform layer flips from never-inline to inline**, while the allowlist entries that would have
carved out the parts needing inlining are dead alongside it.

**This is already known at two places.** `cvlr_summaries_core.txt` carries duplicate blocks for
`solana_pubkey::Pubkey::create_program_address` (`:76`) and `find_program_address` (`:92`) beside the
`solana_program::` originals — the only split spellings anywhere in the four files. So somebody hit
this, fixed the two symbols that bit them, and the other fourteen were not revisited.

**Verified fix.** Renames, all confirmed against the symbol table rather than read off the module,
because `solana-program` is a *partial* facade and per-module reasoning gives the wrong answer:

```
solana_program::account_info   -> solana_account_info
solana_program::pubkey         -> solana_pubkey
solana_program::program_error  -> solana_program_error
solana_program::program_pack   -> solana_program_pack
solana_program::rent           -> solana_rent
solana_program::clock          -> solana_clock
solana_program::sysvar         -> solana_sysvar
solana_program::hash           -> solana_hash
solana_program::system_program -> solana_sdk_ids::system_program
solana_program::incinerator    -> solana_sdk_ids::incinerator

solana_program::program::invoke_signed_unchecked
      -> BOTH solana_program::program::invoke_signed_unchecked
         AND  solana_cpi::invoke_signed_unchecked

^solana_program::.*$ -> ^solana_[a-z0-9_]*::.*$      (a superset: correct on either generation)
```

Deliberately **not** renamed: `solana_program::program::{invoke, invoke_signed, set_return_data}` and
`solana_program::instruction::get_stack_height`, which are real functions in the monolith on this
generation and appear in the symbol table under the canonical spelling; and
`solana_program::poseidon::…`, which the generation does not have under any spelling.

Measured effect on the P1 project, against a 70-symbol fixture
(`tests/data/vault_sbf_symbols.txt`): directives matching at least one symbol go 4→15 (core
inlining), 6→7 (anchor inlining), 2→6 (core summaries); symbols reached go 6→35, 26→26, 2→4; **no
symbol loses coverage.**

Note this is a defect in the *files*, not in the prover: they were correct when written.

---

## T2

### A canonical tuning file names one specific on-chain program

`cvlr_inlining_anchor.txt:39` is an `#[inline]` directive whose regex names a particular program's
error enum and its module path. Two consequences. It is inert for every other project, which is
harmless; and a file presented as canonical carries the name of somebody's codebase, which is a
question about the file rather than about verification. The knowledge corpus we built separately
records this pattern as the *project-specific* idiom belonging in the per-package layer, which is
exactly where the template's own three-layer split says it should live.

Fix: move it to the package layer, or drop it.

Line number rather than the name because this repository is public.

---

## T3

### `[package.metadata.certora] sources` omits `Cargo.toml`

`.certora_sources` is what the report and the counterexample analyzer read, so a collected tree with
no manifest cannot be rebuilt. The public examples include it; the template does not. This one is
easy to miss because the job still ends VERIFIED.

Fix: `sources = ["Cargo.toml", "src/**/*.rs"]`.

Related, and separate: the prover's source collector **skips `.certora_internal`**. A project whose
build directory sits inside it uploads no Rust at all — measured as 7 `.rs` files from a plain path
versus 0 from under `.certora_internal` — and the job still ends VERIFIED either way. That is
arguably a fourth prover defect rather than a template one; it is recorded here because the two were
found together.

---

## T4

### `README` names files the template does not ship

The `README` refers to `solana_inlining.txt` and `solana_summaries.txt`. The shipped files are
`cvlr_inlining*.txt` and `cvlr_summaries*.txt`, so a reader following the README edits nothing and
gets no error.

---

## T5

### `certora/mod.rs` never declares `utils.rs`

The file ships and is never compiled.

---

## T6

### `[workspace.dependencies]` pins CVLR by hand

The template pins the CVLR line in the workspace manifest, so a project has two places that decide
what "current CVLR" means. Not a bug so much as a maintenance seam, and it dates: the pin is 0.4
while the current line is 0.6.
