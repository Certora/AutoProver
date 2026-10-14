# Munges as text edits

> A plan to replace the CVLR backend's record-based munges with the EVM backend's free-form text
> edits: a per-unit overlay of whole files, written by the munge editor, with the editor writing the
> cargo-feature gating itself and the report handing the user one patch per unit.
>
> **Implemented and measured.** §7 steps 1-6 are done (the rebuild cost is §10 item 1); step 7
> (re-recording the vault tape, running the CVLR e2e test) is not, and step 8 is a decision about
> the PR's shape. Reverses the decision in
> [who-edits-the-program.md](who-edits-the-program.md) §8.2 ("Option 2 — a typed write surface over
> the munge records") and supersedes the munge halves of
> [single-working-tree.md](single-working-tree.md) §2.3 and §4 and of
> [cvlr-landing-plan.md](cvlr-landing-plan.md) P3a2, P3a3 and P5. §8 lists what each superseded
> section keeps.

---

## 1. The decision

A munge stays **feature-gated on the unit that asked for it**, because the user is meant to be able
to apply it to their own tree safely: with the unit's feature off, their build is the build they
shipped. What changes is who writes the gate and how the edit is stored.

* **The editor writes the gate.** It is told the requesting unit's feature and the gated shape each
  kind of edit takes (§4.4), and the reviewer checks it. AutoProver no longer renders `cfg` lines.
* **An edit is text.** The editor edits whole files in a per-unit VFS overlay, as EVM's
  [munge_agent.py](../composer/spec/source/munge/munge_agent.py) does. There are no munge records,
  no replay onto the pristine project, and no union of units' munges in one file.
* **The report hands the user one patch per unit**, as EVM's `source_edits` does
  (`SourceEditRecord.cumulative_diff`). Merging two units' patches to one file is the user's job.

What this buys is the removal of every piece of Rust reading the munges need — finding a function's
signature and its body's end, parsing `use` trees, recognizing a sibling's gate — along with the
record vocabulary, its appliers, its typed refusals, and the replay ordering. About 1,300 lines of
`munge.py` and most of 2,500 lines of munge tests go.

**No mechanical check that the gate is right.** EVM has none either, and lives with it. A check is
sketched as future work in §9.

---

## 2. Why §8.2's objection no longer holds

[who-edits-the-program.md](who-edits-the-program.md) §8.2 rejected a VFS for Solana because "a
free-form text edit has no `cfg_attr`": two units editing one file in the shared tree collide, and
recovering isolation "means one overlay per unit and one materialization per unit — which is the
arrangement [single-working-tree.md](single-working-tree.md) exists to have removed".

Two things have changed underneath that.

1. **The tree no longer needs to be correct for every unit at once.** The argument that it must was
   [single-working-tree.md](single-working-tree.md) §2.4's first half: a sibling may rewrite the
   tree during a unit's in-flight build, so every write has to be inert for every unit. The same
   section's second half then extended the build permit from staging through the Prover's upload,
   because a sibling's build replaced the `.so` an in-flight job was about to upload.
   `HarnessTarget.stage` is the tree's only writer and runs under that permit. So every check, build
   and upload already has the tree to itself, and the tree can hold *the building unit's* copy of
   each munged file.
2. **The single tree removed per-unit build directories, not per-unit file contents.** Its saving is
   that dependencies — all but 4 of ~515 artifacts on the three-unit scenario
   ([single-working-tree.md](single-working-tree.md) §3) — compile once per run. Swapping a few
   source files of the program's own crate at stage time keeps that saving.

So: **one tree, one `target/`, one permit; at each stage the tree holds every unit's harness draft
(each behind its own feature, as now) and only the staging unit's munged files.** A path the overlay
stops serving is restored from the project by the materializer, which is how a sibling's munged file
reverts.

---

## 3. What it costs

* **A program-crate rebuild when consecutive builds come from different units and either has munged
  a file.** With the union, a munged file's bytes are the same for every unit, so each unit's
  compiled variant stays cached in `target/`. With swapping, the bytes change, so cargo recompiles
  the program crate (and any local crate whose file was munged). Measured on klend: about 20s per
  SBF build, about 1s per `cargo check` (§10 item 1).
* **Drift detection on resume.** A record replay refuses when the project's source has moved since
  the munge was recorded. A stored whole-file overlay silently overwrites the developer's newer
  copy. EVM has the same gap. Within one run the project copy does not move.
* **Enforcement of the closed vocabulary.** The kinds become guidance in the editor's charter and a
  checklist for the reviewer, not typed tools. The boundary is as strong as the reviewer.
* **A ready-merged deliverable.** Two units' patches to one file will often conflict textually even
  where their gated changes are compatible. The user merges them.

---

## 4. Design

### 4.1 State

`CvlrGenerationExtra.munges: list[Munge]` is replaced by EVM's pair: a per-unit `vfs` overlay
(project-relative path → whole file) and `version_history`, with the reducer in
`composer/spec/source/live_explorer.py`. `GeneratedHarness.munges` becomes `applied_edits` (EVM's
`AppliedEdit` shape) plus the `vfs`.

`tuning_history()` keys on `version_history` instead of `munge_history`, so applying or reverting an
edit still invalidates the prover and judge stamps.

### 4.2 The tree

`UnitEdits` carries the unit's `vfs` instead of its munges. `SharedTree.reconcile` writes every
unit's draft (as now) plus **the staging unit's overlay only**. The materializer's content
comparison stays, so restaging the same unit writes nothing.

Every overlay path is checked against `ProjectSource` when the editor submits and again at stage, as
munge paths are today: an edit may touch only the project's own crates, never a fetched dependency
unpacked into the tree's `CARGO_HOME`. A new file (a module redirect's stand-in) must also land
inside a project crate.

