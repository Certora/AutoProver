# Two working copies, two munge strategies

> How the EVM and CVLR backends each give an agent a modifiable copy of the code under
> verification, how each lets an agent modify it, and the chain of forced decisions that produced two
> answers with almost nothing in common.
>
> Companion to [cvlr-backend-plan.md](cvlr-backend-plan.md) §5.2 and §7.6. That plan predicted these
> would be one mechanism; they are not, and the reason is worth writing down because it is not the
> reason anybody expected.

> **§5's conclusion is wrong for CVLR, for a reason it names and then discards** — see
> [who-edits-the-program.md](who-edits-the-program.md) §2.1. The sub-agent does not have to argue that
> a property-relative munge was acceptable; CVLR's judge holds the properties and already rules on
> both kinds, where EVM's munge reviewer is deliberately built not to.
>
> **The CVLR half of §1–§3 no longer describes the code.** The per-unit workdir has been replaced
> by one tree for the run, with each unit's module behind its own cargo feature —
> [single-working-tree.md](single-working-tree.md) is the argument and what was built. §7's trigger 3
> is what fired, one step earlier than expected: the per-unit arithmetic did not survive being
> checked against what cargo actually caches. §5's argument about *who* applies a munge has since
> been settled the other way and built — program source has one owner, a munge editor agent with a
> reviewer of its own ([who-edits-the-program.md](who-edits-the-program.md) §9) — and §4's charter
> stands except for its count of the vocabulary; see the note there.
>
> **And the title's premise is retired: the two working copies have converged.** CVLR's tree is now a
> graphcore VFS — the same instrument EVM uses, base plus overlay, materialized incrementally into a
> persistent directory ([the-tree-is-a-vfs.md](the-tree-is-a-vfs.md)). So §1–§3's finding that these
> are "two answers with almost nothing in common" describes a state that no longer exists, and §1.3's
> table has flipped on the CVLR side row by row. What remains genuinely different is *agent
> topology*, which is §6 — and that was already the answer §6 reached by a different route.

**This document is a snapshot of reasoning, not a conclusion.** Every measurement in it was taken
while the CVLR backend still had a blocking prover defect ([upstream-defects.md](upstream-defects.md)
P6) and had never
completed a run that verified a real program end to end. The comparison is therefore drawn against a
backend whose costs are known and whose *benefits* are not yet demonstrated. §7 lists what would
change the answer, and [cvlr-backend-plan.md](cvlr-backend-plan.md) §7.10 is the scheduled re-read
that checks them once the backend is fully functional.

---

## 1. What each backend gives an agent to edit

Both backends face the same problem: an agent needs to modify the code under verification without
touching the developer's tree, and a build tool has to see the result. They solve it in opposite
orders.

### 1.1 EVM — an in-process overlay, materialized on demand

The EVM editing kit is built on graphcore's VFS (`graphcore/tools/vfs.py`). Despite the name,
**nothing is virtualized at the operating-system level**: there is no FUSE mount, no overlayfs, no
mount namespace, no `LD_PRELOAD`. The entire mechanism is one field of LangGraph state:

```python
class VFSState(TypedDict):
    vfs: Annotated[dict[str, str], merge_vfs]
```

A `dict[str, str]` — project-relative path to file *text* — with a last-writer-wins reducer. What
makes it behave like a filesystem is that a small set of Python functions consult it in a fixed
order. `VFSAccessor.get` checks the overlay first and falls through to `fs_layer`, a real directory
on disk:

```python
if file in state["vfs"]:
    return state["vfs"][file].encode("utf-8")
...
path = pathlib.Path(fs_layer) / file
```

So it is a **union overlay over the real project directory, resolved in-process**. Five tools see
it — `get_file`, `list_files`, `grep_files`, `edit_file`, `put_file` — and nothing else does.
`edit_file` reads through the accessor, performs a string replacement, and returns the whole new file
content as a state update. Nothing on disk changes.

Three properties follow from the representation and are easy to trip over:

* **Text only.** Overlay values are `str` and materialize through `write_text`. A binary file can
  only ever come from the base layer.
* **Append-only.** `merge_vfs` sets keys and never removes them, and there is no delete tool. This is
  load-bearing downstream: `vfs_diff` relies on it to conclude that "a path missing from `old` is an
  addition, never a deletion."
