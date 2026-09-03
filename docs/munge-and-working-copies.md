# Two working copies, two munge strategies

> How the EVM and CVLR backends each give an agent a modifiable copy of the code under
> verification, how each lets an agent modify it, and the chain of forced decisions that produced two
> answers with almost nothing in common.
>
> Companion to [cvlr-backend-plan.md](cvlr-backend-plan.md) §5.2 and §7.6. That plan predicted these
> would be one mechanism; they are not, and the reason is worth writing down because it is not the
> reason anybody expected.

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

| | EVM | CVLR |
|---|---|---|
| copy made | per materialization — every compile check, every review | once per unit, at formalization |
| copy lives in | a `TemporaryDirectory`, deleted on context exit | `.cvlr_work/<unit>/`, kept for inspection |
| between copies, an edit is | a dict update in graph state | a write to a file |
| reverting an edit | pick an older snapshot from the edit store | not possible |
| cost of an edit | negligible | negligible |
| cost of letting a build see edits | a full filtered tree copy | zero |

EVM's VFS buys **cheap, revertible, reviewable edits** and pays a tree copy whenever a subprocess
needs to see them. CVLR inverted it: the copy is paid once, edits are real writes, and there is no
revert.

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
attributes are in the pinned reference set. The backend exposes the first two as `munge_function`,
and that closed set **is** the give-up boundary: a change needing a third kind is a `record_skip`
naming the kind it would have needed (plan §7.6.4, closing open question 4).

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
3. **Units multiplying.** The per-unit workdir was measured on a three-unit run. At ten units the
   arithmetic is ~10 GB and ten dependency builds, and the deferred shared read-only cache stops
   being an optimization.
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

1. ~~**The judge cannot see what the author changed.**~~ **Closed** — the CVLR judge is contextual,
   and `HarnessAssumptions` (the summaries and the munges, each with the author's justification)
   rides into its input on every review. See plan §7.7.5. Note what remains uncovered: EVM's
   reviewer receives a *diff* and its approval is void the moment anything changes, whereas this
   judge receives a description and its stamp is invalidated by `tuning_history` rather than by the
   judge itself. Same effect, different mechanism, and only the digest enforces it.
2. **Nothing checks that a munge reached the build.** Plan §5.2 predicted this analogue of
   `EditsNotCompiled` and it is not built. The Rust failure is quiet: an attribute inserted into a
   file the `certora` feature gates out changes nothing and reports nothing, and the report still
   carries a source-edit record claiming a change that had no effect.
3. **Munges accumulate and cannot be undone.** No `revert_to_edit`, no history log. Mitigated today
   by a munge being one line with a seconds-long compile gate behind it.
4. **No findings-staleness oracle.** Not yet applicable — CVLR findings are synthesized per run, so
   there is no cross-version store for an oracle to answer about. It becomes a real gap the moment
   findings persist across runs.

Two deferred optimizations, in the order they have to land:

1. **Shared read-only registry.** `shared_cargo_ro_paths` already carries the hook and a comment
   saying a future shared cache "can add specific cache subtrees here without re-opening the
   credentials file". Payoff: 119 MB of download per unit.
2. **Shared dependency build cache.** Larger payoff and necessarily second, and probably `sccache`
   (which keys on inputs) rather than `CARGO_TARGET_DIR`. The private cargo home puts each unit's
   dependency *sources* at a different absolute path, which is the kind of input cargo fingerprints
   and rustc debug paths take in, so a shared target dir would likely rebuild until the registry is
   shared. That mechanism is reasoning, not measurement — confirm it before budgeting for it.