Removed: `replay` and its ordering, `Drifted`, `Reconciled.drifted`, `munge_diff`, `pristine_of`,
`run_global_munges`, `UnitEdits.munges`, and the `created` handling in `reconcile`.

### 4.3 Reads

The agents' source tools today read the tree layered over the project (`entry.py`,
`build_layered_source_tools`), so that an agent sees the bytes the compiler is given
([the-tree-is-a-vfs.md](the-tree-is-a-vfs.md)). Once the tree holds whichever unit staged last, that
is no longer one unit's view. **Each agent must read its own unit's overlay over the project
instead** — the author, its judge, and the editor and reviewer it commissions. That is what EVM's
VFS read tools already do, and it keeps [the-tree-is-a-vfs.md](the-tree-is-a-vfs.md)'s property per
unit. The harness drafts still come from the tree or from state; only munged program files change.

### 4.4 The editor

EVM's topology, which the CVLR editor already follows: request in, edit, earn a review, submit
behind a digest, or give up. And now EVM's machinery for it:

| | EVM today | CVLR after |
|---|---|---|
| write tools | graphcore `edit_file` / `put_file` over the VFS | the same |
| review | `RequestReviewTool`, `reviewed_digest` = hash of the overlay | the same |
| diff | `summarize_changes` | the same |
| submit gate | `check_edits_compile` (`certoraRun --build_only`, `srclist`) | stage the overlay under `build_slot`, `cargo check --features certora,<unit>`, and refuse a munged path missing from rustc's dep-info (`depinfo.compiled_sources`), which is the existing `NotCompiled` |
| completion | `MungeDescription` (adds `added_files`) | `CommonMungeDescription` |
| storage | `EditStore.commit` → an application key | the same |

**The charter keeps the kinds as guidance, with the shape each must take.** The current docstrings
already describe the gated shapes; they move into the prompt:

* an attribute: `#[cfg_attr(feature = "<unit>", cvlr::early_panic)]` and the like, above the item;
* an extraction: the original verbatim under `#[cfg(not(feature = "<unit>"))]`, the delegating
  replacement and the extracted `pub fn` under `#[cfg(feature = "<unit>")]`;
* a derive swap: the moved derives under `cfg_attr(not(feature = "<unit>"), derive(..))`, additions
  under `cfg_attr(feature = "<unit>", derive(..))`, the rest left on the plain `derive`;