* **Filtered.** A global-include predicate applies to reads *and* to materialization, with a `.git`
  floor always on, and the EVM path additionally excludes `.certora_internal` and `emv-*` trees.

**A subprocess cannot see any of it.** That is what `materialize` exists for:

```python
with accessor.materialize(state) as tmp:
    folder = Path(tmp)
    (folder / _CONF_NAME).write_text(json.dumps(config))
    result, stdout = await run_prover_inner(folder, [_CONF_NAME, "--build_only"], ...)
```

`_materialize` opens a `TemporaryDirectory`, writes every overlay entry into it as a real file, then
walks `fs_layer` and copies in every base file not already present. solc runs against that physical
tree. So the compile check checks a *snapshot*, and producing the snapshot is a full copy of the
(filtered) project — on every invocation. `debugging_tmp_directory` exists precisely because that
tree is otherwise deleted before anyone can look at it.

### 1.2 CVLR — a physical copy per unit, edited in place

> **Superseded twice.** One copy per *run* now, at `<project>/.cvlr_work/build/`, and edits are no
> longer "in place": every file the run derives is rewritten from checkpointed state on every stage,
> which is what makes the tree disposable ([single-working-tree.md](single-working-tree.md) §4). And
> that tree is no longer a bespoke copy at all — it is a graphcore VFS base plus one overlay, with
> the author's read tools and the build's materializer coming out of one `fs_tools_layered` call so
> they cannot disagree about what the composite view holds
> ([the-tree-is-a-vfs.md](the-tree-is-a-vfs.md) §4–§5).

`CvlrFormalizer.formalize` gives each unit `<project>/.cvlr_work/<module>/`, produced by
`shutil.copytree` with `target`, `.git`, `.certora_internal`, `certora_out` and `.cvlr_work` itself
excluded, and `symlinks=True`. **It is not a git worktree** — there is no object-store sharing and no
copy-on-write; the bytes are duplicated. An existing workdir is reused rather than re-copied, so a
resumed run finds whatever the last session staged.

That copy is then the unit's whole world: its harness module, its tuning files, its conf, its build
output, and — because of confinement, see §3 — its own `CARGO_HOME`. Every edit is a real write to a
real file, and `cargo` sees it because it is simply there.

### 1.3 The same copy, at different times

Stated side by side, the two are less different than they look. Both end with a physical tree that a
subprocess builds. They differ in *when* it is made and what is cheap in between.

| | EVM | CVLR (as described here) | CVLR (now) |
|---|---|---|---|
| copy made | per materialization — every compile check, every review | once per unit, at formalization | once per run, incrementally reconciled |
| copy lives in | a `TemporaryDirectory`, deleted on context exit | `.cvlr_work/<unit>/`, kept for inspection | `.cvlr_work/build/`, kept for inspection |
| between copies, an edit is | a dict update in graph state | a write to a file | **a dict update in graph state** |
| reverting an edit | pick an older snapshot from the edit store | not possible | **drop it from state; the tree is rederived** |
| cost of an edit | negligible | negligible | negligible |
| cost of letting a build see edits | a full filtered tree copy | zero | **a content-compared incremental dump** |

EVM's VFS buys **cheap, revertible, reviewable edits** and pays a tree copy whenever a subprocess
needs to see them. CVLR inverted it: the copy was paid once, edits were real writes, and there was no
revert.

**That inversion is what has since been undone**, and the fourth column is why the comparison this
document is built on no longer holds. The premise that made the inversion look necessary — that
materializing means a fresh directory per compile — is a property of `TempDirectoryProvider` rather
than of `Materializer`, so a persistent materializer buys the revertibility back without paying the
copy ([the-tree-is-a-vfs.md](the-tree-is-a-vfs.md) §2).

---

## 2. What a per-unit workdir actually costs

Measured on the repository's own Solana test scenario (213 crates resolved; the last gate run had
three units). Sizes are per unit.

| | | |
|---|---|---|
| **Duplicated** — source copy | 104 KB | `copytree` |
| **Duplicated** — `.sandbox_cargo/registry` | **119 MB** | index 16 MB, `.crate` tarballs 13 MB, unpacked sources 90 MB — 212 crates |
| **Duplicated** — `target/` | **955 MB** | debug 626 MB, release 177 MB, `sbf-solana-solana` 153 MB |
| | **≈1.07 GB per unit** | |
| **Shared, read-only** — `RUSTUP_HOME` | 15 GB | granted whole |
| **Shared, read-only** — `~/.cargo/bin` | 62 MB | shims only; never the cargo-home root, which holds `credentials.toml` |
| **Shared, read-only** — platform-tools | 1.2–2.5 GB per generation | |

