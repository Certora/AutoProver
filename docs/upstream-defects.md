# Upstream defects found while building the CVLR backend

Everything here was found by pointing the CVLR backend (`docs/cvlr-backend-plan.md`) at real Anchor
programs, and every entry is reproducible. Nothing here is filed anywhere yet — this document exists
so that routing is a decision somebody makes rather than one I guess at, since the two groups below
belong in different places and one of them cannot be public.

**Group P — the Solana Prover.** Closed source, so these can only be reported. **None of them blocks
Anchor verification on the path real specs use**, and an earlier draft of this file said P1 did. Production verifies
Anchor programs by depending on `Certora/anchor`, a maintained fork whose `Error` is unboxed — see P1
for how that was missed and what is left of the defect.

**Group U — `prover_output_utility`.** The library the report stack reads verdicts through. One
entry, and unlike the others it was corrupting our own output rather than merely obstructing us;
`composer/spec/source/report_prover.py` now works around it.

**Group T — the recommended starting template.** A public repository, and every one of these has a
concrete fix we have already verified locally. `composer/spec/cvlr/env_paths.py` works around T1 at
emit time; the rest are worked around by not reproducing them.

One note on naming, because it constrains how T2 is written up: this repository is public, so no
client, project or repository names appear in it. T2 is about a canonical file that carries one, and
the write-up gives the line number rather than the name.