* an import swap: the original `use` under `cfg(not(..))`, the stand-in binding under `cfg(..)`;
* a module redirect: `#[cfg_attr(feature = "<unit>", path = "..")]` on the `mod` declaration, and
  the stand-in as a new file.

The rule they share, stated once: **with the unit's feature off, the file must compile to exactly
what the developer wrote.** The editor never edits code outside a gate, and never retypes code it
keeps; it wraps it.

**A munge inside a local dependency crate.** That crate's manifest does not declare the unit's
feature, so the patch must also declare it there and forward it from the program's manifest. This is
what `forwards_feature` checked and why a dependency redirect was gated on the shared `certora`
feature. Under swapping no edit is ever in force for another unit's build, so the shared gate and
`run_global_munges` go; the manifest edits are part of the unit's patch, and the charter and
reviewer must cover them.

**The reviewer's checklist gains the gating.** Every hunk is gated on the requesting unit's feature
and only that one; code the edit keeps is kept verbatim inside `cfg(not(..))`; nothing outside a
gate changed; a dependency edit declares and forwards the feature.

### 4.5 The author's surface

EVM's surface, unchanged. `code_editor(request)` stays, but an approved edit is no longer applied to
the author's build on return: the editor's result is committed to `EditStore` and the author gets
the summary, the soundness argument, the diff and an application key, and applies the edit by
calling `commit_edit(key)`. An edit the author never commits is never in force. `edit_history_log`
and `revert_to_edit` replace `revert_munge`. `amend_munge` goes: re-wording a justification is a new
edit with the same overlay.

This costs the author one tool call per edit, and buys one author surface for both backends: the
same tool names, the same prompt text about editing, and the same tools (§4.8).

### 4.6 The judge

`HarnessAssumptions` lists the unit's applied edits as EVM's `AppliedEdit` accounts (executive
summary, `why_sound`) plus the cumulative diff, instead of kind-tagged records with `subject` and
`describe()`. The judge prompt's per-kind weighing stays, keyed on what the diff shows rather than
on a record's kind. The "IN FORCE FOR EVERY UNIT" note goes with the shared gate.

### 4.7 The report

`CvlrFormalizer.source_edits` becomes EVM's: one `SourceEditRecord` per delivered unit, its
`applied_edits` from the editor's accounts and its `cumulative_diff` from
`diff_against_baseline(unit vfs, project)`. Each is a patch the user can apply on its own.

### 4.8 Shared code

[who-edits-the-program.md](who-edits-the-program.md) §8.6 recommended building Solana's editor
against a local protocol first and lifting a shared core "once there are two implementations to
generalise from". With one edit representation that condition is met. Lift from `munge_agent.py`:
`RequestReviewTool` (with a backend `briefing` placed before the diff), `GiveUpTool`, the reviewer
(`munge_feedback_judge`, its prompts a parameter), the digest gate (`unapproved`) and the acceptance
(`accept_edits`); and from `author.py`, the author's side: `ApplyEditTool` (`commit_edit`),
`EditHistoryLog` and `RevertToEdit`, now in `munge/edit_tools.py` and typed on `VersionedHistory`,
the only state they read, with `AppliedEdit` and `applied_edits` beside `EditStore`. `EditStore` and
`vfs_diff` are already shared. Each backend keeps its own submit tool: the
gates' inputs differ too much (a conf and added files on EVM, the harness draft and summaries on
CVLR) for one parameterized class to be smaller than two that call the shared helpers. The conf-map
extension, `added_files`, ERC-7201 tools and Solidity prompts stay behind the EVM side.
`EditToolsHost` is already the seam.

---

## 5. What goes, and what stays

* **`composer/spec/cvlr/munge.py` (1,339 lines): all of it.** `forks.py` is already separate.
* **`composer/spec/cvlr/rust_source.py` stays.** `anchor_surface.py` uses `DERIVE_LIST`, `body_span`
  and `code_positions` to read Anchor's generated surface. What goes is only what munges added on
  top: signature and definition locating, the `use`-declaration reader, `attributes_above`.
