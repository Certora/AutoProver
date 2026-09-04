# One working tree

> The per-unit workdir replaced by a single shared tree, per-unit cargo features, and a build
> permit — and the tree made derivable from checkpointed state rather than being state itself.
>
> Companion to [munge-and-working-copies.md](munge-and-working-copies.md), which describes the
> arrangement this replaces, and to [cvlr-backend-plan.md](cvlr-backend-plan.md) §7.5.2, whose
> "forced" answer this document argues is not forced. §7.10 scheduled a re-read of that reasoning;
> this is the first half of it.
>
> **Built.** §6 is done and §8's three cheap checks pass; what each turned out to be is recorded
> where it happened. The two that need the SBF toolchain and a real run are still open, and §8 says
> which.

---

## 1. The claim

[cvlr-backend-plan.md](cvlr-backend-plan.md) §7.5.2 answers open question 3 — one workdir or many —
with **per unit, and forced**, and rejects three alternatives. Two of those three are rejected on
grounds that do not survive contact with either cargo's actual behaviour or the corpus:

| §7.5.2's alternative | Its reason for rejecting | What is wrong with it |
|---|---|---|
| One workdir, a run-wide lock around stage-and-check | "The lock does not help: the *sibling file* is still broken on disk while A checks" | Correct, and it is why the lock is not sufficient **on its own**. A sibling file that is `#[cfg]`'d out is not compiled, is not in rustc's dep-info, and cannot break or dirty A's build |
| A `--cfg` or cargo feature per unit | "feature- and flag-varying builds get separate fingerprints anyway, so the cache cost is the same one it was meant to avoid — and it puts per-unit features into the target's own manifest" | Inverted. Feature-varying gives the **program crate** separate fingerprints — 4 of 519 artifacts by §2's own count. The dependencies keep one fingerprint and compile once, and the dependencies are the entire cost the per-unit workdir was avoiding. And per-unit features in the manifest is what the corpus already does |

The two rejected alternatives were considered separately. **Together they work**, and the third
mechanism — `mock_fn(when = …)` with the conf's `cargo_features` — makes the source edits additive
so that one tree has a single well-defined state.

