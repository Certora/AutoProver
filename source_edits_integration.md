# Source edits: integration guide

This document is for the frontend and cloud-orchestration owners. It covers what
changes in an AutoProve run's outputs now that the CVL author can edit the Solidity
source it verifies against. Nothing changes on the input side — no new flags, no
changes to how runs are launched — so this guide is entirely about the outputs: the
new `report.json` section, how the reference renderer presents it, and what edited
source does to the reproducibility of the existing on-disk deliverables.

During formalization, the author may now modify a copy of the project source to work
around constructs the verification tooling cannot handle (e.g. normalizing a
non-standard storage layout, hoisting inline assembly into a summarizable function).
Every edit goes through a review gate and carries the editor's written account of
what changed and why the change preserves the original behavior. The edits are
**virtual**: they live in an in-memory overlay, the on-disk project is never
touched, and the prover runs against a temporary materialized copy. When a
component's verification ran against edited source, its report outcomes are claims
about the modified code — and the report now says so, per component, with the full
diff.

## 1. `source_edits` in `report.json`

`schema_version` moves to `"3.1"`. The change is purely additive — a `"3.0"`
document still validates against the new model — so consumers should accept both
versions and treat a missing/empty `source_edits` as "every component was verified
against the on-disk source".

The new top-level field:

```jsonc
"source_edits": [
  {
    "component": "RateKeeper",
    // The editor's account of each applied edit, in application order.
    "applied_edits": [
      {
        // Opaque edit-store snapshot id; also shown in the HTML render.
        "edit_id": "edit-3f9c...",
        // What changed, in the editor's words (markdown-rendered prose).
        "executive_summary": "Normalized the constant storage slot to a proper ERC-7201 annotation ...",
        // Why the change preserves the original behavior (markdown-rendered prose).
        "why_sound": "The slot value is unchanged; only the declaration form differs ..."
      }
    ],
    // One cumulative unified diff: on-disk baseline -> the source the proof ran against.
    "cumulative_diff": "--- a/src/RateKeeper.sol\n+++ b/src/RateKeeper.sol\n@@ ..."
  }
]
```

Record semantics:

- A component appears **iff it delivered a result and at least one edit was
  applied**. Un-edited components, gave-up components, and the synthetic
  "Structural Invariants" entry never appear — the structural-invariant phase runs
  with editing denied by design, so its outcomes are always about the on-disk
  source. Foundry-backend reports never carry entries.
- `applied_edits` lists every edit that survived to the final working copy, in the
  order they were applied. `executive_summary` and `why_sound` are free prose from
  the editor; the reference renderer treats both as markdown.
- `cumulative_diff` is a standard unified diff, the concatenation of per-file
  chunks with `a/<path>` / `b/<path>` labels, paths relative to the project root.
  A file the editor created from scratch (e.g. a minted harness) diffs from
  `/dev/null`. There are never file deletions — the editing model can only add or
  modify files. Files an edit touched and a later edit reverted drop out (identical
  content produces no chunk).

The presence of a record means exactly this: **the component's rules, verdicts, and
run links in the main tables are claims about the modified code, not the code as
shipped.** The properties/rules/groups tables themselves are unchanged in shape —
edited components are not segregated the way gaps are; the record is a qualifier,
not an exclusion.

## 2. Rendering

What the reference HTML renderer does with the field, for FE parity:

- **"Source modifications" appendix** — one section per edited component: each
  edit's `executive_summary` and `why_sound` (markdown-rendered, the latter under a
  "Soundness argument" label), followed by the `cumulative_diff` in a collapsible
  block.
- **Per-group "edited source" badge** — a group is flagged when *any* of its member
  properties belongs to an edited component. The badge is warning-styled and links
  to the appendix.

The rule the FE should preserve: never present an edited component's verdicts
without the edited-source qualifier in sight. A "Verified" on modified code is a
different claim than a "Verified" on the shipped code, and the report's whole
design here is that the reader cannot miss the difference.

## 3. Disk and reproduction semantics

**The project tree is never modified.** Edits exist in three places only: the
`cumulative_diff` in `report.json`, the temporary directory the prover ran in
(materialized per run, deleted when the run ends), and the sources uploaded to the
cloud prover (the run link shows exactly what was proved — it is the ground truth).

**The deliverable set is unchanged** — `certora/specs/`, `certora/confs/`, the
properties/commentary files, `report.json`, `job_info.json`,
`components_to_prover_runs.json` all keep their existing locations and shapes.

**But for an edited component, the delivered conf no longer reproduces the proof on
its own.** The `.conf` references paths in the project tree, whose on-disk content
is the *unedited* baseline; re-running it verifies different code than the report
describes, and rules that depend on the edits may fail. The conf can even reference
an editor-minted file (a harness registered during generation) that exists only in
the diff, in which case it will not run at all against the pristine tree. To
reproduce an edited component's proof locally:

```
git apply <(jq -r '.source_edits[] | select(.component=="RateKeeper") | .cumulative_diff' \
    certora/ap_report/report.json)
certoraRun certora/confs/RateKeeper.conf
```

Apply one component's diff at a time, to a clean tree. Each component's edits are
an independent overlay over the same pristine baseline — two components may modify
the same file in different, mutually inconsistent ways, so there is no single
"edited project" and the diffs must not be stacked.

**Caching.** The edited working copy and the edit provenance are persisted with the
generation result, so a cache-hit re-run reports the same `source_edits` as the run
that produced it — the proof's source view is never silently lost between runs.