* **`composer/spec/cvlr/editor.py` (1,796 lines): rewritten** around §4.4. The per-kind tools,
  `MungeChoice`, the record digest, `AmendMunge`, `DropMunge` and `RevertMunge` go. `gate_edits`'
  cargo check and dep-info half stays.
* **`composer/spec/cvlr/tree.py`: §4.2.** Drafts, adoption, materialization, resync, confinement and
  `ProjectSource` stay.
* **`composer/spec/cvlr/verify.py`:** `stage` takes the overlay; `pristine_source`, `in_package`,
  `_drift_note` and the drift plumbing through `CargoCheck` and `VerifyRules` go. (Its unused munge
  imports at 80-89 go too.)
* **State, author, pipeline:** §4.1, §4.5-4.7. Also `harness.py`'s `<stem>.assumptions.json`,
  `LastCvlrAttempt.munges`, and `composer/cli/diagnostics/ap_trail_recover.py`'s rendering of
  checkpoint munges.
* **Templates:** `cvlr_munge_editor_system.j2` (charter, shapes, gating rule, tool names),
  `cvlr_munge_review_system.j2` (gating checklist), `cvlr_property_generation_system_prompt.j2`
  (tool list, "ask the editor" section), `cvlr_property_judge_system_prompt.j2` (per-kind weighing),
  the smaller references in `cvlr_property_generation_prompt.j2` and
  `cvlr_vacuity_system_prompt.j2`, and `composer/kb/resources/cvlr_baseline_facts.md` (which also
  disagrees with the prompts on how many kinds there are).
* **Tests deleted:** `test_cvlr_munge.py` (except its path-confinement cases),
  `test_cvlr_derive_swap.py`, `test_cvlr_import_swap.py`, `test_cvlr_module_redirect.py`, and
  `test_cvlr_editor.py`'s record-tool cases. **Kept and adapted:** `test_cvlr_tree.py`'s draft,
  feature, manifest and permit cases, `test_cvlr_editor.py`'s dep-info and approval-voiding cases.
  **Fixture updates:** `test_cvlr_judge_input.py`, `test_cvlr_author.py`, `test_cvlr_vacuity.py`,
  `test_cvlr_build_permit.py`, `test_cvlr_tuning.py`, `test_cvlr_restamp.py`,
  `test_cvlr_judge_round_cost.py`, `test_cvlr_knowledge.py`, `test_cvlr_worked_example.py`,
  `test_ap_trail_recover.py`.
* **New tests:** staging swaps one unit's overlay in and a sibling's out; a swap back writes the
  same bytes; a read sees the unit's own overlay, not the last staged one; the submit gate refuses
  an overlay path outside `ProjectSource` and one rustc never read; an approved edit is not
  staged until the author commits it; `source_edits` produces one patch per unit.
* **The vault tape** (`composer/testing/ui_harness_cvlr_vault.py`) records editor turns calling
  `swap_import` and `munge_function`. It is re-recorded (`scripts/record_cvlr_tape.sh`) once the
  editor's tools change.

---

## 6. Landing

* **#260 shrinks to the working tree**, close to the old P3a1: `tree.py` with drafts and the
  per-unit overlay swap of §4.2, `ProjectSource`, `RUST_BUILD_DIRS`, the graphcore bump. `munge.py`
  and its tests leave the PR. `rust_source.py` leaves it too and lands with `anchor_surface.py` in
  P4, its first caller, carrying only the scanner primitives.
* **P3a2 and P3a3 are dropped.** There is no munge vocabulary to land on its own.
* **P5 becomes the text editor:** the lifted shared core (§4.8), the CVLR editor over it, depinfo,
  the two prompts, and the state, judge and report changes, since none of them is testable without
  the editor.
* [cvlr-landing-plan.md](cvlr-landing-plan.md)'s table, P3a and P5 sections and paths appendix are
  updated to match.

---

## 7. Order of work