The shape is not novel here. The Crucible backend runs exactly this way: one crate, one cargo feature
per unit, and `serialize_toolchain: true` so that — in its own words — "every campaign shares one
crate and one `target/`". Its `docs/crucible.md` §1 and §3 live on the `eric/crucible-app` branch
rather than in this tree. The semaphore that implements it is
already in this repo at [adapter.py:544](../composer/rustapp/adapter.py#L544), documented in
[rust-applications.md](rust-applications.md) §3, and covered by
[tests/test_rustapp_toolchain_sem.py](../tests/test_rustapp_toolchain_sem.py).

---

## 2. The mechanism

Three parts, each of which is inert without the others.

### 2.1 Per-unit cargo features gate the spec modules

`specs/mod.rs` declares each unit's module behind its own feature, and `[features]` gains one empty
feature per unit:

```rust
#[cfg(feature = "unit_deposits")] pub mod deposits;
#[cfg(feature = "unit_admin")]    pub mod admin;
```

```toml
certora = ["no-entrypoint", "dep:cvlr", "dep:cvlr-solana"]
unit_deposits = []
unit_admin = []
```

A unit gates with `cargo check --features certora,unit_deposits` and submits with the same set
through the conf's `cargo_features`, which
[prover.py's `resolved_features`](../composer/spec/cvlr/prover.py#L115) already threads so that "the
gate build and the prover's rerun cannot end up with different feature sets".

**The features must stay empty.** A unit feature that enabled a dependency feature would change the
deps' resolved feature set, give them separate fingerprints, and reinstate exactly the per-unit
dependency build this is removing. Same rule for the `when`-gated munge features in §2.3.

This also removes a live fragility: [harness.py](../composer/spec/cvlr/harness.py#L23) records that
"every declared module must exist as a file or *no* unit compiles". Under a `cfg` gate, a unit whose
file does not exist yet is invisible to everyone else.

### 2.2 rustc's dep-info is what makes it correct

rustc emits, per compilation, the list of source files it actually read; cargo fingerprints against
that list. A module behind a disabled `cfg` is never read, so it never appears in the dep-info, so
**the `unit_admin` variant's fingerprint does not depend on `deposits.rs`**.

That is precisely the property §7.5.2 bought the per-unit workdir to get — "unit A's gate fails
whenever unit B's draft is momentarily broken … not reproducible" — and it holds for dirtiness as
well as for breakage: A's edits do not force B to rebuild.

**Measured** (§8 item 3): with `unit_b`'s module made syntactically invalid on disk, `--features
certora,unit_a` finished in 0.00s having compiled nothing, while `--features certora,unit_b` failed
with the expected parse error.

### 2.3 `mock_fn(when = …)` makes the source edits additive

`cvlr::mock_fn` takes a `when` parameter (`cvlr-macros-0.6.1/src/mock.rs`) defaulting to `"certora"`,
and expands to the original function under `#[cfg(not(feature = when))]`. The corpus uses it to keep
a menu of mocks in the source, each dormant until a conf asks for it — kamino metavault declares
eleven such features under the comment *"Per-conf prover mocks; enabled via `cargo_features`. Inert
without `certora`."*, and klend-audit eleven more.

This backend used to do the opposite: `attribute_line` was always called with `DEFAULT_FEATURE`, so
every munge was on for every unit. In a shared tree that is a semantic leak — unit A's mock silently
applies to unit B's rules. Now [`FunctionMunge`](../composer/spec/cvlr/munge.py) carries the feature
that activates it, and `munge_function` records the recording unit's own. Two units' munges of the
same file coexist as two dormant lines, and the union of all units' munges is the single
well-defined content of that file.

**What was built gates the whole `cfg_attr` rather than using `mock_fn`'s `when`.** Both produce the
identical result — `mock_fn(when = "f")` expands the original function under `#[cfg(not(feature =
"f"))]`, and `#[cfg_attr(feature = "f", mock_fn(...))]` contributes no attribute at all — and the
`cfg_attr` form is one mechanism for both kinds, where `when` would be a second one that
`cvlr::early_panic` cannot use because it has no such parameter. The corpus gates `early_panic` by
wrapping the `cfg_attr` condition anyway, 60 times.

The evidence for the `when` idiom, and for the four munge kinds beyond the two this backend
implements, is in §7 below; it belongs in a revision of
[munge-and-working-copies.md](munge-and-working-copies.md) §4 rather than here.

### 2.4 The build permit

One `asyncio.Semaphore(1)` for the run, held across staging and the local cargo invocation.

Note what it is *not* for. Concurrent `cargo` invocations against one `target/` already serialize on
cargo's build-directory lock, so the serialization happens either way. The permit buys:

* a queue the host can report — "waiting for a build slot" instead of a silent stall behind cargo's
  own `Blocking waiting for file lock on build directory`;
* not having N sandboxed processes parked holding grants and file descriptors;
* one place to put ordering and fairness if a unit ever starves.

It does **not** make the shared tree correct. §2.1 and §2.2 do that.

**The permit stops at the prover run, and that needed a decision the plan had not reached.**
`certoraSolanaProver` executes the build script, so cargo runs again inside the submission — but the
submission also waits minutes on a cloud job, and a permit held across it would serialize *prover
runs* across the whole run, which is a far worse trade than the one §3 costs out. It does not have
to be held, because of what a sibling can actually change underneath an in-flight build: another
unit's harness module, which rustc never reads because it is `cfg`'d out, and a munged file, whose
new line is a `cfg_attr` on a feature this build does not enable. Both are inert. The one thing that
is *not* safe is a half-written file, so every derived write is `os.replace`-atomic and a concurrent
reader sees the old file or the new one and nothing else. [`submit`](../composer/spec/cvlr/prover.py)
is split into `prepare_submission` and `run_submission` so the loop can hold the permit across the
first and not the second.

---

## 3. The cost argument, inverted

All figures from [munge-and-working-copies.md](munge-and-working-copies.md) §2, measured on the
three-unit Solana test scenario.

| | Per-unit workdirs (N units) | One tree |
|---|---|---|
| source copy | 104 KB × N | 104 KB |
| `.sandbox_cargo/registry` | 119 MB × N, **re-downloaded per unit** | 119 MB, fetched once |
| `target/` | 955 MB × N, **dependencies recompiled per unit** | ~955 MB + N × the program crate's own artifacts |
| dependency builds | N | 1 |

§2's own finding is the whole argument: *"`target/debug/deps` holds 515 artifacts, of which **4**
belong to the program's own crate. The rest are dependencies that cannot differ between units — same
lockfile, same `certora` feature, same toolchain — rebuilt per unit for byte-identical results."*

Feature-varying duplicates the 4, not the 515. §7.5.2's "the cache cost is the same one it was meant
to avoid" is true of the program crate and false of everything that made the arrangement expensive.

**Measured, on a synthetic crate with one dependency and two empty unit features** (§8 item 2).
Building `--features certora,unit_a` then `certora,unit_b`: the dependency compiled once and was not
recompiled for the second feature set; only the package's own crate got a second `-C metadata` hash.
Returning to `unit_a` afterwards ran **zero** rustc invocations — both variants coexist in one
`target/`.

Both of §8's deferred optimizations are absorbed rather than merely deferred. "Shared read-only
registry" and "shared dependency build cache" were workarounds for the per-unit split; one tree is
both, and it sidesteps §8's own caveat that a shared `CARGO_TARGET_DIR` would likely rebuild anyway
because each unit's private cargo home puts dependency sources at a different absolute path.

**What gets worse is wall clock in steady state.** Units used to build genuinely concurrently —
N× the work, but max-latency rather than sum. Serialized, a gate can queue behind N−1 others, and
loop latency is §9's top risk. The trade is a much cheaper cold start for a worse warm gate under
contention. On a cold run the sum should still beat the old max, because each queued gate is one
crate-variant recompile rather than a dependency build; on a warm run it will not. Only the local
build queues — §2.4 keeps prover runs concurrent, which is where the minutes are — so the queue is
seconds deep, not minutes. §8 item 5 is the measurement, and it has not been taken.

---

## 4. Resume: the tree is derived, not state

This is the part that has to be right for a cached run to be replayable, and most of it is already
built.

### 4.1 The tree is already a pure function of checkpointed state

[`HarnessTarget.stage`](../composer/spec/cvlr/verify.py#L136) runs before every build and writes the
draft, then replays the summaries and the munges out of graph state:

```python
self.module_path.write_text(draft)
if summaries: self.tuning.write(tuple(summaries))
for munge in munges:
    match apply_munge(path.read_text(), munge, DEFAULT_FEATURE): ...
```

Every input is checkpointed. `munges` and `summaries` are reduced state keys
([state.py](../composer/spec/cvlr/state.py#L178-L183)); the draft is the authoring buffer, of which
state.py says *"The ground truth is the buffer, not the run."*
[`apply_munge`](../composer/spec/cvlr/munge.py#L598) is pure, and
`TuningFiles.write` is already documented as *"rewritten wholesale from `directives` rather than
appended to, so the state the run recorded and the file on disk cannot drift."*

So:

```
tree = pristine project + buffer + munges + summaries
```

and every term after the first is in the checkpoint.

**This is a stronger position than the EVM backend's, not a weaker one.** EVM must checkpoint its VFS
overlay — `codegen_store.recovery_from_thread` snapshots `channel_values["vfs"]` verbatim — because
its edits *are* file text: an agent rewrote a Solidity file and the resulting bytes are the only
description of what it did. CVLR's edits are records and the text is derived. It is also why
`merge_vfs` is append-only with no delete tool (a text overlay cannot express undo) where dropping a
`FunctionMunge` from a list can. That answers the *representation* half of
[munge-and-working-copies.md](munge-and-working-copies.md) §8 gap 3 and not the tool half: nothing
removes a munge from state today, so removal happens on a rewind or a replay and not on an author
changing its mind.

**Therefore: no VFS.** Adding one would checkpoint bytes that are already derivable, and would pay
§3's step (5) — cargo's input is source *plus* `CARGO_HOME` *plus* `target/`, so a materialized
snapshot is a cold fetch and a full rebuild per compile check. Reconciling in place is the right
shape precisely because the expensive parts must persist.

### 4.2 The one line that broke it

```python
def _copy_workspace(source: Path, dest: Path) -> None:
    if dest.exists():
        _log.info("cvlr: reusing the existing workdir at %s", dest)
        return
```

Its docstring's premise — *"a resumed run should find the workspace it left, including whatever the
last session staged"* — is what failed, and it failed for reasons unrelated to how many trees there
are:

* a crash between a tool call and the checkpoint write leaves the tree **ahead** of state;
* resuming a checkpoint that is not the latest leaves the tree ahead by an arbitrary amount, and
  `stage` never *removed* a munge, so a rewound run silently inherited one its state no longer knew
  about;
* **a cache replay has no tree at all**, so "reuse what is there" is not resume in the first place.

The tree still gets copied only once — the build output and the sandbox's `CARGO_HOME` live in it,
and re-copying would throw away the warm cache the whole arrangement exists to keep. What changed is
that nothing the run put in it is trusted: every derived file is rewritten from state on every
stage. The tree stops being state and becomes a build cache, and losing it costs a rebuild and never
correctness.

### 4.3 Three care points, all of them real

1. **Replay the union of all units' munges, not the current unit's.** Replaying one unit's is right
   for N trees and wrong for one: after a cold rebuild, unit B's stage would leave A's munge lines
   absent until A next stages, and units would fight over the file on alternating gates. Feature
   gating (§2.3) is what makes the union safe, and it is the only stable fixed point.
   `SharedTree` accumulates each unit's edits and replays all of them — and the file a unit stops
   munging is found from the tree's own note of what it derived, since nothing in state names it.
2. **Content-compare before writing.** Cargo fingerprints on mtime, so rewriting identical bytes
   forces a rebuild and would make every resume a cold one. Read, compare, write only on difference —
   and replace atomically, because §2.4 leaves the prover's own build script reading these files
   outside the permit.
3. **Surface the drift, do not log it.** Now `Reconciled.drifted`, appended to whatever the gate
   tells the author, on every branch — a munge that did not reach the build is as much a part of why
   a rule failed as of what a passing rule means. Reconstruction is a function of the *pristine
   project*,
   which is the developer's tree and can move between a cached run and its replay. Neither backend
   pins it — EVM's `fs_layer` has the same exposure. But `apply_munge` returns typed refusals
   (`FunctionNotFound`, `FunctionAmbiguous`, `AlreadyMunged`), so replay onto drifted source is
   *detectable*, where a VFS overlay would paste stale bytes over a moved function and say nothing.
   Those used to go to `_log.warning("cvlr: cannot apply munge to %s")` and
   `_log.info("munge of %s not re-applied")`, where nobody saw them.

---

## 5. Confinement, honestly

[recipes.py:57](../composer/sandbox/recipes.py#L57) states the threat the private `CARGO_HOME`
answers: a confined build runs untrusted `build.rs` and proc-macro code, and *"a malicious build
could overwrite an extracted source under `registry/src` and poison a **later** run that compiles
that crate"*.

One tree means one `.sandbox_cargo` shared by every unit of a run. What that costs, precisely:

* **Within a run, isolation drops from per-unit to per-run.** The untrusted code in question is the
  target program's own build scripts, identical for every unit, so unit A poisoning a cache unit B
  reads is the same code either way. The authored spec code is compiled, not executed at build time,
  and the author cannot add dependencies — `Cargo.toml` is the scaffold's. This is a real reduction
  and a small one.
* **Across runs, the protection is preserved only if the tree is discarded.** The stated threat is a
  *later* run, and a shared cargo home that outlives the run reintroduces exactly it. §4 makes the
  tree disposable by construction, so "delete the tree between runs" is always available; it should
  be the default for any target that is not our own test scenario, with keeping it a deliberate
  warm-cache opt-in.

Neither point is mine to settle unilaterally: [command-sandbox.md](command-sandbox.md) §11 owns the
confinement posture, and item 8 there records the same class of problem being hit and fixed on the
Rust wheel path — a shared crate raced on `Cargo.toml` and `main.rs`, producing a *"package does not
contain this feature"* that silently dropped a unit. That failure is the direct precedent for §6's
insistence that the manifest and `specs/mod.rs` are written once, up front, by a single writer.

---

## 6. What was built

Every item landed. Where the shape differed from what was planned, the row says so.

| # | Change | Where |
|---|---|---|
| 1 | Reconcile always from pristine. `_copy_workspace`'s `if dest.exists(): return` is gone; `SharedTree.reconcile` rebuilds each derived file from the pristine copy and replays, so a munge dropped from state disappears from disk | [tree.py](../composer/spec/cvlr/tree.py), [verify.py](../composer/spec/cvlr/verify.py) |
| 2 | Content-compared, atomic writes; drift returned as `Reconciled.drifted` and put in front of the author rather than logged | [tree.py](../composer/spec/cvlr/tree.py), `_drift_note` in [verify.py](../composer/spec/cvlr/verify.py) |
| 3 | `FunctionMunge.feature`, defaulted to `certora` and always set to the recording unit's own; `apply_munge` lost its feature parameter and `edit_id` gained the feature | [munge.py](../composer/spec/cvlr/munge.py) |
| 4 | `declare_modules` gates each `pub mod` on `HarnessModule.feature`; `declare_unit_features` declares them empty in the package manifest. Both run in `begin`, before the tree is copied | [harness.py](../composer/spec/cvlr/harness.py), [scaffold.py](../composer/spec/cvlr/scaffold.py), [pipeline.py](../composer/spec/cvlr/pipeline.py) |
| 5 | **Not needed, and the reason is worth keeping.** The plan called for splitting `nondet.rs` / `log.rs` per unit. No tool writes them — the author's four tools are `put_harness`, `cargo_check`, `summarize_for_prover` and `munge_function`, and the first writes this unit's module only — so units already put their `impl Nondet for Foo` in their own module. The `cfg` gate from item 4 *fixes* the latent `E0119` that two units doing so would have hit; the per-unit workdir had been hiding it | — |
| 6 | Per-unit summaries: a fourth `_run` layer per unit, composed into a per-unit composite that the unit's conf names. `RunOverlay.summaries` carries it, added to whatever the base conf already names rather than replacing it | [tuning.py](../composer/spec/cvlr/tuning.py), [conf.py](../composer/spec/cvlr/conf.py), [scaffold.py](../composer/spec/cvlr/scaffold.py) |
| 7 | One tree at `.cvlr_work/build`, one `CargoSession`, one `asyncio.Semaphore(1)`, all made once in `begin` and carried on `SharedBuild`. A failed warm is recorded rather than raised, so each unit still reports a `GaveUp` saying why | [pipeline.py](../composer/spec/cvlr/pipeline.py) |
| 8 | `HarnessTarget.features` is the one place that pairs `certora` with the unit's feature, used by both the gate and the submission | [verify.py](../composer/spec/cvlr/verify.py) |

Item 6 reverses a recorded decision, so the reasoning is now in
[conf.py's module docstring](../composer/spec/cvlr/conf.py) rather than only here. `solana_inlining`
stays unowned by the run — nothing in the loop writes an inlining directive, so the package's
`[package.metadata.certora]` declaration still answers for it, and
`certoraParseBuildScript.add_solana_files_to_context` applies each attribute independently.

### Two things the implementation found that the plan had not

**A file whose last munge is dropped has nothing in state naming it.** Rebuilding "every munged
file" from the current munge set restores nothing when that set has just become empty — which is
exactly when restoring matters. The tree therefore records the files it has derived, in a
`.cvlr-derived.json` at its root, so a later session can restore a file the session that munged it
never got to unmunge. It is a hint and never a source of truth: a corrupt or absent note costs one
redundant restore-and-replay that the content comparison then declines to write.

**Replay order had to stop depending on scheduling.** Two munges of one function each insert a line
above its signature, so the order decides the file's bytes — and bytes that depended on which unit
staged first would make the crate's fingerprint depend on it too. Replay is ordered by `edit_id`.

**A third: the run-level declarations are not derived from any unit's state.** `specs/mod.rs`, the
package manifest and the placeholder module files are a function of the *job list*, and they are
written into the project because they are deliverables — before the tree is copied. A reused tree
predates them, so a resumed run whose component set had changed would build against a manifest
missing a unit's feature and a `mod.rs` missing its module. `SharedTree.adopt` re-syncs exactly the
paths `declare_modules` and `declare_unit_features` own, content-compared like everything else.

## 7. What this leaves for the munge document

Two findings from the corpus survey that motivated this plan but belong in a revision of
[munge-and-working-copies.md](munge-and-working-copies.md) §4 and §7:

* **Source munging is in 9 of 11 corpus projects, not one.** Counting `cfg_attr`-with-`certora`
  outside `certora/` trees: restaking 73, spl 50, klend-audit 43, stake-pool 21, kamino 13,
  smart-account 12, manifest 10, vault-tutorial 5, token 3. Only fluid and SolanaExamples are clean.
* **The vocabulary is six kinds, not two.** Beyond `early_panic` (152 sites) and `mock_fn` (~47):
  `certora_make_pub` (6 — pure visibility widening, universally sound by construction),
  `cvlr_hook_on_entry`/`on_exit` (9 — a call to a spec-side function at a function's entry or exit,
  universally sound when observation-only), `inline(never)` (5) and `derive(Copy)` (2).

The hooks matter beyond bookkeeping: they are in the pinned reference set (`cvlr-hook-0.6.1`,
re-exported from `cvlr` as `hook_on_entry`/`hook_on_exit`), this backend had never heard of them when
this was written, and they are a spec-side observation instrument — which is
[munge-and-working-copies.md](munge-and-working-copies.md) §7 trigger 5, and a plausible answer to
trigger 1's already-observed case where a property wanted a state-transition function extracted.
**Since acted on**: the munge editor offers both
([who-edits-the-program.md](who-edits-the-program.md) §9.3), which leaves extraction as the one kind
those skips wanted and the vocabulary still lacks.

Separately: a fully separate spec crate — the Crucible shape — is **not** available, and the reason
is not Rust versus Solidity. Crucible's harness can live outside the crate because it is black-box:
it drives the program through LiteSVM transactions and reads accounts back, so the public
instruction surface suffices. A CVLR rule is white-box — it calls handlers directly, which is what
`no-entrypoint` is for. Three blocks: 0 of 12 corpus projects use a separate spec crate (all are
`<program-crate>/src/certora/`, and multi-crate workspaces put a `certora/` subtree in *each* crate
they verify); `mock_fn` and the hooks expand at the program's own definition site and name
`crate::certora::…`, so a separate crate would have to be a dependency of the program; and
`certora_make_pub` shows specs reaching items that are not even `pub(crate)`.

The transferable idea from Crucible is not "move the spec out of the crate". It is "make the
source-side footprint inert and per-unit selectable, share one crate, and serialize the builds" —
which is this document.

---

## 8. The checks, and which are still open

1. ~~**Multi-variant caching.**~~ **Passes on host cargo.** `--features certora,unit_a`, then
   `unit_b`, then `unit_a` again: the third ran zero rustc invocations. **Still open for
   `cargo certora-sbf`**, which is the same cargo with a different triple and ought to behave
   identically — but the standing rule (plan §7.6.7) is that an error reproduced in a scaffold we
   wrote is evidence about the scaffold until it is checked against a project the scaffold did not
   create, and that scepticism runs in both directions. It belongs in the expensive gate.
2. ~~**Dependency fingerprints do not vary with the unit feature.**~~ **Passes.** The dependency
   compiled once and kept one `-C metadata` hash across both feature sets; only the package's own
   crate got a second. This is the whole cost argument, and it holds.
3. ~~**Cross-unit independence.**~~ **Passes.** A syntactically broken `unit_b` module left
   `--features certora,unit_a` finishing in 0.00s with nothing compiled.
4. ~~**The disposability invariant.**~~ **Covered by unit tests**
   ([test_cvlr_tree.py](../tests/test_cvlr_tree.py)): deleting the tree and reconciling reproduces
   it byte for byte, a munge dropped from state leaves the file, and a *resumed* session restores a
   file whose munge is gone. The end-to-end form — `rm -rf .cvlr_work && resume`, same submission —
   needs a real run and is **still open**.
5. **Latency under contention.** Not measured. §3 predicts one tree wins cold and loses warm, with
   the queue now seconds deep rather than minutes because §2.4 keeps prover runs concurrent. Time a
   three-unit run both ways before believing either half.

## 9. What would change this answer

1. **Cargo stops sharing dependency artifacts across the units' feature sets** — check 2 fails, or a
   future resolver change makes a leaf feature propagate. The entire cost case is that one fact.
2. **A munge kind arrives that cannot be `when`-gated.** `when` is a `mock_fn` parameter;
   `early_panic` has none and is gated by wrapping the `cfg_attr` condition, which the corpus does 60
   times. A kind that is not an attribute at all — a hand-unrolled loop, an extracted function —
   cannot be made dormant, and one tree would then have to hold two incompatible versions of a file.
   That is the same trigger as [munge-and-working-copies.md](munge-and-working-copies.md) §7 item 1,
   reached from the other side.
3. **Unit counts grow past the point where serialization dominates.** §3's trade is cold-start cost
   against warm-gate latency; at some N the queue is the bottleneck and the answer is a permit count
   above 1 with per-unit target dirs for the overflow, which is a hybrid neither document has costed.
4. **Confinement tightens.** §5 accepts a per-run rather than per-unit cargo home. If the posture
   changes to require per-unit isolation of untrusted build code, §2's arrangement survives but the
   cargo-home saving does not, and most of §3's win goes with it.