**The thing the copy exists for is 0.01 % of what a unit costs.** Isolation is nearly free; what is
expensive is everything isolation drags along with it.

And the duplication is worse than disk. The 119 MB registry is a **re-download** per unit — `cargo
fetch` runs unconfined and online, once per session, into that private home. The 955 MB `target/` is
not copied at all; it is **recompiled**. On a real project the source copy stops being free too: the
Solana verification projects available locally have source trees of 7 MB, 11 MB, 77 MB and 191 MB.

**Almost none of the compilation is unit-specific.** `target/debug/deps` holds 515 artifacts, of
which **4** belong to the program's own crate. The rest are dependencies that cannot differ between
units — same lockfile, same `certora` feature, same toolchain — rebuilt per unit for byte-identical
results.

---

## 3. The reasoning chain

> **The whole chain is superseded.** (1)–(4) fell to
> [single-working-tree.md](single-working-tree.md): the compile gate is whole-crate only while every
> unit's module is compiled, and each is now behind its own cargo feature, so a `cfg`'d-out module is
> neither compiled nor in rustc's dep-info; (2), (3) and (4) were forced by (1) and fall with it.
>
> **(5) and (6) were then said to survive. They do not** —
> [the-tree-is-a-vfs.md](the-tree-is-a-vfs.md). (5) is an assumption about how a VFS materializes
> (a temp dir per check) rather than a property of cargo, and the incremental persistent
> materialization it treats as impossible is what `SharedTree` already does. (6) is false twice
> over: `revert_munge` exists, and the overlay's remaining purchase — the author reading what the
> build compiles — is exactly what its absence cost. A run against a real program died on a
> 2,272,575-token prompt, 28,739 lines of which were the run's own working tree.

Each step below was forced by the one above it. None of them is a preference, and only the last one
is about munging at all.

**(1) The CVLR compile gate is whole-crate.** A harness is a Rust module *inside* the crate under
verification, so `cargo check` compiles every unit's harness module, not just the one being worked
on.

**(2) Therefore the workdir is per unit** (plan §7.5.2, open question 3 — recorded as *forced*, not
preferred). In a shared workdir, unit A's gate fails whenever unit B's draft is momentarily broken —
nondeterministically, for a reason that has nothing to do with A. That is worse than a slow gate,
because it is not reproducible.

**(3) Therefore each unit has a private `CARGO_HOME`.** This one is confinement's doing rather than
the workdir's: the sandbox policy points each session at `<workdir>/.sandbox_cargo` so that an
untrusted build's `build.rs` and proc-macros can only poison a throwaway cache, never a shared one.
`shared_cargo_ro_paths` grants only `~/.cargo/bin`, deliberately never the cargo-home root, because
Landlock's `PathBeneath` is hierarchical and the root holds registry credentials.

**(4) Therefore each unit has its own `target/`, and its dependencies are fetched and compiled
separately.** Both follow mechanically from (2) and (3).

**(5) Therefore a VFS would have been expensive here in a way it is not for solc.** solc's input is a
source tree, so materializing one is a file copy. `cargo`'s input is a source tree *plus* a
`CARGO_HOME` *plus* a `target/`. A materialized cargo snapshot is either a cold 119 MB fetch and a
full rebuild per compile check, or it is not isolated at all — and the isolation a VFS would provide
is isolation the per-unit workdir already provides, for free, because the workdir is already private.

**(6) Therefore edits are real writes, and there is no revert.** Once the working copy is a private
directory that a build already sees, the overlay has nothing left to buy.

**(7) And separately: the munge vocabulary is small enough that no sub-agent is warranted.** This is
the one step not forced by the filesystem. It comes from the corpus (§4), and it is the step most
worth re-testing.

---

## 4. Two charters

### 4.1 EVM: three prose categories, behaviour-preserving

`munge_charter.j2` authorizes **Exposure** (add a thin external harness so the spec can reach an
internal computation), **Refactor** (hoist a computation into a standalone function, or insert a
side-effect-free hook), and **Standardize** (rewrite a prover-hostile construct into ordinary
Solidity). Then a page of "Lines You Do Not Cross": never add or remove a `require`/`assert`/
`revert`, never delete or stub a function, never fix a bug, never improve, never change the external
interface.