1. ~~**Measure the rebuild cost**~~ (done, §10 item 1) on klend, so the trade in §3 has a number before
   anything is deleted.
2. **Lift the shared editor core** out of `munge_agent.py` with EVM still passing unchanged.
3. **Per-unit overlays in state and the tree** (§4.1, §4.2), and per-unit reads (§4.3).
4. **The CVLR editor over the shared core** (§4.4), its prompts, and the author surface (§4.5).
5. **Judge and report** (§4.6, §4.7).
6. **Delete** `munge.py`, the munge-specific parts of `rust_source.py`, and the record tests; fix
   the incidental tests.
7. **Re-record the vault tape**; run the CVLR e2e test by hand.
8. **Reshape #260 and the landing plan** (§6), and mark the superseded docs (§8).

---

## 8. Docs this supersedes

Each gets a one-line banner pointing here; none is deleted.

* [who-edits-the-program.md](who-edits-the-program.md): §8.2-8.6, §9.3, §10-13. §1-7 (one editor, a
  reviewer, the author describes problems) stand.
* [single-working-tree.md](single-working-tree.md): §2.3 and §4 (the tree replays a union of
  records), and the first half of §2.4. §2.1, §2.2, the permit, and §3's cost case stand.
* [munge-and-working-copies.md](munge-and-working-copies.md): §4.2's CVLR charter and §5-6.
* [the-tree-is-a-vfs.md](the-tree-is-a-vfs.md): the run-wide munge layer in §5. The layering stands,
  per unit (§4.3).
* [the-state-behind-the-bytes.md](the-state-behind-the-bytes.md): the `DeriveSwap` record and its
  refusals. The case for swapping derives stands, as charter text.

---

## 9. Future work

* **A mechanical inertness check.** Compare rustc's macro-expanded output of the munged crate with
  the unit's feature off against the pristine crate's:

  ```
  RUSTC_BOOTSTRAP=1 cargo rustc --lib -- -Zunpretty=expanded
  ```

  `cfg` stripping happens before expansion and the output is pretty-printed, so inserted lines and
  formatting do not count; only a real change to the feature-off program does. Host toolchain,
  seconds. Untried: whether the bootstrap variable survives the sandbox, whether Anchor's proc macros
  expand deterministically, and whether absolute paths leak into the output.
* **One merged deliverable.** Combine every unit's patch into one gated patch per file, if users
  find merging by hand a burden.
* **Drift detection.** Record each overlay file's base hash and refuse to stage onto a project file
  that has changed since.

---

## 10. Open questions

1. ~~**How long is a program-crate rebuild on a large target?**~~ **Measured on klend
   (2026-10-14): a swap costs one rebuild of the program crate and nothing else.** Two units each
   gating one attribute in `state/reserve.rs`, staged alternately through `SharedTree.reconcile`,
   three rounds each way (37k lines of program source, 287 crates in the host graph):

   | build | cold | restage, no change | swap |
   |---|---|---|---|
   | host `cargo check` (rustc 1.74.1) | 25.6s | 0.1s | 1.2s |
   | `cargo-build-sbf` (solana 1.17.34) | 67.2s | 0.4s | 19.5s |

   Every swap rebuilt exactly one crate. The SBF figure is the one a submission pays, and it is
   the same rebuild a unit already pays whenever its own draft changed since its last build, which
   is the usual reason to run `verify_rules`. So the marginal cost is about 20s only on a build
   whose draft did not change. No mitigation is needed yet. Not measured: the `certora` and unit
   features (the harness modules and the CVLR crates were not compiled), and the Prover's own
   rebuild. The host check ran on klend's pinned 1.74.1, because the backend's
   `HOST_RUST_TOOLCHAIN` (1.90.0) cannot compile klend's `farms` dependency: its static size
   assertions fail under the `u128` alignment change in rustc 1.77.
2. ~~**Auto-apply or `commit_edit`?**~~ **Decided: `commit_edit`, as EVM does** (§4.5).
3. **Does the report need merge guidance?** Two units' patches to one file conflict textually but
   not semantically. A note in the rendered report may be enough.