| id | defect | severity |
|---|---|---|
| [P1](#p1) | `Box::new` of a stack-built struct rejected as an illegal stack-pointer store | worked around upstream |
| [P2](#p2) | Un-inlining a call with no summary kills the job in 3.6s with no diagnostic | major |
| [P3](#p3) | `solanaOptimisticJoinWithStackPtr` is documented as a conf key and is not one | minor |
| [P4](#p4) | `-solanaAggressiveGlobalDetection` does not fix the [3308] it is recommended for, and neither does any summary | minor — **not** blocking; see the correction |
| [P5](#p5) | A [3308] in the generated vacuity check is reported as a clean `VERIFIED` when `rule_sanity` is off | **critical** |
| [U1](#u1) | `extract_job_id_from_url` cannot parse a Solana Prover job link | **major**, worked around |
| [T1](#t1) | Tuning files are spelled for pre-2.2 `solana-program` paths | major |
| [T2](#t2) | A canonical tuning file names one specific on-chain program | hygiene |
| [T3](#t3) | `[package.metadata.certora] sources` omits `Cargo.toml` | major |
| [T4](#t4) | `README` names files the template does not ship | minor |
| [T5](#t5) | `certora/mod.rs` never declares `utils.rs` | minor |
| [T6](#t6) | `[workspace.dependencies]` pins CVLR by hand | minor |
| [T7](#t7) | Nothing points a new project at the Anchor fork it needs | **major** |
| [T8](#t8) | A canonical summary is spelled for a `RawVec` symbol current toolchains no longer emit | major |

---

## P1

### `Box::new` of a stack-built struct is rejected as an illegal store of a stack pointer

**This is already solved in practice, and the first draft of this entry did not know that.** The
effect is real — no path that constructs an Anchor error can be analyzed — but it applies to the
*crates.io* `anchor-lang`, and production does not use it. `Certora/anchor` carries a branch per
upstream release (`certora-v0.26.0` … `certora-v0.32.1`) whose `Error` is unboxed, and a verification
project depends on that. The reference project's verification branch pins exactly that fork at
`certora-v0.31.1`; its non-verification branch uses crates.io, which is why a glance at the wrong
branch shows no fork at all.

So this is a defect with a maintained workaround, not a blocker. Still worth reporting — `Box::new` of
a stack-built value is ordinary Rust, and rejecting it means every Anchor project carries a forked
dependency in perpetuity — but the severity is "upstream should not need a fork", not "Anchor cannot
be verified".

**How the mistake happened, since it is the more useful finding.** The scaffold this backend writes
depends on crates.io Anchor, because the template it follows does. So the backend reproduced a
condition no real project is in, hit the resulting error, and I wrote it up as a property of the
prover without checking what real projects depend on — which was one `grep` over the local checkouts.
Generalised: *before reporting that a tool cannot do something, check what the people who do it every
day actually run.*

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

**Three workarounds attempted from inside the configuration, none of which works.** Each was a
separate cloud submission; details under P2, P3 and below. All three are beside the point given the
fork, and are kept because two of them turned up separate defects.

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

**What would resolve it** so that no fork is needed: treat a stack-to-heap *move* as distinct from a
stack pointer *escaping*. Failing that, give the tuning files a way to say "do not analyze this body,
and here is its result" that works on a function in the inlining allowlist — the summary attempt above
suggests they cannot today, which is arguably the more tractable ask.

**And a fourth workaround, which is the one that works.** Depend on the fork. This backend now writes
that `[patch.crates-io]` itself ([munge.py](../composer/spec/cvlr/munge.py)); the gap it fills is T7.

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

## P4

### `-solanaAggressiveGlobalDetection` does not fix the [3308] it is recommended for

[3308] "illegal dereference of an absolute address" comes with the prover's own remedy in the help
text:

```
help: To resolve this error consider one of the following:
	(1) add prover option "-solanaAggressiveGlobalDetection true"
```

It does not work. Measured on the P1 reproducer with the Anchor fork in place: the flag reached the
jar (confirmed in the job's `jarSettings` as `['-solanaAggressiveGlobalDetection', 'true']`) and the
error is byte-identical with and without it, same trace and same dev message.

The trace starts in the program's own `#[error_code]` enum and runs through `ToString` / `Display`
into `String::push_str` → `Vec::extend` → `copy_nonoverlapping`:

```
[3308] illegal dereference of an absolute address
source: .../core/src/intrinsics.rs:2978
  from: .../alloc/src/vec/mod.rs:2147          (Vec::extend_from_slice)
  from: .../alloc/src/string.rs:1065           (String::push_str)
  from: .../core/src/fmt/mod.rs:1627
  from: programs/vault/src/lib.rs:133          <- #[error_code] pub enum VaultError
  from: programs/vault/src/lib.rs:75           <- the `?` that builds the error
Dev message(0xd840): dereference of an absolute address 1 (0x1) at *(u32 *) (r2 + 15) = r3
```

Worth noting what is *not* the cause, since it was the first guess: `cvlr_summaries_core.txt:104`
summarizes `alloc::fmt::format::format_inner`, and that symbol is present in this binary, so the
summary does apply. The failing path reaches `String` through `ToString`/`Display` rather than
through `format_inner`, which the summaries do not cover.

**Two asks**, either of which resolves it: make the flag actually identify the `#[msg("...")]` string
literals as globals, or ship a summary for the `ToString`/`Display`-into-`String` path in the core
env file, alongside the `format_inner` one that is already there.

**Severity — corrected, and it was wrong in the reassuring direction.** This section previously read
"major rather than blocking, because it only bites a rule that goes through Anchor's full `entry`
dispatch", and concluded that it "bounds an edge case". That was inferred from the reference project's
harness — zero occurrences of `entry`, four of `Context::new` — and the inference does not hold.

A rule that builds a context and calls the handler directly reaches this path whenever the handler's
own error path constructs an `#[error_code]` value, which is what `require!` and `?` expand to. An
end-to-end authoring run measured it: of three units against one small Anchor vault, two could
formalize nothing at all. `deposits` skipped all five of its properties and `withdrawal_fee_distribution`
ten of twelve, both blocked on exactly this, both calling `crate::vault_program::<handler>` directly
and neither going anywhere near `entry`. 17 of 24 properties were lost to it.

So: **blocking**, for any handler that can return a program-defined error — which is most of them.
The misleading remedy is the smaller half of the report. The larger half is that the only workaround
is a summary for `<Error as Display>::fmt`, which means every Anchor project needs a hand-written
tuning entry that nothing tells it to write (compare T7).

**The summary remedy does not work either, and this is now measured rather than assumed.** An
authoring run given a tool to add its own summaries spent nine submissions on eleven directives
against one vault's `deposit`/`withdraw` error path. Seven of the eleven matched real symbols in the
build — `core::fmt::write`, `<&T as core::fmt::Display>::fmt`,
`<anchor_lang::error::ErrorCode as core::fmt::Display>::fmt`,
`<alloc::string::String as core::fmt::Write>::write_str`/`write_char`, `ConvertVec::to_vec`,
`drop_in_place<String>` — and **the error trace was byte-identical across all of them.** The other
four named the program's own `VaultError::Display`, `ToString::to_string`, `String::push_str` and
`Vec::spec_extend`, none of which is a symbol in any build of that program: rustc inlines them into
the handler.

That is the whole finding. The code the pointer analysis objects to ends up inlined *inside the
handler function*, so there is no symbol to address and summaries cannot reach it at any level. A
summary for the `ToString`/`Display`-into-`String` path in the core env file — this section's second
ask — would very likely fail for the same reason. **The fix has to be in the pointer analysis, or in
whatever controls inlining for the analyzed build.**

`composer/spec/cvlr/tuning.py` gives the loop the tool anyway, because a project whose blocking
symbol *does* survive is worth unblocking, and because a run that tries and reports precisely why it
failed is worth more than one that gives up without evidence — the eleven-directive enumeration
above is that run's own account, carried into its report.

**And the flag set real engagements run does not fix it either — measured, matched pair.** The three
Anchor projects verified against this prover carry a much larger `prover_args` than this backend's
default: `klend-audit` has it in **39 of 39** confs and `smart-account-program-audit` in its
`base.conf`. The full set adds the five `-solanaOptimistic*` memory-model flags,
`-solanaAggressiveGlobalDetection true`, `-solanaRemoveCFGDiamonds true`, `-solanaSlicerIter 6`,
`-solanaEnablePTAPseudoCanonicalize false` and `-solanaPrintDevMsg true`. Two jobs were submitted
from one tree, one harness and one fork commit, differing in nothing but those ten flags:

| rule | drives | baseline | engagement flags |
|---|---|---|---|
| `rule_withdraw_debits_exactly_the_amount` | `withdraw`, past `require!` | [3308] | [3308] |
| `rule_prompt_example_verbatim` | `deposit` | [3308] | [3308] |

**The error text is byte-identical between the two jobs** — same trace, same
`dereference of an absolute address 1 (0x1) at *(u32 *) (r2 + 15) = r3`. The flags did reach the jar
(they appear in the job's inputs, and the emitted TAC differs), so this is a negative result rather
than a misconfiguration. `-solanaOptimisticMemcpyPromotion` was the specific hope, since the trace's
innermost frame is a `copy_nonoverlapping`; it makes no difference.

The two traces land on the two `?`-on-`VaultError` sites — `lib.rs:49` in `deposit` and `lib.rs:75`
in `withdraw` — both by way of `lib.rs:133`, the `#[error_code]` enum, through
`ToString::to_string` → `String` → `Vec::spec_extend` → `copy_nonoverlapping`. That is the same chain
this section already describes, now confirmed from two independent handlers.


## P5

### A [3308] in the generated vacuity check is reported as a clean `VERIFIED` when `rule_sanity` is off

The most consequential entry in this file, because it is the only one whose failure mode is a green
result rather than an error, and it was found by accident while measuring P4.

`rule_sanity` makes the prover generate a companion `rule_not_vacuous_cvlr` subrule per rule. When
the pointer analysis fails with [3308] on the rule's path, **the failure is attributed to that
generated subrule**, and the rule's own `Assertions` subrule comes back `VERIFIED` — "determined
without running SMT solver (i.e. solely by static analysis)", `attempted 0 splits, proved a total
weighing 0 %`. With `rule_sanity` on, the rule's roll-up status becomes `ERROR` and the [3308] is
visible. With it off, there is no vacuity subrule to fail, and the same build on the same tree
reports:

```
rule_prompt_example_verbatim  -> VERIFIED  (7ms)
```

with no error, no warning and no dev message anywhere in the results tarball.

**Reproducer — a matched pair, identical but for `rule_sanity`.** One tree, one harness, one fork
commit, the default `prover_args`:

| `rule_sanity` | `Assertions` | vacuity subrule | rule status | [3308] anywhere in output |
|---|---|---|---|---|
| `none` | VERIFIED (7ms, 0 % proved) | not generated | **VERIFIED** | **no** |
| `basic` | VERIFIED (6s, static only) | ERROR [3308] | ERROR | yes |

Both rows reach the handler: the `basic` row's trace runs through the handler's own `?` on line 49.
So the code was analyzed, the analysis failed, and in the first row the run reported success.

**Why this is worse than an error.** A rule whose body is unreachable passes trivially, which is what
"determined solely by static analysis, 0 % proved" describes — and the check designed to catch that
is the one the [3308] disabled. The prover therefore cannot distinguish "your rule is vacuous" from
"the analysis could not proceed", and off the sanity path it calls both of them verified.

**What would resolve it**: report a [3308] raised while analyzing a rule against the *rule*, not
only against whichever generated subrule happened to trigger it. Failing that, refuse to emit
`VERIFIED` for a rule discharged at `0 %` with a pointer-analysis error recorded anywhere in its
analysis.

**Not worked around, and it does not need to be here**: this backend's conf default is
`rule_sanity: "basic"` (`composer/spec/cvlr/conf.py`), which was added for an unrelated reason — to
catch the rule a blocked author writes by assuming the conclusion. It is what makes this visible, and
it is now load-bearing for soundness rather than merely useful. Any project that turns it off is
exposed.

---

**CORRECTION — this was never blocking, and the cause was ours.** Everything above is still true: the
recommended flag does not work, summaries cannot reach the inlined code, and the engagement flag set
makes no difference. What was wrong is the conclusion drawn from it — that Anchor handlers with error
paths cannot be verified. They can, and the corpus has been doing it all along.

**The rule's own idiom decides it.** A rule that consumes the handler's `Result` with `.unwrap()`
verifies; the same rule, same program, same property, written `if handler(..).is_ok() { assert }`
returns [3308]. Measured as two rules in a single job on the stock fork:

| rule | idiom | result | vacuity check |
|---|---|---|---|
| `rule_withdraw_unwrap` | `h(..).unwrap(); assert` | **VERIFIED** | VERIFIED, `satisfy reached` |
| `rule_withdraw_is_ok` | `if h(..).is_ok() { assert }` | ERROR [3308] | ERROR |

`.unwrap()` makes the failure path a `panic`, and `assert_on_panic` defaults to false — the CLI's own
`default_desc` is *"Rust panic functions are not treated as assertion violations"* — so the prover
prunes that path and the error is never constructed. `.is_ok()` keeps the failure path live and merges
it back, forcing the analysis through the `#[error_code]` enum's generated `Display` and its `String`.

The two forms assert the same property over the same states, so this is not a weakening. It is simply
the idiom every verified project already uses: across three engagement repositories the counts are
6385 / 218 / 146 uses of `.unwrap()` against 87 / 36 / **0** of `.is_ok()`.

**Why we did not see it.** The `.is_ok()` form was in *our own author prompt's worked example*, so
every run reproduced it, and the resulting [3308] was read as a property of Anchor rather than of the
example. §7.6.2 of the plan recorded the clue and it was not followed: the control tier there "passes,
because it uses `.unwrap()` — a panic, assumed away". That generalises to the handler call, and the
inference was not made. The prompt now teaches `.unwrap()` and says why, including the coupling to
`assert_on_panic` — with that flag on, an `.unwrap()` that can fail asserts the handler never fails,
which is a different claim.

**What remains genuinely upstream**, and why this section is kept rather than deleted: the flag in the
title still does not do what its help text promises, and a project that legitimately needs to reason
about a failure path — a "rejects a bad input" property — still cannot, because that requires the
error value the analysis refuses. That is a real limit, now correctly scoped to a property class
rather than to every handler.

**Candidate fix, still valid and now measured.** Independently of the idiom, the allocation can be
removed at its source. On `Certora/anchor` both *readers* of `AnchorError`'s strings are already
no-ops — `impl Display` returns `Ok(())`, `log` is empty — while both *writers* remain
(`lang/attribute/error/src/lib.rs:110,112` and `lang/syn/src/codegen/error.rs:82,84`). Setting
`error_name` and `error_msg` to `String::new()` — four lines, no API change — makes the `.is_ok()`
form verify too, measured on a locally patched fork: both rules VERIFIED, both vacuity checks
VERIFIED. That would restore failure-path reasoning, and it is a small PR to a repository Certora
owns. `certora-v0.29.0` and `certora-v0.31.1` are byte-identical at all four sites, so one change
applies across branches.

## U1

### `extract_job_id_from_url` cannot parse a Solana Prover job link

`prover_output_utility.api.url_utils.extract_job_id_from_url` recognizes two shapes:

```python
# https://prover.certora.com/output/userid/jobid/...
# https://prover.certora.com/job/jobid
```

The Solana Prover emits neither. Its report link — the one `certoraSolanaProver` prints, and the one
the CLI records — is:

```
https://prover.certora.com/jobStatus/<userId>/<jobHash>?anonymousKey=<key>
```

There is no `jobStatus` branch, so the function raises `ProverAPIError: Could not extract job ID from
URL`. Reproduced directly against a green job:

```
File ".../prover_output_utility/api/url_utils.py", line 125, in extract_job_id_from_url
    raise ProverAPIError(f"Could not extract job ID from URL: {url}")
```

**Why it mattered more than an obstruction.** `get_all_checks` is how the report reads per-rule
verdicts, and the caller treats any failure as "no verdicts". So a CVLR run whose every rule VERIFIED
produced a report in which every rule was `UNKNOWN`, with no line numbers and no durations — a
false negative in the deliverable, arrived at silently, from a run that was green. The job's own
`output.json` said `SUCCESS` for all seven rules at the same time as the report said `UNKNOWN`.

**The fix upstream** is a `jobStatus` branch taking the segment after the user id. **The workaround
here** is that POU's public entry points accept a bare job id as well as a URL, so
`composer/spec/source/report_prover.py` extracts the id and hands that over, passing every shape POU
already parses through untouched.

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

Line number rather than the name because this repository is public — and this backend had *copied*
the line, name and all, into its own canonical layer, so the same name sat in this public repository
until it was replaced with the generic
`^.+ for anchor_lang::error::Error>::from$`. That generic form is not an invention: it is what the
three engagement projects carry in place of the named line, so the template's version is behind its
own users. The replacement is also strictly more useful, since the named line matches nothing on any
other program while the generic one matches every program's own error conversion.

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

## T7

### Nothing points a new project at the Anchor fork it needs

The template is the recommended way to start a Solana spec, and it does not mention
`Certora/anchor`. A project scaffolded from it depends on crates.io `anchor-lang`, and the first rule
that calls a handler fails with [3006] — a message about pointer analysis, with no indication that a
fork exists or that every real Anchor verification uses one.

This is the defect that actually cost time here, and it is a template and documentation gap rather
than a technical one. Two things would close it: a `[patch.crates-io]` stanza in the template's
workspace manifest carrying the branch table, and a line in the README saying why.

Major because of the shape of the failure, not its difficulty: somebody hitting it has no path from
the symptom to the answer.

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

---

## T8

### A canonical summary is spelled for a `RawVec` symbol current toolchains no longer emit

`cvlr_summaries_core.txt` carries the one directive in the whole set whose comment describes the
dangling-pointer precondition that [3308] is about:

```
;;; - precondition: (*i64)(r1+0) is a Rust dangling pointer
;;; - post-condition: (*i64)(r1+0) points to new allocated memory (malloc)
#[type((*i64)(r1+0):ptr_heap)]
^alloc::raw_vec::RawVec<T,A>::reserve_for_push(_[0-9][0-9]*)*$
```

`reserve_for_push` is not defined in a program built with platform-tools v1.43. Read back with
`composer/cargo/symbols.py`, the alloc-related functions that *are* defined are
`RawVec<T,A>::grow_one`, `raw_vec::finish_grow`, `RawVec<T,A>::reserve::do_reserve_and_handle`,
`raw_vec::handle_error`, `raw_vec::capacity_overflow` and `alloc::handle_alloc_error`. Upstream Rust
renamed the push-growth path: `reserve_for_push` became `grow_one`. The sibling directive for
`do_reserve_and_handle` still matches.

So on a current toolchain the directive is inert, silently — which is P2's complaint from the other
side: a summary that matches nothing produces no diagnostic. Every project surveyed carries the same
stale pattern and none mentions `grow_one`, so this is inherited from the template rather than
anybody's local mistake, and it is not what separated our results from theirs.

Fix: add `grow_one` (and probably `finish_grow`) alongside the existing pattern rather than replacing
it, since older toolchains still emit the old name. More usefully, a way to *report* a directive that
binds to no symbol would have caught this the first time it went stale — `composer/cargo/symbols.py`
exists here for exactly that reason and could be pointed at the canonical files, not just at
author-written ones.