The charter's soundness criterion is **behavioural equivalence** — *"Representation may change;
logical behavior may not."*

### 4.2 CVLR: two library attributes, property-relative

> **The count is wrong, and by a lot.** "The one project in the corpus that carries a source-level
> munge" is nine of eleven: counting `cfg_attr`-with-`certora` outside `certora/` trees gives
> restaking 73, spl 50, klend-audit 43, stake-pool 21, kamino 13, smart-account 12, manifest 10,
> vault-tutorial 5, token 3; only fluid and SolanaExamples are clean. And the vocabulary across those
> is six *kinds*: beyond `early_panic` (152 sites) and `mock_fn` (~47) there are `certora_make_pub`
> (6 — pure visibility widening, universally sound by construction, but a **project-local proc
> macro** with no counterpart in `cvlr`, and Rust cannot make visibility conditional on a feature
> without duplicating the item), `cvlr_hook_on_entry` / `on_exit` (9 — a call to a spec-side function
> at a function's entry or exit, universally sound when observation-only), `inline(never)` (5) and
> `derive(Copy)` (2). Three of the six are now offered
> ([who-edits-the-program.md](who-edits-the-program.md) §9.3).
> The hooks matter most: they are in the pinned reference set (`cvlr-hook-0.6.1`, re-exported from
> `cvlr` as `hook_on_entry` / `hook_on_exit`), this backend had never heard of them when this note
> was written and the editor now offers both, and they are the spec-side observation instrument §7
> trigger 5 asks about. Rewriting this section against that evidence has not been done.
>
> One thing the corpus also settles: `cvlr::mock_fn` takes a `when` parameter naming a cargo feature,
> and the conf's `cargo_features` selects which mocks are live per run — kamino metavault declares
> eleven such features under the comment *"Per-conf prover mocks; enabled via `cargo_features`. Inert
> without `certora`."*. That is what makes a source munge scopable at all, and
> [single-working-tree.md](single-working-tree.md) §2.3 is what this backend does with it.

Read off the one project in the corpus that carries a source-level munge — 22 files, 1097 lines —
whose every source hunk is one of six kinds:

| kind | uses | what it does |
|---|---|---|
| `cvlr::early_panic` | 15 | rewrites every `?` in the function to `.unwrap()` (three paired with `inline(never)`) |
| `cvlr::mock_fn(with = …)` | 6 | replaces the function with a named stand-in |
| mock traits into scope | 6 | a feature-gated `use`; bookkeeping for the row above |
| logging redirected | 3 | `msg!` swapped for a verification log module |
| bounded collections shrunk | 2 | literal array sizes replaced by constants that shrink under the feature |
| a hand-unrolled loop | 1 | the only hunk that is none of the above, and the only one its author commented |

Five of six are a CVLR attribute or the `cfg`/`use` bookkeeping those attributes need. Both
attributes are in the pinned reference set. The backend first exposed these two as `munge_function`;
the vocabulary is now five, and the closed set **is** the give-up boundary either way: a change
needing a kind that is not in it is a `record_skip` naming the kind it would have needed (plan
§7.6.4, closing open question 4).

---

## 5. Why the topologies cannot be swapped

The tempting move is to give CVLR the EVM arrangement: a dedicated editor sub-agent commissioned by
the author, reviewed by a second agent, gated on a digest. It does not transfer, and the reason is
sharper than "Solana is different".

**Neither EVM munge agent ever sees the specification.** The editor's prompt says outright that it is
*"not expected to understand why a construct troubles the Prover"* and must not reason about prover
internals. The reviewer's entire seven-item checklist is behavioural — lost writes, added
constraints, arithmetic checkedness, interface drift, conversion coherence, scope, report fidelity —
and its verdict is scoped to match: *"Approval is not an endorsement that the edit was wise — only
that the editor did its job within the rules."*

That works because behavioural faithfulness is checkable **without knowing what is being proved**.

Both CVLR kinds are on the other side of that line. `early_panic` preserves *runtime* behaviour — a
Solana panic reverts exactly as a returned error does — and changes **what the prover explores**,
because panicking paths are pruned. `mock_fn` replaces a computation with a simpler one outright.
Neither fits any of EVM's three categories, and what they do is precisely what the EVM charter routes
*away* from the source: *"When 'pretend this function does not exist' is genuinely right, CVL has a
deleting summary for it — declared in the spec."*

**EVM keeps property-relative approximation on the spec side, where the author owns it, and delegates
only behaviour-preserving rewriting to an agent.** CVLR has no comparable spec side — the harness is
Rust inside the crate, and the only spec-side instrument, `summarize_for_prover`, havocs rather than
computes. So property-relative approximation has to happen in the source, which puts it on the
author's side of EVM's own dividing line.

Three consequences:

* **The soundness argument would be written by the wrong party.** `why_sound` for a munge is "sound
  for the properties in this batch", and only the author holds the batch. A sub-agent could report
  faithfully what it changed and could not argue the change was acceptable.
* **The request would have to be a prescription.** The editor prompt's central discipline is
  *"Requests describe problems, not edits"*. With a two-element vocabulary, describing the problem
  precisely enough for an editor to choose *is* choosing.
* **The cost is inverted.** An editor conversation plus a reviewer conversation, per one-line
  attribute insert, inside a loop whose latency §9 already lists as the top risk.

---

## 6. What was shared, and what the boundary turned out to be

Three things were reused from the EVM munge machinery:

| | |
|---|---|
| `report/schema.py` | `SourceEditRecord` and `AppliedEditRecord`. Its docstring is already the disclosure a munge owes: *"its presence means the component's outcomes are claims about the modified code, not the code as shipped."* |
| `pipeline/core.py` | `Formalizer.source_edits()` — the hook the EVM backend fills and this one returned `[]` for. |
| `munge/vfs_diff.py` | `compute_diff` / `fs_resolver`. Written twice: a hand-rolled `difflib` version went in first and produced *byte-identical* output, which is the argument for deleting it. |

Not reused, with the reason:

| | lines | why not |
|---|---|---|
| `munge/munge_agent.py` | 501 | no sub-agent — §5 |
| `munge/compile_check.py` | 153 | solc-specific throughout: scrapes `srclist` from `.certora_build.json` and suffix-matches the instrumented tree. The *idea* transfers (§8) |
| `munge/edit_store.py` | 93 | content-hash VFS snapshots; there is no VFS to snapshot |
| `munge/edit_oracle.py` | 65 | needs a versioned findings store; CVLR findings are per-run |
| `munge/erc7201.py` | 72 | EVM storage-slot arithmetic |
| the three munge templates | 275 | all presuppose the agent topology |

**The boundary was agent topology, not Solidity versus Rust.** Read the second table again:
`munge_agent` exists because a sub-agent does the editing; `edit_store` exists because that sub-agent
needs stageable, revertible edits; the VFS shape, both system prompts, the digest gate and the
charter-as-shared-template all follow from the same single decision. One question — *does a separate
agent do the editing?* — accounts for every non-reusable line.

This is a partial answer to the question plan §7.9 asks Soroban to settle. Soroban is CVLR too, so it
should inherit the *Solana* shape essentially whole: expect a re-read of the vocabulary against a
Soroban project, one more `ForkOverride` if a Soroban fork exists, and close to no new code — and
expect the chain-neutral core to need nothing, because the seam that mattered here was never
chain-shaped.

---

## 7. What would change this answer

Written as falsifiable triggers rather than caveats, because the point of the note in the plan is to
check these rather than to re-read the argument.

1. **A seventh munge kind.** The vocabulary is closed on the evidence of one project's diff. If a
   real target needs a kind that is not an attribute — a hand-unrolled loop, an exposure-style
   harness around an internal function, a restructured state machine — then for *that kind* soundness
   becomes property-independent again, EVM's charter already has the prose for it, and a sub-agent
   starts making sense. The give-up boundary already surfaces this: it is exactly the case that
   produces a `record_skip` naming a kind nobody has written down.

   **Already observed once.** Two of the eight skips in the last passing gate run were
   deposit-balance properties blocked by P6, both saying the handler had no state-transition
   function to descend to. What they wanted was that function *extracted* — Refactor/Exposure, not
   an attribute. The test scenario was editable and gained one (plan §7.6.6); a real target would
   not have been, and the question of whether that justifies a seventh kind is exactly what this
   trigger is for.
2. **Evidence that the closed vocabulary is costing rules.** If skips naming a would-be munge become
   a common outcome, the corpus's five-of-six-are-attributes finding was about one project's style
   rather than about CVLR.
3. ~~**Units multiplying.**~~ **Fired, and earlier than the trigger anticipated.** It did not take
   ten units: the per-unit arithmetic was wrong about what cargo caches, and one tree with a cargo
   feature per unit removes the duplication at three units as well as at ten
   ([single-working-tree.md](single-working-tree.md) §3).
4. **Confinement changing.** `CargoSession.cargo_home` returns `None` when the sandbox is disabled,
   so unconfined dev runs share `~/.cargo` and pay none of the 119 MB. If production confinement
   relaxes, step (3) of the chain in §3 weakens and with it the case against a VFS.
5. **A spec-side approximation instrument appearing.** §5's argument turns on CVLR having no
   equivalent of a CVL summary that *computes*. If `summarize_for_prover` gains one — or CVLR gains a
   spec-side mock — property-relative approximation could move off the source, and the EVM split
   becomes available.
6. **The backend verifying a real program end to end.** Everything here compares a working
   arrangement against a hypothetical one using cost alone, because the benefit side is not yet
   measurable. Redo the comparison when it is.

---

## 8. Known gaps in the CVLR side

Carried here so §7's revisit has them in one place. Ranked by what a wrong answer costs.

1. ~~**The judge cannot see what the author changed.**~~ **Closed, twice over.** The CVLR judge is
   contextual, and `HarnessAssumptions` (the summaries and the munges, each with its justification)
   rides into its input on every review — plan §7.7.5. What this entry then recorded as still
   uncovered was that EVM's reviewer receives a *diff* while this judge received a description; that
   is closed too. `HarnessAssumptions` now carries a diff computed from the munge records, and the
   briefing tells the judge to read it *rather than* the summary, because the summary is the
   editor's account of its own work ([who-edits-the-program.md](who-edits-the-program.md) §9.1).
2. ~~**Nothing checks that a munge reached the build.**~~ **Closed.** Plan §5.2 predicted this
   analogue of `EditsNotCompiled`, and the Rust failure was the quietest one this backend had: an
   attribute inserted into a file no enabled feature reaches changes nothing, reports nothing, and
   leaves the report claiming a source edit that did not happen. Two halves, closed separately. A
   munge whose *function* cannot be found on replay is a typed refusal put in front of the author
   ([single-working-tree.md](single-working-tree.md) §4.3). And the editor's submit gate now reads
   cargo's dep-info — the `.d` file beside the artifact for *this* feature variant, identified by a
   file only this build compiles — and answers whether the munged paths are in it; a build whose
   dep-info cannot be identified reports `NotChecked` rather than passing
   ([who-edits-the-program.md](who-edits-the-program.md) §9.2).
3. ~~**Munges accumulate and cannot be undone.**~~ **Closed.** The *representation* already
   supported removal: each munged file is rebuilt from the pristine copy and replayed from state on
   every build, so a `FunctionMunge` that is not in state is not on disk, including across a resume
   — which is what makes a rewound checkpoint mean something and is a property a text overlay could
   not have had. What was missing was the tool, and `revert_munge` is now on the author's belt: with
   program-source edits owned by the editor agent, the author's say over an edit it regrets *is* the
   undo ([who-edits-the-program.md](who-edits-the-program.md) §9.1, §9.4). The history log EVM's
   edit store keeps is still absent.
   history log EVM's edit store keeps is still absent.
4. **No findings-staleness oracle.** Not yet applicable — CVLR findings are synthesized per run, so
   there is no cross-version store for an oracle to answer about. It becomes a real gap the moment
   findings persist across runs.

Two deferred optimizations, in the order they have to land — **both now moot**, because the shared
tree fetches and compiles the dependency graph once for the whole run and is therefore both of them
at once ([single-working-tree.md](single-working-tree.md) §3):

1. **Shared read-only registry.** `shared_cargo_ro_paths` already carries the hook and a comment
   saying a future shared cache "can add specific cache subtrees here without re-opening the
   credentials file". Payoff: 119 MB of download per unit.
2. **Shared dependency build cache.** Larger payoff and necessarily second, and probably `sccache`
   (which keys on inputs) rather than `CARGO_TARGET_DIR`. The private cargo home puts each unit's
   dependency *sources* at a different absolute path, which is the kind of input cargo fingerprints
   and rustc debug paths take in, so a shared target dir would likely rebuild until the registry is
   shared. That mechanism is reasoning, not measurement — confirm it before budgeting for it.
