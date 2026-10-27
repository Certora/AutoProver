# Landing the CVLR backend

`eric/solanaProver` is 128 commits over 165 files — about 95,000 inserted lines, of which one
recorded tape is 51,000 and one pinned fixture is 4,200. It cannot be reviewed as a branch. This
document breaks it into pull requests that each stand on their own, ordered so that the work with
no CVLR dependency lands first.

It is a decomposition, not a schedule. Nothing here says who does the work or when, and the sizes
are the argument for the split rather than an estimate of effort.

**Two branches carry the work, and the PRs are cut from `eric/solana-staging`.** `feature/solana`,
from 2026-10-14, is where the end-to-end runs and the alpha are prepared. It was cut from
`eric/solanaProverRebase`, the line rebased onto master after #258. `eric/solanaProver` carried
parallel work, and was merged into `feature/solana` on 2026-10-15 (`a7e0251d`).
`eric/solana-staging` is where the slices are shaped for review, and where review changes come
back. How the two stay in step with each other and with master is in
[Keeping the branches in step](#keeping-the-branches-in-step). Every `git checkout <branch> --
<paths>` below names `eric/solana-staging`, and the counts in the next two paragraphs describe
`eric/solanaProver` and the rebased line as they were then.

New work on either branch rides the slice that owns the files it changes, under rule 5: the
first unlanded PR whose capability it extends. Work on files a merged PR owns is a follow-up PR to
master. Work that needs a new file names its slice in the appendix in the same commit. What the
merge brought, and the work since, is assigned in
[What the merge and later work carry](#what-the-merge-and-later-work-carry).

**Re-cut 2026-09-18.** The first draft split the branch along its module DAG — leaves first, wiring
last. [#243](https://github.com/Certora/AutoProver/pull/243) is what that produces: `composer/cargo/`,
1,242 lines, eight new files, no caller anywhere in the diff. The review feedback was that it cannot
be evaluated out of the context of its usage, and that is a correct reading of the partition rather
than of the PR — the same draft asked for three more like it, and made every one of twelve backend
PRs unreachable until the last. Wave 2 is re-cut below so that each feature arrives in the first PR
that uses it. Rule 5 states the criterion; the [old-name map](#appendix-the-old-partition-and-where-it-went)
is the last appendix. The rules, the deferrals and the drops are otherwise unchanged.

**Where it stands, 2026-10-27.** Wave 1 is finished: S4 was closed and folded into P7. Of wave 2,
**P1, P2, P3a1, P3b1 and P3b2a have merged**: P1 as [#248](https://github.com/Certora/AutoProver/pull/248)
(`2eb4a391`), P2 as [#258](https://github.com/Certora/AutoProver/pull/258) (`b999c306`), P3a1,
the working tree, as [#260](https://github.com/Certora/AutoProver/pull/260) (`68f60985`), and P3b1,
submitting a generated draft, as [#267](https://github.com/Certora/AutoProver/pull/267)
(`9068e362`), and P3b2a, what a verdict depends on, as
[#268](https://github.com/Certora/AutoProver/pull/268) (`70f5850a`). The corpus half of P6 merged
ahead of all five as
[#244](https://github.com/Certora/AutoProver/pull/244). **P3a2 is dropped.** The branch replaced the
munge records with text edits ([who-edits-the-program.md](./who-edits-the-program.md) §18), so there is no
munge vocabulary left to land; see [P3a](#p3a--the-working-copy-and-what-may-be-changed-in-it).
`feature/solana` took master after #260 by merge (`899981fd`). On 2026-10-27 the two branches were
brought into step: `eric/solana-staging` merged `feature/solana` (`068ab6d4`) and then master after
#268 (`5e2f7470`), and `feature/solana` fast-forwarded to it. The next PR is **P3b2b**, [#280](https://github.com/Certora/AutoProver/pull/280), what a
verdict means. It was stacked on #268 and still carries #268's first commit, so it is rebased onto
master before review. **P1b**, SDK 3 targets, follows it: a follow-up to master that the merge of
`eric/solanaProver` made necessary. P3b2 was split in two on 2026-10-23, after its rebase onto
master. P3b had grown to about +3,250 code and +1,530 tests, and on
2026-10-16 it was split into three: P3b1, submitting a generated draft; P3b2, the rest of what a
verdict depends on; and P3b3, the vacuity diagnostic. See [P3b](#p3b--a-generated-harness-and-what-its-verdict-means).

The branch was rebased onto master after #258 (`709293a9`): 291 commits, 195 files, about 100,000
inserted lines. P1 changed a lot in review, and the branch carries 727 lines of later work on files
P1 now owns. The [list below](#what-the-branch-carries-on-top-of-p1s-files) assigns each of those to
the PR it rides.

**Revised 2026-10-12 for the API-documentation work.** Between 2026-09-24 and 2026-09-30 the branch
carried out [cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md). This plan had not been revised for it,
and it changed four of its slices:

* **The CVLR source mount is gone** (`5b2afeff`). `crate_mount.py`, `source_tools.py` and
  `cvlr_source_tools.j2` no longer exist, so P6 cannot carry them. The author and the judge reach
  CVLR through a research sub-agent, `cvlr_research`, which now rides **P6** in their place.
* **The crate reference is produced here.** `composer/scripts/cvlr_api_docs.py` generates it from
  rustdoc into a second corpus, `cvlr_api_kb` (`1aa9f5a9`). C7b had been held back until that
  producer was reworked. The rework has happened, and C7b is now the producer, the corpus and its
  tools. It moves into wave 2 ahead of P6, which reads it. See
  [C7b](#c7b--the-cvlr-api-corpus).
* **`cvlr_kb` is now the manual and nothing else** (`26f7b9c9`). The practice entries C7c existed to
  disclose are no longer corpus content, so C7c is dropped. See
  [Dropped: the practice manifest](#dropped-the-practice-manifest).
* **The branch re-registered the corpora by tag.** It did this for both corpora, which goes against
  [Dropped: naming the CVLR corpus by tag](#dropped-naming-the-cvlr-corpus-by-tag). This plan keeps
  the drop. The branch has since been reworked to match (`008a7397`); see
  [decision 8](#decisions-to-make-before-starting).

---

## Five rules, two of them learned the expensive way

**1. Check master for an existing PR before writing one.** On 2026-09-16 a PR was opened here for a
bounded retry around the cloud results fetch — [#238](https://github.com/Certora/AutoProver/pull/238).
[#223](https://github.com/Certora/AutoProver/pull/223) had been open since 2026-09-08 doing the same
thing: the same two files, the same new function name, the same constants. Nobody looked. Every PR
below touches shared code that somebody else may also be fixing, so the first step of each is
`gh pr list --state open` and a search for the paths it touches.

**2. Assemble by file-set, not by commit range.** The branch's history interleaves shared-code edits
with CVLR ones — a single commit routinely does both, because that is how the work was done. So a
PR is built by branching from `origin/master`, `git checkout eric/solana-staging -- <paths>`, and
committing once with a message written for a reader who has no Solana context. Cherry-picking the
branch's commits does not produce these PRs and will fight you.

**3. Each PR must pass on master alone.** `uv run --no-sync pytest tests/ -m "not expensive" -q` and
`uv run --no-sync pyright`, on a branch off `origin/master` with only that PR applied. The structural
half of the same rule: nothing outside `composer/spec/cvlr/` may import from it. Where a shared seam
needs to know about CVLR, it takes a parameter or a registry entry instead — that is why the seams in
wave 1 look the way they do.

**4. Shared-code work goes to master directly.** The rough-draft gate fix
([#233](https://github.com/Certora/AutoProver/pull/233)) was written on this branch and landed on
master as its own PR, and the branch, rebased in those days, then dropped it at no cost. Landing
shared fixes on master first is the pattern; a merge drops them as cheaply.
Every shared fix that instead waits on this branch is a merge conflict being saved up. The
exception rule 5 carves out is a *refactor* with no second user, which is not a fix.

**Both branches take changes by merge, never by rebase or cherry-pick.** The full procedure is
[Keeping the branches in step](#keeping-the-branches-in-step).

The history below is from the rebase era, before `feature/solana`. Its one lesson still applies: a
file's conflict resolution must start from history that has merged.

The rebase is free only when the PR lands as it was cut. #240 and #248 did not: #248 merged after
61 review commits. Replaying the branch's own history of those files onto the reviewed versions
conflicts on every commit, and the conflicts mean nothing. What worked on 2026-10-01: replay every
commit *without* its changes to files both sides touched, then bring those files up to date in one
reconciling commit. For each file that commit is a three-way merge of master and the branch tip,
based on whichever earlier version of the file is closest to the branch's copy. **Choose the base
only from history that has merged.** One file's closest match was a commit on the still-open #241.
Taking it as the base made the branch's S4 work look like history master had moved past, and the
merge quietly dropped it.

**5. A PR contains the usage that justifies it.** This is the one the review taught. A module lands
in the first PR that calls it, and a PR that adds a seam, a parameter or an abstraction ships the
caller that makes it the right seam. The test is not "does it have a caller somewhere on the
branch" — everything does — it is: **can a reviewer decide whether this code is right without
reading a PR that does not exist yet?**

Three corollaries, because the rule is easy to over-apply:

* *A test can be the usage,* if it drives the real API the way production will. What does not count
  is a test that only asserts the module's internals back to itself.
* *It applies per file, not per PR.* A capability whose tools are usable today can land while the
  one template that only a future prompt includes waits for that prompt — see **P6**.
* *It has a floor.* A fifteen-line defensive parse called from the function above it does not get
  held back for the caller that will exercise its new branch. `job_input` was planned as that case
  in **S4**. Since #232 it has no caller on master at all (wave 1, below), so it waits for P3b3.

## Keeping the branches in step

Two workstreams run at once: slicing PRs out of `eric/solana-staging` and taking their review back,
and end-to-end testing on `feature/solana`. Changes move between the branches and master only by
merge:

```
master ──merge──▶ eric/solana-staging ◀──merge──▶ feature/solana
   ▲                     │
   └──── slice PRs ──────┘
```

* **Master reaches the branches only through `eric/solana-staging`.** After a slice merges, merge
  `origin/master` into `eric/solana-staging`. `feature/solana` gets master by merging staging, so
  a squashed PR is reconciled once, by whoever cut it.
* **Check the slice before that merge.** A slice lands as a squash commit, so nothing ties it to
  the branch's commits, and a merge resolved as "keep the branch" drops every review change that
  was not taken back. List the lines the squash adds that the branch lacks, file by file, and
  account for each: taken back, superseded by later work, or deliberately held. #267's check on
  2026-10-27 found the `certora_unit_` prefix, the deliverable directories in `gen_types`, the
  simpler `.cvlr_rules` reader and the derived `HarnessTarget.module_path` never taken back.
  Where a review change can be written on staging first and cherry-picked onto the PR branch, do
  that. The PR branch is squashed away, so copying in that direction costs nothing.
* **`eric/solana-staging` and `feature/solana` merge each other in both directions, and never
  cherry-pick.** Each merge moves the merge base, so the next one conflicts only on what changed
  since. A fix found in an end-to-end run is committed on `feature/solana` and reaches staging by
  merge, often. Staging reaches `feature/solana` when the alpha side chooses, after the fast
  suite and a tape replay pass.
* **Neither branch is rebased.** A rebase rewrites the merge base the other branch depends on, and
  AIAutoProver's `feature/solana` pins `feature/solana` commits by SHA. PR branches cut from
  staging can be rebased freely.
* **An alpha freeze is a release branch** cut from `feature/solana`. Fixes are cherry-picked into
  it, which is safe because it never merges back.
* **Everything on either branch reaches the other, so material that is not public stays out of
  the repository.** AutoProver is public. Slices are cut by hand, so alpha-only documents on
  staging do not reach master, but they are published by being pushed.

When a merge brings a slice back, resolve each conflict to master's reviewed version plus what the
branch has added to that file since. Two things a clean merge does not show: a definition both
sides added in different places merges as two definitions (`conf.py`'s `SettingsEdit` did), and
rerere can replay a resolution recorded for a different merge (it did for `uv.lock`). Run the fast
suite and pyright after every merge, and sync a worktree's venv in full when the merge brings a
new dependency.

## Wave 1 — shared code, no CVLR dependency

These are reviewable by anyone who knows the subsystem, and none of them mentions Solana except in
motivation. Sizes are insertions/deletions against master.

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **S1** Confined builds: one scratch directory, a readable git config, an unreadable output — [#239](https://github.com/Certora/AutoProver/pull/239), **merged** `27f2fe93` | 10 | +247 −37 | Three findings from making Rust builds run under the sandbox, and one story. `composer/layout.py` declares `CERTORA_DIR` / `INTERNAL_DIR` where `composer.sandbox` can name them without importing pydantic, which that package stays free of. The sandbox's scratch (`CARGO_HOME`, tmp) moves under `INTERNAL_DIR`; `RUST_FORBIDDEN_READ` withholds that directory — and the entry itself, so graphcore prunes the subtree instead of rejecting a 730 MB registry file by file — wherever it sits; and the rule is read off the ecosystem `cli_pipeline` is handed rather than passed beside it, so the two cannot disagree. And `git_config_ro_paths` grants the global git config, without which libgit2 refuses to open a fully warm cached git dependency and reports it as an offline-mode *network* error. |
| **S3** The prover layer learns there is more than one chain — [#240](https://github.com/Certora/AutoProver/pull/240), **merged** `568ba02d` | 28 | +703 −164 | Three parts, two of them one seam. *Which CLI:* `ProverApp` names the three entry points `certora_cli` ships, `import_prover_entry` resolves one honouring `$CERTORA`, and `prover_app` narrows an untrusted string at the single boundary where one arrives. *Which frames:* a counterexample stops being a rendered string and becomes data — trace, assertion, source span — so `classify_violation` can decide whether a violation says anything about the program; `TraceShape` then says which frames of a chain's trace survive rendering. Between those two points nothing learns which chain ran, which is the claim `tests/data/solana_cex` measures. *How a run is configured:* `ProverOptions` carries the app, the server and the Prover's budget as fields, instead of a `list[str]` of CLI flags it read its own meaning back out of, and one `ProverOptions` reaches the codegen tool rather than being reassembled from parts. That part grew out of the review, and is most of the difference between the size this PR opened at and its size now. |
| **S4** Report: what a component gave up on — [#241](https://github.com/Certora/AutoProver/pull/241), **closed**, folded into P7 | 6 | +160 −32 | `Abandoned` replacing a `None` that discarded the reason, and `GaveUpComponent.reason` where it lands — the only change in wave 1 that alters an EVM run's output. Plus `make_prover_fetcher` typed at `ReportableResult` rather than at CVL, and `job_input`, the one part of the PR with no caller on master: POU cannot parse a Solana job link, and the best-effort fetch turns that into every rule UNKNOWN. |

**Where the wave stands.** S1 and S3 merged on 2026-09-17, in that order, half an hour apart. S3
answered a changes-requested review by absorbing the `ProverOptions` rework rather than by argument,
which is most of why it nearly doubled between opening and merging. S2 is dropped (below). S4 was closed
unreviewed on 2026-10-01, and its parts went to P3b3 and P7 (below). S5 is gone: its mount is
dropped (below), its one live function folds into P6, and its docstring fix goes to master on its
own. Wave 1 is therefore finished.

Dependencies inside the wave: none — the one that remained was the CLI seam before the trace
parser, and they were one PR. Merging the sandbox work into
one PR removed the wave's other ordering constraint, which had been an artefact of the split rather
than of the code: the forbidden-read test imports the sandbox's own scratch-directory constants, so
the two could never have been reviewed apart.

**Why S4 was folded rather than rebuilt.** By the time P1 merged, #241 conflicted with master in
`report_prover.py`. The cause was [#232](https://github.com/Certora/AutoProver/pull/232), merged
2026-09-20. #232 changed the CVL verdict fetch to read every run that covers a component's spec
(`GeneratedCVL.run_link_specs`), and to rewrite a `/jobStatus/` link to its `/output/` view before
calling POU. That left each of S4's three parts somewhere else:

* *`job_input`* is no longer needed on the verdict path. POU's `extract_job_id` rejects the raw
  Solana link but parses the `/output/` form #232 produces; this was checked against the installed
  POU. Its remaining caller is the unsat-core fetch behind `explain_vacuity`, which takes the link
  as given. That is **P3b3**.
* *Retyping `make_prover_fetcher`* is no longer possible. The CVL fetcher now reads a field only
  `GeneratedCVL` has. The rebased branch keeps it and adds `make_run_link_fetcher`. Its only caller
  is `CvlrFormalizer`, so it lands in **P7**.
* *`Abandoned` and `GaveUpComponent.reason`* merge cleanly, and master already has the callers that
  would use them. The CVL author, foundry and the Rust wheel all return `GaveUp(reason=…)`, and
  `pipeline/core.py` discards it. This part could have gone to master alone. It rides **P7**
  instead, as a decision about review cost rather than a consequence of rule 5. P7 is where the
  CVLR backend's own give-up first reaches a report. The cost is that a shared report-schema
  change and an EVM-visible behaviour change sit inside the largest CVLR PR, and P7's body has to
  say so. Meanwhile, the report package keeps moving under it ([#185](https://github.com/Certora/AutoProver/pull/185)
  is open there).

The rebased branch shows the resolved shape of all three.

Two unrelated fixes and two annotation tightenings are still unlanded. They now live in
[Miscellaneous](#miscellaneous-shared-changes-master-could-take), along with the other shared
changes the branch has picked up since.

**S6 has moved.** It was here on the reasoning that a `KnowledgeBundle` refactor is shared code with
no CVLR dependency, which is true and is exactly what rule 5 now disqualifies: it would land a
generic mechanism with one instance and no second reader. It merges with **K1** in wave 2, where the
second instance is in the same diff.

---

## Wave 2 — the backend, in capability slices

Every PR here is a thing the backend can *do*, carrying the modules that first do it. The order is
still forced by the DAG, but the unit is no longer a DAG layer: it is a sentence of the form "point
it at a Solana project and it …", and the PR contains both halves of that sentence.

Sizes are insertions against master, code and tests counted separately, because the ratio is part of
the argument: a slice whose tests are a third of it is exercised, and one whose tests are a tenth of
it is not.

| PR | Code | Tests | What it can do when it lands |
|----|------|-------|------------------------------|
| **P1** Resolve the project — [#248](https://github.com/Certora/AutoProver/pull/248), **merged** `2eb4a391` | +3,697 | +2,391 | Point it at a Cargo workspace: it names the package under verification, resolves which `cvlr` / `cvlr-solana` the build gets, writes the prover conf, and scaffolds a CVLR workspace into a project that has none. |
| **P2** A hand-written rule in, verdicts out — [#258](https://github.com/Certora/AutoProver/pull/258), **merged** `b999c306` | +639 | +624 | Build a CVLR harness for SBF and submit it, and read the verdicts back. The deterministic submission path, end to end, with no agent in it. |
| **P3a1** The working copy — [#260](https://github.com/Certora/AutoProver/pull/260), **merged** `68f60985` | +388 | +535 | Materialize a fresh working tree over the project for each run, holding every unit's harness draft and the staging unit's edits to the program, and refuse an edit outside the project's own crates. |
| ~~**P3a2** Module redirects~~ — **dropped** | — | — | Nothing to land: the munge records are gone, and a module redirect is now a text edit the editor gates on the unit's feature (P5). |
| **P3b1** Submit a generated harness draft — [#267](https://github.com/Certora/AutoProver/pull/267), **merged** `9068e362` | +847 | +616 | Declare each unit's harness module behind its own feature, stage the draft into the shared tree, build it, read the rules the build lists from the `.so`, submit those, and stamp the draft when every one of them verified. |
| **P3b2a** A verdict depends on more than the draft — [#268](https://github.com/Certora/AutoProver/pull/268), **merged** `70f5850a` | +661 | +562 | Stage the unit's summaries, edits and prover settings with the draft and stamp them with it; let the author add summaries and move the two prover settings; and say which summaries matched nothing. |
| **P3b2b** What a verdict means — [#280](https://github.com/Certora/AutoProver/pull/280), draft, to rebase onto master after #268 | +514 | +279 | Stamp when every rule is accounted for rather than verified: let the author mark expected failures and re-decide the stamp from the last run, and report incomplete checks, rules with no verdict, and the functions the Prover treated as external. |
| **P3b3** Why a rule passed vacuously | ≈ +630 | +299 | Rerun a rule reported vacuous with its unsat core collected, decide whether that proof needed the rule's own assertion, and have an analyst read the core against the harness and the program. |
| **K1** A knowledge bundle, its second instance, and the CVLR recipes | +1,220 | +170 | Hold two corpora behind one context mechanism instead of a fork of it, carry the run-invariant CVLR fact the author's prompt would otherwise restate, and index the recipes an author retrieves when stuck. |
| **P4** The Anchor surface and the worked example | +831 | +720 | Read an Anchor program's account and handler surface, and render the worked example an author is shown against that program rather than against a generic one. |
| **C7b** The CVLR API corpus | +1,580 | +700, and 19,751 of fixtures | Generate the CVLR API reference from the pinned crates' rustdoc, ingest it as `cvlr_api_kb`, and search it with three tools that say they outrank the manual. |
| **P5** The munge editor | ≈ +1,460 | ≈ +470 | Ask an agent to edit the program as text in the unit's own overlay, gated on the unit's feature, and have a second one accept or reject it; the judge sees the edits and the report gives one patch per unit. |
| **P6** The authoring loop | ≈ +2,150 | ≈ +2,250 | Give the author a program and get CVLR rules, judged, with a research sub-agent over both CVLR corpora behind the author and the judge. |
| **P7** The backend, the pipeline and the CLI | +1,300 | +1,600 | `console-solana` / `tui-solana` on a real target. |

### P1 — Resolve the project

**Merged 2026-10-01 as [#248](https://github.com/Certora/AutoProver/pull/248), `2eb4a391`:** 36
files, +6,088 −41, after 61 review commits. It differs from the description below in five ways:

* `cvlr_reference.py` became `composer/spec/cvlr/reference.py`. Its vocabulary changed too:
  `chain` became `chain_crate`, and `PlatformGeneration.crates` became `sdk_crates`.
* Two `composer/cargo/` modules were added. `manifest.py` validates `Cargo.toml` with pydantic,
  and `features.py` names a feature as `CargoFeature`. `metadata.py` now validates
  `cargo metadata` with pydantic as well. A workspace is read with `Workspace.read`, and the crates
  are resolved with `CvlrSources.of`.
* `ProverSettings` became `TunableConf`, and `with_rules` became `RuleSelection.apply_to`.
* The scaffold writes its harness files from templates (`cvlr/harness_files/`). The empty Anchor
  summaries layer was dropped, and so was the nonlinear solver portfolio.
* `tests/data/vault_sbf_symbols.txt` came with it, so P3b no longer carries it.

What did not land: the `cvlr-preflight` console script (which nobody has written yet) and the
`entry.py` hunks. Both move to **P7**, which is where `entry.py` gets its first caller. Until then,
`preflight` on master is reached only from its tests.

As planned, P1 carried these files:

`composer/cargo/{metadata,session}.py`, `composer/spec/cvlr_reference.py`, and
`composer/spec/cvlr/{conf,crates,env_paths,forks,scaffold,preflight}.py` with the starting inlining
and summary layers under `envs/`. It also carries the env half of `tuning.py` — the layer families
and `compose_env`, which the scaffold writes composites with — and **P3b2a** appends the summaries the
authoring loop adds.

And `composer/prover/conf.py`: writing a prover conf and scoping it to a rule selection, shared with
the CVL side rather than written a second time in `cvlr/conf.py`. Its CVL callers (`source/prover.py`,
`source/author.py`, `source/artifacts.py`, `natspec/task_description.py`) move onto it in the same PR.
That is the one part of P1 that edits existing files; the settings a CVL conf carries are unchanged.

This is the whole of the old **C1a** and **C1c**, plus the half of **R1** they call. Nothing in it
is there for a later reader: `crates` and `env_paths` exist to consume `cargo metadata`, `scaffold`
consumes `conf`, `env_paths` and `cvlr_reference`, and `preflight` consumes all of them. A reviewer
who starts at `preflight.select` reaches every module in the PR. (A `refresh_cvlr_envs.py` that
re-vendored the env files from the template was on this list; the files are maintained here now, and
the script is gone.)

It carries ~40 lines of new work that no wave-3 PR needed: a `cvlr-preflight` console script that
runs preflight and prints what it found. That is what makes the slice *runnable* rather than merely
imported, it is the natural first user of `preflight`, and it stays afterwards as the way to debug a
target that the pipeline then mis-selects. `entry.py`'s argument parser, confinement construction
and `parse_main_program` land with it; `cvlr_executor` and `_entry_point`, which need the pipeline,
wait for P7. #248 as opened carries neither the script nor the `entry.py` hunks, so both are still
to add, or to move to P7.

The test splits are done. `tests/test_cvlr_scaffold.py` and the P1 half of
`tests/test_cvlr_plumbing.py` are on master. The plumbing file's P2, P3b1 and P3b2 cases remain on the
branch, appended to master's half.

#### What the branch carries on top of P1's files

That is +727 −54 across 14 files P1 now owns. Before the rebase, these were edits to the branch's
own copies of those files. After it they are edits to master's, made in the reconciling commit
`69e20da0`. The commits that first made them are still on the branch, minus those hunks. Each one
rides the first PR that uses it:

| Change | Files | Rides |
|---|---|---|
| Name the package to build by its manifest (`2c852922`) | `preflight.py` | **master now** (below) |
| `CvlrSources.roots()` removed, along with the CVLR source mount | `crates.py` | **master now**, with the fix above. Nothing on master calls it either |
| `OptimisticLoop` (the author's reason for switching it on) | `conf.py` | **P6**, with the judge briefing that reads the reason. #268 (P3b2a) landed `optimistic_loop` as a plain bool and stores no reason |
| The submission purpose: `CheckVerdicts \| CollectUnsatCore`, and `RunOverlay.purpose` | `conf.py` | **P3b3**: only the unsat-core run sets it |
| `SummaryDirective`, `TuningFiles`, and the rest of the tuning layer | `tuning.py` | **P3b2a** |
| Starting-directive additions: Anchor account validation, `system_program::transfer`, `try_borrow_lamports`, and `realloc` moved from summarized to inlined | `envs/*.txt`, `tests/test_cvlr_env_paths.py` | **Held** on the branch, and submitted together later (see *Deferred: the starting tuning directives*) |
| `ProgramModel`, `models` / `companions` replacing `specializations`, and `withholding` (`67f2215e`, `eb18bc80`) | `reference.py`, `tests/test_cvlr_reference.py` | **P7**: `entry.py`'s `--withhold-crate` is the caller |
| The build, conf and submission cases | `tests/test_cvlr_plumbing.py` | **P2**, **P3b1** (the prover accounting) and **P3b2a** (the `optimistic_loop` conf case) |

**The manifest-path fix is now one hunk for master.** `2c852922` stops the compile checks naming
their package with a bare `--package <name>`. Cargo refuses that whenever the name is ambiguous. It
is ambiguous whenever the program under verification is *also* a published crate that one of its own
dev-dependencies pulls back in: the local and published copies then share a name at two versions.
`--manifest-path` names exactly one package. Passing it alongside `--package` does not help; that
was measured. #248 landed the `manifest_dir` parameter on `CargoSession.check`, but master's
`preflight.py` still calls `check(package=...)`. The rebased branch's one-line hunk is the fix. The
`verify.py` and `editor.py` halves ride **P3b1** and **P5**. Nothing on master exercises the fix,
because no corpus project is a published crate. It was found by pointing preflight at the Solana
stake program (`docs/stake-benchmark.md`). A regression test needs a fixture whose package name
collides with its own graph, and that test is still to write.

#### Later work on merged files: the toolchains, and the Prover's signed math

Three commits from 2026-10-12 and 2026-10-13 change `conf.py` and other files that P1, P2 and S1
own. They came from pointing the backend at SDK 3 targets and running it in the Solana image.

| Change | Files | Rides |
|---|---|---|
| Build with platform-tools v1.53 (`f5179ae0`). v1.43's cargo 1.79 cannot parse the edition-2024 manifests an SDK 3 lockfile is full of | `conf.py` *(`PLATFORM_TOOLS_VERSION`)*, `tests/data/vault_sbf_symbols.txt` *(regenerated)*, `tests/test_cvlr_env_paths.py` *(the coverage counts)* | **master now**, as *Toolchains* (below) |
| Run every cargo command with one host Rust, `HOST_RUST_TOOLCHAIN`, rather than the target's `rust-toolchain.toml` pin (`2f2c8ecc`). The image's rustup home is read-only to the runtime user, so a pinned toolchain the image lacks cannot be installed | `conf.py` *(`HOST_RUST_TOOLCHAIN`)*, `composer/cargo/{metadata,sbf,sbf_build_script,session}.py`, `composer/sandbox/{config,recipes}.py`, `preflight.py`, and hunks in `tests/test_cvlr_{plumbing,scaffold}.py` and `tests/test_sandbox_config.py` | **master now**, as *Toolchains*. The `pipeline.py` and `tests/test_cvlr_entry.py` hunks ride **P7**; `Dockerfile.solana` and `tests/test_cvlr_image.py` ride **R3** |
| `-solanaTACSoundSignedMath`, and `prover_version: master` until the Prover releases what the flag needs (`91f4261d`) | `conf.py` *(`BASE_CONF`)*, `tests/test_cvlr_plumbing.py` *(hunk)* | **Held** (see *Deferred: sound signed math*) |

**Toolchains is one follow-up PR to master, about +200 lines.** Both commits are about the build a
real target gets: which platform-tools release compiles the program, and which host Rust runs
cargo around it. Their tests run without a toolchain or the image. One overlap to cut by hand:
the held starting directives also change counts in `tests/test_cvlr_env_paths.py`, so its v1.53
lines have to be separated from theirs. The loop-bound test's v1.53 disassembly stays on the
branch with that test, which #258's review took out.

### P2 — A hand-written rule in, verdicts out

**Merged 2026-10-02 as [#258](https://github.com/Certora/AutoProver/pull/258), `b999c306`:** 10
files, +1,269 −8. What review changed is below. The last change came after the branch had stacked on
#258. The build script, its command file and the conf moved out of the working tree, into an `into`
directory the caller supplies (`write_build_script` refuses one the confined build can write). The
Prover runs that script unconfined, and the script reads its confinement from the command file, so a
`build.rs` that could rewrite either one could make the Prover's rebuild run unconfined. The
branch's reconcile (`fc61b4b6`) supplies `.cvlr_work/submissions`, a sibling of the shared tree. It
rides **P3b1** (`VerifyDeps.submissions`) and **P7** (`SUBMISSIONS_DIR`, created in
`CvlrStagedFormalizer.begin`). `tests/test_cvlr_end_to_end.py` passed on the rebased branch.

`composer/cargo/sbf.py`, `composer/spec/cvlr/{prover,rules}.py`, and
`tests/test_cvlr_end_to_end.py`.

This is the slice with the best exit criterion in the whole plan, and it was invisible in the old
partition because `sbf` was in **R1**, `prover` was in **C5a** and `rules` was in **C4a1** — three
PRs in two waves. `test_cvlr_end_to_end.py` already states the criterion: clone
`Certora/SolanaExamples`, build its two minimal CVLR projects, submit them, and compare against the
expected-verdict files their own CI compares against — including the rule that is meant to fail and
the one that is meant to fail sanity. It imports exactly `cargo.sbf`, `cargo.session`, `cvlr.conf`
and `cvlr.prover`, which is to say: exactly P1 and P2 and nothing else.

`rules.py` (reading declared rule names out of harness source) was planned to join them, because
`test_cvlr_loop_bound.py` needed it. That test built a probe, submitted it, and measured what a
loop bound actually does. Review on #258 removed both. The test's scenario is not in this tree,
and what it checks belongs to the authoring loop. Without it, `rules.py` has no caller in P2, so
it moved to **P3b1**, where `verify` read it. P3b1 then stopped reading names from the draft: the
gate reads the names the build lists in the `.so`'s `.cvlr_rules` section (`cvlr/manifest.py`). So
`rules.py` has no caller in P3b1 either, and waits for **P6**, whose publish gate reads it.

Review also took `UnanalyzedCexHandler` out. `run_submission` now requires its caller to choose a
counterexample handler instead of defaulting to one that skips analysis. The end-to-end test,
which reads only verdicts, passes a local stub. The handler's first production caller is P3b1's
`verify_rules`, which uses it when the unit has no counterexample analysis, so it lands there. P3b3's
unsat-core diagnostic run is the second. P2 no longer changes `composer/prover/`.

The tuning-directive additions came out as well. Nothing in P2 reads the starting tuning files: they
reach a build only through the scaffold, and nothing in P2 scaffolds. They are held instead (see
*Deferred: the starting tuning directives*).

Marked `expensive` and skipping, named, when there is no toolchain — so the routine gate still
passes on a machine without one.

### P3a — The working copy and what may be changed in it

Planned as two PRs: **P3a1**, the working tree with the munge kinds it replays, and **P3a2**, module
redirects. On 2026-10-14 the branch replaced the munge records with text edits
([who-edits-the-program.md](./who-edits-the-program.md) §18): the editor writes whole files into the unit's
own overlay and writes the `cfg` gate on the unit's feature itself, as EVM does. `munge.py`, the
record-reading half of `rust_source.py`, the record replay in `tree.py` and their tests were
deleted. P3a1 shrank to the tree, and P3a2 was dropped.

#### P3a1 — The working copy

**Merged 2026-10-16 as [#260](https://github.com/Certora/AutoProver/pull/260), `68f60985`:** 8 files, +923 −9.

- `composer/spec/cvlr/tree.py`: `SharedTree`, the run's one working copy, derived from state as
  the project, every unit's draft, and the staging unit's edits. `ProjectSource`, the allowlist an
  edit may write: the source directories and manifests of the project's own path crates, as
  `cargo metadata` resolves them. `check_edit` returns the key an edit writes, or why it may not.
- `composer/layout.py` (`RUST_BUILD_DIRS`) and `composer/pipeline/ecosystem.py`: one list of the Rust
  build directories, for the agent's source tools and the tree.
- `composer/spec/cvlr/preflight.py` *(hunk)*: `CvlrPreflight.source`.
- The graphcore pin, `85be3db` → `9f4e9fc` (graphcore #39, `PersistentMaterializer`).
- `tests/test_cvlr_tree.py`, less its harness cases, and a `tests/test_fs_forbidden_read.py` hunk.

What review changed: an overlay is keyed by the project-relative path it writes, so a path with `..`
(`NotProjectRelative`) or a second spelling of one file is refused, and a symlinked path must be
editable at both ends. A tree that overlaps the project is refused when it is constructed. A staged
draft is a `_UnitDraft` rather than a tuple.

Nothing in production calls the tree until P3b1.

#### What the branch carries on top of #260's files

| Change | Files | Rides |
|---|---|---|
| The harness case: unit-feature gating | `tests/test_cvlr_tree.py` | **P3b1**, appended to master's file. It imports `harness`. The build-permit cases were dropped; `tests/test_cvlr_verify.py` covers the permit in **P3b1** |
| `.cvlr_work` out of `NOT_PROJECT_SOURCE`, and `materialize`'s docstring (`f1dd37fc`) | `tree.py` | **master now**. #260 already refuses a tree inside the project, so nothing on master puts a tree there |
| The editor's write filter and submit gate, on `check_edit` | `editor.py` | **P5** |

#### ~~P3a2 — Module redirects~~

Dropped 2026-10-14. There is no munge vocabulary to land on its own. A module redirect is now one of
the shapes the editor's charter describes and writes as text (P5), and
`tests/test_cvlr_module_redirect.py` is deleted.

### P3b — A generated harness, and what its verdict means

**Split 2026-10-16 into three, and re-cut the same day.** Measured against master, P3b had become
about +3,250 code and +1,530 tests, not the +1,919 and +1,180 planned; `verify.py` alone was 1,251
lines. The vacuity diagnostic came away first, by moving files rather than cutting them: it is the
second half of the slice's own sentence, a rule that passed for a reason that says nothing about the
program. That became **P3b3**.

The rest was first cut into writing the harness and verifying it. That put the author's state and
tools in one PR and the gate in the next, so the first landed tools with nothing to call them. It
was re-cut along a different line: **what a stamp depends on**. P3b1 submits a draft and stamps it on
its verdicts alone. P3b2 adds everything else a verdict depends on, so each PR's code is reached by
its own `verify_rules`. The re-cut also simplified the records: `rule_names` returns names, one
`RuleSubject` replaces two variants, `adjust_prover_config` takes its two settings as fields, and
`declare_modules` writes only the gated `mod.rs` (`9ac302ae`, `615b80a1`).

The **publish gate** left the slice in the re-cut. `state.py`'s property-to-rule mapping,
`RuleSubject`, `validate_property_rules`, `validate_rule_subjects`, `check_cvlr_completion`,
`HarnessAssumptions`, `CVLR_JUDGE_KEY` and `CvlrGenerationInput`, and `GeneratedHarness`'s published
fields (the skip list and the job link), are read only by `author.py`, and ride **P6**, with the `AppliedEdit` they name. The
`CvlrGeneration` and `CvlrJudge` cache markers ride **P5**, whose `editor.py` imports them first.

The **anchor-reach probe** (`tests/test_cvlr_anchor_reach.py` and `tests/data/anchor_reach_probe.rs`,
+260) left the slice too. It is an expensive instrument for one question, whether a rule can reach
an Anchor handler's own code, rather than a test of anything P3b adds. It stays on the branch and
may never go to master.

#### P3b1 — Submit a generated harness draft

**Merged 2026-10-23 as [#267](https://github.com/Certora/AutoProver/pull/267), `9068e362`:** 27
files, +847 −69 code and +616 −60 tests, planned at +750 and +503.

Master submits the CVLR program already in the crate (P2). A generated harness arrives as a draft,
so P3b1 declares each unit's module behind its own cargo feature (`harness.py`), writes the draft
into the shared tree (P3a1), builds it, reads the rules the build lists from the `.so`'s
`.cvlr_rules` section (`cvlr/manifest.py`), and submits those. The draft stamps when every one of them
verified.

Its usage is `verify_rules`, driven through `gate_tools` by `tests/test_cvlr_verify.py`. What that
tool does not reach waits for the slice whose code does, under rule 5:

* `verify.py`: `BuildPermit`, `HarnessTarget.stage`, `CargoCheck` and `VerifyRules`, with the prover
  accounting (`_RunAccounting`). `VerifyRules` always runs `UnanalyzedCexHandler`. Counterexample
  analysis rides **P6** (`AuthorModel`, `CexAnalysis`, the store capture), and the record of
  incomplete checks rides **P3b2b**, whose report first reads it. `_stage_and_submit` takes a
  required `choose`, which sees the section's names and returns the submission to write;
  `verify_rules` and P3b3's rerun each pass their own. A build that lists no rules is refused
  rather than submitted. On the branch, a draft with no rules stamps when every property is skipped
  (`VerifyRules._nothing_to_submit`); skips are the author's, so that path rides **P6**.
* `cvlr/manifest.py`: `rule_names_in_elf`, with `pyelftools`, a new dependency.
* `harness.py`: `HarnessModule`, and `CvlrArtifactStore` with `declare_modules`. `GeneratedHarness`
  carries only what the store writes; its published fields ride **P6**, and the store's
  `_job_info_payload` rides **P7**, whose `entry.py` writes `job_info.json`.
* `state.py`: only what the gate reads, `PROVER_VALIDATION_KEY` and the generation state.
  `LastVerdicts` rides **P3b2b**.
* `composer/prover/core.py`: `UnanalyzedCexHandler`.
* Tests: `tests/test_cvlr_verify.py` and `tests/test_cvlr_manifest.py`, new in the PR; the
  feature-gating case in `tests/test_cvlr_tree.py`; the accounting case in
  `tests/test_cvlr_plumbing.py`.

**What review changed.** The run accounting left `verify.py`: `composer/prover/accounting.py` is
shared by CVL and CVLR, and takes the build permit when it is constructed. The per-unit prover lock
is gone; staging a draft goes through the build permit, and the build steps follow staging in one
session. The gate submits with no counterexample handler, so `UnanalyzedCexHandler` did not
land: `verify_rules` reports a violated rule itself, and its report leaves the counterexample out.
Counterexample analysis still rides **P6**. Unit features are prefixed `certora_unit_`, the conf
directory is `submission_dir`, the deliverables stay out of the working tree, and the unit
submission helpers (`submission_for`) are `VerifyDeps` methods. `rule_names_in_elf` refuses a
second `.cvlr_rules` section and leaves any other unreadable file to pyelftools: the ELF64 and
`SHT_NOBITS` checks went in review, since rustc produces neither.

**A follow-up the review asked for: choosing which rules to run.** `verify_rules` always submits
every rule the build lists, and the reviewer prefers that it take a selection. Postponed; see
[cvlr-todo.md](./cvlr-todo.md) U34, which also says what a selection needs from the stamp and the
conf.

**What P3b1 carries from #258's review.** `_stage_and_submit` (`cvlr/verify.py`) implements it,
tested by `tests/test_cvlr_verify.py`. Two submissions of one crate share its target
directory, so they also share the `.so`, and the Prover's local phase rebuilds and uploads it. A
sibling's build with other features could replace the `.so` before this job uploads it. #258
settled on one shared target directory; one directory per submission was tried and backed out, at a
cold dependency build and about 1 GB of disk per unit. So the permit is held from staging until
`on_prover_link` fires, or until `run_submission` returns if the CLI fails before uploading.
`prepare_submission`'s docstring states this contract. That serializes each submission's local
phase, a warm program-crate rebuild plus the upload, but not the cloud wait. P3b3's unsat-core run
goes through the same `_stage_and_submit`, so it takes the same permit.

#### P3b2 — A verdict depends on more than the draft, and what it means

Planned as one PR, +1,062 code and +577 tests, and opened as #268. Rebased onto master after #267 it
was about +1,180 code and +800 tests, and on 2026-10-23 it was split along a line the state already
drew: the inputs a verdict depends on, and how a verdict that is not `VERIFIED` is read. Each half's
code is reached by its own `verify_rules`.

##### P3b2a — A verdict depends on more than the draft

**Merged 2026-10-27 as [#268](https://github.com/Certora/AutoProver/pull/268), `70f5850a`:** 13
files, +661 −46 code and +562 −14 tests, planned at +668 and +520.

A summary, an edit to the program, or a prover setting changes what a green rule means, and none of
them is in the harness source. P3b2a stages them with the draft and puts them in the stamp
(`tuning_history`), so a change to any of them drops a stamp earned before it. The stamp's
predicate is still P3b1's: every listed rule verified.

* `author_tools.py` (moved out of `verify.py` on the branch, `487232d2`): `summarize_for_prover` and
  `adjust_prover_config`, which `gate_tools` binds next to `verify_rules`.
* `tuning.py`'s summary half (`SummaryDirective`, `TuningFiles`) and `conf.py`'s `OptimisticLoop`.
* `state.py`: summaries, `vfs`, `prover_settings`, `VersionedHistory`, and `tuning_history`.
* `verify.py`: `BuildPermit.stage` writes the summaries file and lays the unit's edits over the tree
  with the draft; `VerifyDeps.submission_for` takes the settings from state; the report names
  summary directives that matched nothing.
* `composer/cargo/symbols.py`: summary directives that match no symbol in the build. It reads symbols
  with `pyelftools`, which P3b1 added, and demangles with `rustfilt`, as the Prover does. `rustfilt`
  is a prerequisite of a run, listed in `README-solana.md`; master's `README.md` does not mention it.
* Tests: `tests/test_cvlr_tuning.py`, `tests/test_cvlr_symbols.py`,
  `tests/test_cvlr_prover_settings.py` (its cases came out of `tests/test_cvlr_author.py`, P7), the
  conf cases in `tests/test_cvlr_plumbing.py`, and additions to `tests/test_cvlr_verify.py`.

**What review changed.**

* **Prover settings.** `adjust_prover_config` writes a `SettingsEdit` holding only the fields it
  set, and the `prover_settings` reducer (`merge_settings`) applies it, so two calls in one graph
  step that set different fields both land (`a878f3a6`). Every `loop_iter` change needs a `why`,
  as does turning `optimistic_loop` on; turning it off alone does not (`2057128c`, `8c7dd422`). The
  tool's text says that under `optimistic_loop` the bound is assumed rather than checked, so it is
  part of what a verdict means.
* **`optimistic_loop` is a plain bool** (`8a6adfd4`). The `why` is required but not stored, because
  nothing on #268 read it back. The branch keeps `OptimisticLoop(why=…)`: the judge briefing
  (`HarnessAssumptions`, in `state.py`) reads the reason. The stored reason rides **P6** with that
  briefing.
* **No per-unit summaries file** (`8a6adfd4`). The unit's directives are rendered from state straight
  into the composite file the conf names.
* **Reading symbols** (`cb58abdb`, `16af7948`). `prover_function_names` counts undefined call
  targets as well as defined functions, as the Prover numbers both. `demangle` runs `rustfilt` as an
  async subprocess. A failed or short `rustfilt` run raises `RustfiltFailed` or `RustfiltMismatch`,
  and an unreadable symbol table is said in the `verify_rules` result rather than read as every
  directive matching. `nearest` ranks suggestions by `difflib` similarity to the pattern.
* **The inert-summaries note** is a typed template (`_InertSummaries`, `5ef4c53f`), so
  `template_manifest.json` gained its entry.

The commits cited are #268's own, squashed in the merge. `eric/solana-staging` took all of it back
(`70b009a7`, `c5fe4241`, `98c681ee`, and the merge of master, `5e2f7470`) except the plain bool, and
`feature/solana` has it through that merge.

##### P3b2b — What a verdict means

+514 code, +279 tests. Branch `eric/cvlr-verdict-meaning`,
[#280](https://github.com/Certora/AutoProver/pull/280), stacked on P3b2a and still to be rebased
onto master now that #268 has merged.

A rule that did not verify may expose a real defect, may have stopped on an assertion the Prover
generated, or may have no verdict at all. P3b2b tells those apart and stamps when every listed rule
is accounted for; a run that is not withdraws a stamp of the same draft.

* `author_tools.py`: the expected-failure markings, which re-decide the stamp against the last run
  (`unaccounted`, `stamp_under`).
* `state.py`: `LastVerdicts`, `expected_failures`, `external_functions`.
* `verify.py`'s report: incomplete checks, external functions, rules with no verdict, expected
  failures that verified, and the withdrawn stamp; its notes are templates in `composer/templates/cvlr/`.
* `composer/prover/core.py`: `ProverReport.incomplete` (from `classify_violation`) and
  `ProverReport.external_functions`.
* `composer/prover/ptypes.py`: `LoopUnwound` and `OverflowUnproven` (`c40ee1a7`), with
  `tests/test_solana_cex_trace.py`'s update. That commit's prompt change rides **P6**; its
  `tests/test_cvlr_findings.py` change rides **P7**, because that test imports `cvlr.pipeline`.
* The external-functions hunks in `results.py` and `cloud.py`, with
  `tests/test_prover_external_functions.py`.
* Tests: `tests/test_cvlr_restamp.py`, and additions to `tests/test_cvlr_verify.py`.

#### P3b3 — Why a rule passed vacuously

`composer/spec/cvlr/vacuity.py` and its two prompts, and `explain_vacuity`, the tool in `verify.py`
that uses it: `_DiagnosticAccounting`, `VacuityDeps`, `ExplainVacuity`, and its entry in
`gate_tools`.

On Solana the Prover writes no unsat core for the vacuity check itself. So the tool reruns the one
rule with the check off and cores on, under which a vacuous rule verifies, and reads the core of that
proof. Whether the core contains the rule's own assertion is decided without an agent, and it decides
what the author should do next; an analyst then reads the core against the harness and the program.

Everything it needs on the way is its alone:

* The submission purpose in `conf.py`, `CheckVerdicts | CollectUnsatCore`, with
  `RunOverlay.purpose`, `Submission.purpose` in `prover.py`, and the case in `solana_conf` that turns
  `rule_sanity` off and `coverage_info` on. None of it is on master, so P3b1 has no single-variant
  union waiting for its second variant.
* The `_RunAccounting._record_link` hook, which `_DiagnosticAccounting` overrides so a rerun's link
  is not recorded as the task's. P3b1 inlines it, having no override.
* The rerun's own `choose` for `_stage_and_submit`: it refuses a rule the build's section does not
  list, and returns the one-rule unsat-core submission. New work: the branch predates the section
  and checks the rule against `rule_names(draft)`.
* `fetch_unsat_cores` and `job_input` in `report_prover.py`. `explain_vacuity` is `job_input`'s only
  caller (see S4 in wave 1).
* `results_api`, the public name for `cloud.py`'s `_results_api`. `verify` is the first reader
  outside `cloud.py`.
* The two unsat-core template fragments, split out of `sanity_tool_prompt.j2` so CVL's sanity
  analysis and this one share them.

Nothing in P3b1 or P3b2 imports `vacuity`; its other importer is `author.py` (P6). `tests/test_cvlr_vacuity.py`
stubs the submission, the results API and the analyst, so it needs no toolchain, network or model.

### K1 — A knowledge bundle, its second instance, and the CVLR recipes

The old **S6**, **K1** and **K2** as one PR: `KnowledgeBundle`, a `KBRecipe` generic over its
channel vocabulary, `@cache`s keyed by bundle, `kb_tools(bundle)`, a `kb_index.j2` that does not
name CVL in prose a second corpus would inherit — *and* `CVLR_BUNDLE`, `CVLR_FACTS_BUNDLE`,
`with_cvlr_context`, `with_cvlr_facts`, `composer/kb/resources/cvlr_baseline_facts.md` (the ~400
lines of run-invariant CVLR and prover fact factored out of the author's system prompt), and the
CVLR recipes: `cvlr_recipes_index.yaml` and its three bodies, K1 to K3.

**The recipes are here because the bundle cannot be built without them.** `CVLR_BUNDLE` loads
`cvlr_recipes_index.yaml`, and `test_cvlr_bundle.py` checks that every recipe it indexes exists.
K2 was planned as content to land last, once the capture triage had settled what survived. The
triage is done: two of 27 candidate entries survived, K1 and K2. K3, sysvars read directly, came
from the stake benchmark. What is left to add is whatever a run shows the author is missing, which
needs no slice of its own.

**This is the one PR that lands ahead of its reader on purpose, and the reason is the criterion
itself.** S6 alone is a refactor with one instance, which is the shape the feedback is about: a
reviewer is asked to judge a generalization against a future second user. S6 *with* `CVLR_BUNDLE` is
the generalization and its second instance in the same diff — `test_kb_bundle.py` renders a bundle
that is not CVL's, using the API the way production will. Its first reader in production code is
P5's munge reviewer, through `with_cvlr_facts`; its first test reader is P4's worked-example test,
which renders the bundle beside the example. That test is why K1 now lands ahead of P4.

Splitting it the other way — bundle to master, CVLR content later — is rule 4's instinct and it is
wrong here: it produces exactly the caller-less abstraction this re-partition exists to stop.
Keeping them together also keeps the two review audiences in one diff, where the CVL owners can see
what their eight agents' context mechanism is being generalized *for*.

### P4 — The Anchor surface and the worked example

`composer/spec/cvlr/{anchor_surface,example,guidance,rust_source}.py`.

`rust_source` was planned for P3a1, as the Rust reading the munges were expressed over. With the
munges gone it is only the scanner primitives (`DERIVE_LIST`, `body_span`, `code_positions`), and
`anchor_surface` is its one caller, so it lands here.

`anchor_surface` reads the program; `example` renders the worked example against what it read;
`guidance` is the static prose that ships beside it. The old **C5a** put `anchor_surface` with the
prover and **C4b** put `example` two PRs later, which separated the analysis from its only reader.
`tests/test_cvlr_worked_example.py` renders the example against a real analyzed program, so the
usage is in the PR even though the agent that will be *shown* it is P6.

### C7b — The CVLR API corpus

`composer/scripts/cvlr_api_docs.py`, `composer/cargo/doctest.py` and `composer/tools/cvlr_api_rag.py`.
The first is the producer:
it runs `cargo rustdoc --output-format json` over the releases `reference.py` pins and writes a
manifest that `rag_import` ingests as `cvlr_api_kb`, with every compilable example in the crates'
doc comments compiled against the pin by the second. The third holds the three tools over that
corpus: `cvlr_api_search`, `cvlr_api_lookup` and `cvlr_api_surface`. The corpus registration comes
with them: a role and schema in `init-db.sql`, `CVLR_API_DEFAULT_CONNECTION`, and a
`KNOWLEDGE_BASES` entry. That entry is the one `rag_import` resolves a manifest's
`knowledge_base` through. `populate_cvlr_rag.sh` loses its manifest discovery, and generates and
ingests the API manifest beside the manual. `cvlr_rag.py`'s docstrings now say that `cvlr_kb` is
the manual, and point the question of whether an item exists at `cvlr_api_lookup`.

**It used to be wave 3, and it was held back for a reason that no longer applies.** The old C7b was
only the discovery seam: the manifest was produced in `certora-cvlr-kb`, and that producer was
expected to be reworked. It was reworked by moving it here, so the seam C7b would have landed has
been deleted, and the producer is now in this repo and in the test suite.

**What justifies it is the same thing that justified #244.** The producer, the ingest and the tools
together are a capability someone can run today: `populate_cvlr_rag.sh` builds the corpus, and the
tools answer from it. `test_cvlr_api_docs.py` drives the producer over trimmed payloads from a real
rustdoc run. The tool half of `test_cvlr_research.py` drives the tools against a corpus the
producer built. The agent that holds those tools arrives in P6, so this PR has to land before P6.
Nothing else orders it: its only imports from `composer/spec/cvlr/` are `crates.CVLR_PREFIX` and
`reference.reference_for`, and both are on master already. Like K1, it can move anywhere ahead of P6.

**The fixtures are most of its size.** `tests/data/cvlr_rustdoc/` is 19,751 lines of JSON in five
files. They are trimmed from a real run rather than written by hand, because a hand-written payload
would encode the author's belief about rustdoc's schema, and that belief is the thing most likely to
be wrong. A reviewer should read the test, not the fixtures. Decision 2 asks the same question about
P9's tape, at a smaller scale.

Its one `KNOWLEDGE_BASES` entry is `cvlr_api_kb`, for `rag_import`; nothing registers a search
factory for it ([decision 8](#decisions-to-make-before-starting)).

The `PROVENANCE` stamp now has a reader, so it rides here. `populate_cvlr_rag.sh` prints it, which
is what the deferral in *Dropped: naming the CVLR corpus by tag* was waiting for.

### P5 — The munge editor

`composer/spec/cvlr/editor.py`, `composer/cargo/depinfo.py`, the two prompt templates, and the
editing core lifted out of the EVM editor (`8105adc4`): `composer/spec/source/munge/{edit_tools,
edit_store,munge_agent,vfs_diff}.py`, with `composer/spec/source/{author,live_explorer}.py` and
`composer/spec/cvl_generation.py` moved onto it. Also the report's `SourceEditRecord`, one patch per
unit (`composer/spec/source/report/` hunks), tested by `tests/test_cvlr_source_edits.py`. None of
these is testable without the editor.

The lift changes the EVM editor, which keeps passing unchanged. It could go to master ahead of P5 on
its own, but it is then a refactor whose second caller is not in the diff.

`depinfo` — which sources a build actually compiled, from rustc's `.d` files — has exactly one
caller anywhere, and it is the editor: an edit to a file the `certora` feature gates out does not
reach the build, and the editor is what needs to know. In the old plan `depinfo` was in **R1** and
the editor was in **C3b**, wave 2 and wave 3.

First production reader of K1's bundle: the reviewer gets `with_cvlr_facts`, the bundle without
the recipe index, since it holds no tool to retrieve a recipe. The editor gets no bundle.

### P6 — The authoring loop

**It also takes two things P3b1 left out.** Counterexample analysis: `verify.py`'s `AuthorModel`,
`CexAnalysis` and `_CaptureCallbacks` (less the incomplete-check record, which P3b2b has), and
`VerifyDeps.analysis`. `author.py` binds the model, so a reviewer cannot judge it before then. And
`rules.py`, with `tests/test_cvlr_rules.py`: `author.py` and the publish gate in `state.py` read the
rules from the draft, while P3b1's gate reads them from the build. Whether the publish gate keeps
the source parse or takes the build's list is a decision for this PR.

`composer/spec/cvlr/author.py`, the four authoring prompt templates, and
`composer/spec/cvlr_research.py` with its two templates.

**The author's CVLR channel is the research sub-agent.** The draft of this section gave P6 the
crate-source mount, `crate_mount` and `source_tools`. Those were deleted on the branch in `5b2afeff`,
after a corpus-only run showed the corpus could replace them. That run is recorded in
[cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md) §8.1: 51 research calls, zero compile errors
in 2h47m. `cvlr_research` binds C7b's three API tools and `cvlr_kb`'s three manual tools, and is
told that the API corpus wins when the two disagree. It goes to the author and to the judge. Before
this change the judge had no CVLR knowledge channel at all, because it had the mount and no corpus.
The researcher lands here and not on its own: the author and the judge are its only callers, and
the prompt fragment that tells them about it, `cvlr_research.j2`, is included by their two system
prompts. The `cvlr_research` and `cvlr_api_*` entries in `tool_display.py` come with it.

**`cvlr_rag_tools.j2` should be deleted here, and that is new work.** #244 landed it for this PR's
prompt to include. That prompt now includes `cvlr_research.j2` instead, and the author no longer
holds the manual tools directly, so nothing would ever render it. The branch still carries it
unchanged, along with its case in `tests/test_cvlr_rag.py`, so the deletion has to be written. The
test's other half, that the three manual tools bind, stays.

**The corpus half has landed.** [#244](https://github.com/Certora/AutoProver/pull/244) merged on
2026-09-22 as `7d435150`, and it carried `cvlr_rag_tools.j2` despite the advice to hold the template
back for the prompt that includes it. That advice is moot now. The template is on master with no
renderer until this PR. #244 also landed the pieces this section used to list as P6 hunks:
`CVLR_DEFAULT_CONNECTION`, the `cvlr_rag` schema in `init-db.sql`, `ragbuild`'s `<blockquote>`
case, the manual half of `populate_cvlr_rag.sh`, and `tests/test_cvlr_rag.py`. #244 also added
two things the plan had not: `ragbuild --corpus {cvl,extended,cvlr}`, and one search-result
renderer, `composer/rag/render.py`, shared by every corpus. What remains on the branch in those
files is C7b's.

### P7 — The backend, the pipeline and the CLI

`composer/spec/cvlr/{pipeline,entry}.py` (the executor half), `composer/cli/console_solana.py`,
`composer/cli/tui_solana.py`, and the plumbing tests. The `CvlrJudge` / `CvlrGeneration` cache
markers were planned here; `editor.py` imports them first, so they ride P5.

Still the PR that makes the backend a product, and still last in the wave — but it is now the PR
that *composes* seven working things rather than the PR that makes twelve dead ones reachable. Lands
without the two pinned-run flags; see *Deferred: pinned runs*.

It now also carries three things that were meant to land earlier: the `cvlr-preflight` script (still to
write) and `entry.py`'s parser, both of which P1 merged without; `make_run_link_fetcher`, which S4 can no longer
provide; and the reference set's `withholding`, which `--withhold-crate` is the only caller of.

And it carries S4's give-up reason (`Abandoned`, `GaveUpComponent.reason`, `schema_version` 3.2).
That one is not CVLR-specific: it changes every backend's report, EVM included, and P7's
description has to say so rather than leave a reviewer to find it.

`entry.py` is also where the two corpora are opened for the researcher, through P6's
`cvlr_research.corpus_tools`, and where `--no-corpus` turns them off
([decision 8](#decisions-to-make-before-starting)).

### What the merge and later work carry

`eric/solanaProver` merged into `feature/solana` on 2026-10-15 (`a7e0251d`): SDK 3 support,
platform-tools v1.53, design-property extraction and the hoist. The alpha has added work since. Each
change rides the first unlanded PR whose capability it extends. A change to a file a merged PR owns
rides the first PR that reads it, or goes to master as a follow-up when master can exercise it
already. Changes inside unlanded slices' own files are not listed; they are part of those slices.

**One new slice: P1b, SDK 3 targets, a follow-up to master.** Point preflight at a workspace on the
split `solana-*` 3.x crates and it picks that platform generation, scaffolds `cvlr-solana` 0.6.0
for it (0.5.1 for `solana-program` 2.x), and builds with platform-tools v1.53 under one host
toolchain rather than the target's `rust-toolchain.toml`. Its files are P1's and P2's, all merged,
and master's tests reach them through `preflight.select`, the scaffold and the end-to-end test, so
it is a follow-up rather than a slice of its own capability. It goes ahead of P3b: every later
slice is cut from a branch that has it, and their tests assume it. It carries one pin its
description must name: `prover_version: master` until the Prover releases the sound-math fixes
([cvlr-todo.md](./cvlr-todo.md) U25).

| Change | Files | Rides |
|---|---|---|
| Two platform generations, picked per target (`a1a3bfba`, `f17fc6f1`, `5332f039`, `8d99f006`) | `reference.py`, `scaffold.py`, `preflight.py`, `crates.py`, `env_paths.py`, their tests, `tests/data/stake_sdk3_sbf_symbols.txt` | **P1b**. The `entry.py` and `pipeline.py` hunks ride P7, and the per-generation API corpus (`bf0406ac`, `ae33e379`) rides C7b |
| Platform-tools v1.53, one host toolchain, and the tools found at `$CERTORA_PLATFORM_TOOLS_ROOT` (`f5179ae0`, `2f2c8ecc`, `6e6c80c6`, `fd0f653d`) | `conf.py`, `composer/cargo/{metadata,sbf,sbf_build_script,session}.py`, `composer/sandbox/{config,recipes}.py`, `tests/test_sandbox_config.py`, `tests/test_cvlr_end_to_end.py` | **P1b**. The `Dockerfile.solana` hunks ride R3 |
| Top-bit `u64` constants modelled soundly, on Prover master (`91f4261d`) | `conf.py`, `tests/test_cvlr_plumbing.py` *(hunk)* | **P1b** |
| Preflight's cargo graph fetched into a home the run can write (`54dcfb49`) | `metadata.py`, `preflight.py`, their tests | **P1b**; the `entry.py` and `pipeline.py` hunks ride P7 |
| SDK 3 prompt and recipe text (`8477d18d`), rejection rules taught as their contrapositive (`57e33e67`) | the editor's, author's, judge's and vacuity prompts | **P5**, **P6** and **P3b**, by prompt |
| Recipe K3, sysvars read directly (`d129bacf`) | `composer/kb/resources/recipes/cvlr-k3-sysvars-read-directly.md`, `cvlr_recipes_index.yaml`, `tests/test_cvlr_knowledge.py` *(hunk)* | **K1** |
| Design-property extraction: components carry `computations`, and the analysis and extraction prompts lead with the design (`9385d5a4`) | `composer/spec/solana/model.py`, `composer/templates/solana/*.j2`, `composer/pipeline/ecosystem.py` *(hunk)*, `tests/test_solana_components.py` | **P7**, with the hoist, whose shared unit carries the computations. Nothing on master runs the Solana analysis outside its tests. `guidance.py`'s hunk rides P4 |
| The hoist: protocol-wide properties formalized once, in a shared unit (`47430a55`, `8dbd78a4`), and the report saying what each stands for (schema 3.4, `2c423dec`) | `composer/spec/hoist.py`, `composer/templates/property_hoisting_{prompt,system}.j2`, `composer/pipeline/{core,keys}.py` *(hunks)*, the `shared_unit` lines in `composer/foundry/pipeline.py`, `composer/rustapp/adapter.py`, `composer/spec/source/pipeline.py` and both null backends, report hunks, `tests/test_hoist.py`, `tests/test_pipeline_{staged_formalizer,overlap}.py` *(hunks)* | **P7**. CVLR is the seam's only user; every other backend's `shared_unit` is `None`, so EVM and Crucible are unchanged. P7's description says so, as it does for the give-up reason |
| A corpus is rebuilt, not ingested on top of (`e669ec81`, `519a6998`) | `composer/rag/wipe.py`, `composer/scripts/{ragbuild,rag_import}.py` *(hunks)*, `scripts/wipe_rag.py`, `tests/test_rag_wipe.py`, `scripts/autoprove-entrypoint.sh` *(hunk: `setup-db`)* | **master now** ([Miscellaneous](#miscellaneous-shared-changes-master-could-take)). The `populate_cvlr_rag.sh` hunk rides C7b |
| A failed cloud job's alert errors reach the author (`26f7b796`, `fed133f7`) | `composer/prover/{cloud,core,results}.py` *(hunks)*, `tests/test_alert_errors.py`, `tests/test_cloud_results_fetch.py` *(hunk)* | **master now**, with the link row in Miscellaneous. It brings `ALERT_REPORT` and the read with it, which P3b would otherwise carry |
| "Cannot overflow" stops, and a halted rule isolated before its bound is raised (`c40ee1a7`, `0accd84d`, `557cd826`) | `verify.py`, `composer/prover/ptypes.py` *(hunk)*, `tests/test_solana_cex_trace.py` *(hunk)*, the author's prompts | **P3b** for `verify` and `ptypes`, **P6** for the prompts |
| `llvm-readelf` in either platform-tools flavour (`c7519899`) | `composer/cargo/{symbols,sbf}.py`, `tests/test_cvlr_symbols.py` | **P3b** |
| A dangling symlink in the project no longer ends the run (U19, `21b1a827`) | `tree.py`, `tests/test_cvlr_tree.py` *(hunk)* | **master now**, with the P3a1 follow-up |
| The research answer cache keyed by the run's CVLR pins (`a00cbae0`) | `cvlr_research.py`, `tests/test_cvlr_research.py` *(hunk)* | **P6**; the `entry.py` hunk rides P7 |
| A lane per tool-started sub-agent in the tape, a resumable recorder, and the vault tape re-recorded with the corpus bound (`eb832026`, `d729f4af`, `5e856e39`) | `composer/io/context.py`, `composer/testing/{harness_tape,record_tape,record_cvlr_tape}.py`, `composer/testing/ui_harness_cvlr_vault.py`, `tests/test_tape_subagent_lanes.py`, `.claude/skills/generate-tape/SKILL.md` | **P9**. The lane hunks are shared code, and the CVLR tape is the first that needs them |
| A run whose only output is curtailed partials is not reported as failed (U21, `3dae956b`); the report says why the properties fell back to one group (U8, `fffe1b42`); a malformed finding draft is asked for again (`7e0bcbbb`) | `composer/pipeline/ptypes.py`, `composer/cli/console_{autoprove,foundry}.py`, `composer/scripts/budget_math.py`, `tests/test_pipeline_result.py`, `composer/spec/source/report/{build,findings,grouping,render,schema}.py` *(hunks)*, the report template, `tests/test_autoprove_report.py` *(hunks)* | **master now**: every backend's report and exit status. The `console_solana.py` hunk rides P7 |
| The deliverable: `harness.patch`, one patch per component, a curtailed unit's edits, and one Prover conf per delivered unit (`57789d6c`, `6b03e4bc`, `03df1ee6`, `c44e22bf`) | `composer/spec/cvlr/patch.py`, `tests/test_cvlr_patch.py`, `pipeline.py`, `composer/pipeline/core.py` *(hunks: the formalizer hooks)*, report hunks (`CurtailedComponent.source_edits`, `prover_confs`), and hunks in `harness.py`, `conf.py` (`delivered_conf`), `author.py` and `vfs_diff.py` | **P7**, which reads every hunk. The hunks in files earlier slices own are appended when P7 lands |
| The Solana toolchain installed by `composer.scripts.solana_toolchain` for every image, and `verify-solana-image.sh` (`88cbe9ad`, `723f8dc0`) | `composer/scripts/solana_toolchain.py`, `scripts/Dockerfile.solana`, `scripts/verify-solana-image.sh`, `tests/test_cvlr_image.py`, the `sbf.py` hunk (`SBF_SUBCOMMAND_VERSION`) and the `cvlr_api_docs.py` hunk (`PINNED_NIGHTLY`) | **R3** |
| The alpha's README | `README-solana.md` | **P7**, the command it documents |

The report's `schema_version` went from 3.1 to 3.7 on the branch, one step per change. Each PR that
changes the schema bumps it once, from whatever master has.

### Order, and what is forced

**~~P1~~ → ~~P2~~ → ~~P3a1~~ → ~~P3b1~~ → ~~P3b2a~~ → P3b2b → P1b → P3b3 → K1 → P4 → C7b → P5 → P6 → P7**

P3b3 has slack too: it needs only P3b2b, and only P6 needs it, so it can land anywhere between them.

K1, P4 and C7b are the three with slack. K1 needs nothing in `composer/spec/cvlr/` at all, and C7b
needs only what P1 merged, so either could go first today. P4 carries `rust_source` itself and needs K1,
whose bundle its worked-example test renders. C7b has to be ahead of P6, which reads it.
Everything else is the DAG.

Checked rather than assumed: the only modules outside `composer/spec/cvlr/` that import from it are
the two Solana CLI entry points, the tape driver and the env refresher — all of them CVLR-specific
themselves. No shared module reaches into the backend.

**What the re-partition costs.** Four of these — P1, P3a and P3b (before they were split), and P6 — are larger than any PR the
old wave 3 had except C6, and P1 and P6 are larger than C6, which was that wave's biggest. That is
the trade, and it should be made with the number in view: the old wave 3 was 12 PRs averaging 1,900
lines, not one of which a reviewer could run; this is 9 averaging 2,800, each with an exit criterion
in it. If a reviewer wants the smaller unit back, P1 is the place to ask — `scaffold.py` and its env
files are 1,430 of it and could go alone, at the price of landing the scaffold one PR ahead of the
`preflight` that is its only caller, which is the trade this whole re-cut exists to refuse.

**What it does not fix.** `composer/spec/cvlr/` is still eleven layers deep, and a slice still has
to be a *prefix* of that DAG. So P3a1's `tree` and P4's `example` still land before the production
code that calls them, and their justification is still a test rather than a caller. The claim this
partition makes is narrower than "everything has a caller": it is that **no PR asks a reviewer to
judge a module with nothing in the diff that uses it**, which is a thing the old partition asked
four times over.

---

## Wave 3 — gates, the container, documentation

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **P8** The end-to-end gate and its scenario | 9 | +2,930 | `test_cvlr_gate.py` and the `solana_vault_idl` Anchor program it runs against. Real models, real cargo, real cloud jobs. **Still owes the corpus flip.** The gate passes `rag_tools=()`, and its comment calls a corpus-backed run "the comparison, not the baseline". With the mount gone, that floor is an author with no CVLR reference at all, which nobody ships ([cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md) §8.2). |
| **P9** The replay tape | 11 | +25,860 | The recorded run that lets the gate's shape be re-checked for the price of the builds and prover jobs alone. 24,753 of those lines are one generated file. Re-recorded on 2026-10-16 (`5e856e39`) with both corpora bound, after each tool-started sub-agent, the researcher included, was given a lane of its own (`eb832026`): 454 entries, from a run that delivered 4 of 4 units and carried the hoist and per-unit program edits. The replay passes. The lane change is in shared tape code (`composer/io/context.py`, `harness_tape.py`, `record_tape.py`) and rides here, because this tape is the first that needs it. |
| **R3** A Solana container | 8 | +400 −60 | `scripts/Dockerfile.solana` and `scripts/docker-compose.solana.yml`: a second AutoProver container, `autoprove-solana`, carrying the Rust toolchain and the platform-tools release, layered on the base image — which stays lean and loses nothing but its docs stage, where the one-element `for name in cvl` loop becomes `cvl solana` so the shared postgres gets the manual the backend is for. The shared service body moves to `scripts/docker-compose.common.yml`, which the EVM and Solana services both extend. The Solana image also generates the `cvlr_api_kb` manifest at build time, where it has a toolchain and the network, and `setup-db` ingests it beside the manual. The certora-cvlr-kb manifest discovery `setup-db` used to do is gone. The toolchain is installed by `composer.scripts.solana_toolchain`, at the versions the code builds with, and `scripts/verify-solana-image.sh` checks a built image: the toolchain, the entrypoint's guards and one Prover job, with no LLM. |
| **D** Documentation | ~6 | ≈ +3,600 | What is left after each slice carries its own section: the upstream-defect record, the knowledge plan, the API-documentation design note, this document, and the to-do index. The capture plan and the knowledge-bundle plan are not in it: both described the retired certora-cvlr-kb pipeline or a slicing this plan replaced, and were deleted from the branch on 2026-10-13. |

**R3 still goes after P7.** `tests/test_cvlr_image.py` imports `composer.spec.cvlr.conf` to check that
the image bakes `PLATFORM_TOOLS_VERSION`, which makes P1 the hard floor.
But the container exists to run `console-solana`, which is not a console script until P7, and nothing
builds the image in CI — so landing it earlier would ship an entrypoint guarding a command that does
not exist and would not even buy a build-breakage signal in exchange.

The container layout is the one `eric/crucible-app` already ships: one lean base image, and a
sibling image per toolchain layered on it with `FROM ${BASE_IMAGE}`. Crucible, Solana and a future
Soroban are then peers rather than flags on a single image, and none of them obliges an EVM run to
carry a toolchain it never invokes. The two branches disagreed on where the toolchain goes — under
`$HOME`, which the base image makes world-writable, or under `$AUTOPROVE_HOME`, root-owned and
world-readable. Solana's answer is the one to follow: a read-only Landlock grant over a tree the
confined build can also write grants nothing.

**Documentation moves with its subject.** The old **D** was 8,153 lines in one PR — the single worst
offender against the criterion this plan now applies, since a document describing code is exactly a
thing that cannot be evaluated apart from it. `docs/cvlr-backend-plan.md` splits along its own
section boundaries and each part rides the slice it describes; `who-edits-the-program.md`, which
now also holds the working-tree design (§14–§16), goes with P5;
`application-abstraction.md`, `ecosystem-abstraction.md` and `formalization-abstraction.md` with
P7. What is left in **D** is the material that describes the work rather than the code.

---

## Miscellaneous: shared changes master could take

These are changes on the branch that touch shared code and that no CVLR slice needs in order to
work. Each was found while building the backend. None mentions Solana except in its motivation,
and each would read as an ordinary fix to someone who has never seen the branch. By rule 4 they
belong on master, each as its own small PR. They are listed here so that they are not forgotten,
and so that a CVLR PR does not quietly carry one.

The list is of things to *consider*, not a queue. Some of these change EVM behaviour, and whether
master wants that is its owners' call.

| Change | Files | What it fixes | Notes |
|---|---|---|---|
| A statically decided rule keeps its verdict (`3a2c523f`) | `composer/prover/results.py` *(hunk: `flatten_tree_view`)*, `tests/test_tree_view_static_verdict.py` | A rule the Prover decides by static analysis arrives in the tree view as a bare root with no children. The parser recursed into the children, found none, and dropped the rule from the results. | The most valuable entry: a verdict silently disappears, on any chain. The test builds the node with `trace_shape("solana")`; for master it should use the EVM shape, or both. |
| A cloud results download that still fails after retrying is reported, not raised (`3087e754`) | `composer/prover/cloud.py`, `composer/prover/core.py`, `tests/test_cloud_fetch_retry.py`, `tests/test_cloud_results_fetch.py` | Once #223's retries are used up, the error escaped `run_prover` and ended the whole formalization, although the job had succeeded. It is now `CloudResultsUnavailable`, returned to the agent with the job link. | The original commit also lengthened the backoff to 10s/30s/60s, after a failure that recovered "minutes later". The port kept #223's 2s/4s. Whether master wants the longer schedule is a separate question for #223's owners. |
| A cloud job failure names its link and its alert errors (`26f7b796`, `fed133f7`) | `composer/prover/core.py` *(hunk: the `CloudJobError` message)*, `composer/prover/{cloud,results}.py` *(hunks: `ALERT_REPORT`, `error_alerts`)*, `tests/test_alert_errors.py` | A job that fails server-side leaves nothing on disk, so the status alone tells the agent nothing it can act on. An authoring agent then blames its own draft and simplifies it toward a tautology, or resubmits a draft that crashes the Prover. The `ERROR` entries of `Reports/alertReport.json` now reach it, with what to do about an out-of-resources stop. | Visible on EVM runs. Brings the alert-report read that P3b would otherwise carry. |
| A counterexample analysis claims only what the counterexample shows (`bcfeaaaa`) | `composer/templates/cex_instructions.j2` | The single-shot analyzer called two sibling rules passing controls, inferring their verdict from the absence of a counterexample. Both were SANITY_FAILED. | A CVL prompt change, so it changes EVM runs' counterexample analysis. Its motivation was CVLR, but nothing in it is. |
| A relative main-contract path resolves against the project root | `composer/pipeline/cli.py` *(hunk)* | Running the CLI from outside the project failed outright. | S1 was merged without it. Both readings agree wherever the old one worked. |
| `cli_pipeline` reaches `get_provider_for` through its module | `composer/pipeline/cli.py` *(hunk)* | Both the fake-LLM tape and the tape recorder install themselves by replacing `composer.llm.registry.get_provider_for`. A name bound at import time keeps the original. | **Check master first.** Master's `harness_tape.py` and `record_tape.py` patch that attribute, while master's `cli.py` still binds it at import. If a taped test goes through `cli_pipeline`, it may be calling a real, paid model. P9 needs this regardless. |
| A corpus is rebuilt, not ingested on top of (`e669ec81`, `519a6998`) | `composer/rag/wipe.py`, `composer/scripts/{ragbuild,rag_import}.py` *(hunks)*, `scripts/wipe_rag.py`, `tests/test_rag_wipe.py`, `scripts/autoprove-entrypoint.sh` *(hunk: `setup-db`)* | Ingest only inserts. Re-running it added a second copy of every row, or, for the CVL manual, died on a unique constraint part-way; `wipe_rag.py` could not empty the CVLR schemas. `--replace` empties each target first, and `setup-db` passes it. | Every corpus, CVL included. |
| A run whose only output is curtailed partials is not reported as failed (U21, `3dae956b`) | `composer/pipeline/ptypes.py`, `composer/cli/console_{autoprove,foundry}.py`, `composer/scripts/budget_math.py`, `tests/test_pipeline_result.py` | `all_failed` counted only deliveries, so a run the budget cut short after every component had published a partial printed "RUN FAILED" and exited 1. AIAutoProver reports the exit code as the job status. | Any backend's exit status. The `console_solana.py` hunk rides P7. |
| The report says why the properties fell back to one group (U8, `fffe1b42`), and asks again for a malformed finding draft (`7e0bcbbb`) | `composer/spec/source/report/{build,findings,grouping,render,schema}.py` *(hunks)*, `composer/templates/autoprove_report.html.j2` *(hunks)*, `tests/test_autoprove_report.py` *(hunks)* | A fallback grouping was reported without its reason, and a finding draft that failed structured output was dropped, so a confirmed violation could vanish from the findings. | Report schema changes; visible on EVM runs. |
| `OVERLAY_OWNED_KEYS` names the right registry | `composer/spec/source/prover.py` *(docstring)* | It says `author.EDITABLE_FLAGS`; the registry has been `author._FLAG_KEYS` for some time. | From the former S5. |
| Two ignored callback parameters typed `object` | `composer/authoring/judge.py`, `composer/spec/cvl_research.py` | `Any` where nothing reads the value. | Annotation only. Could ride with the docstring fix. |

Changes that look shared but are not on this list, because a CVLR slice is their first reader:
`prover/results.py`'s `external_functions` and the alert-report fetch in `cloud.py` (read by
`verify` and carried in the judge's briefing by `state`, **P3b2b**); `UnanalyzedCexHandler`
(`verify_rules` without a counterexample analysis, **P3b1**); the shared unsat-core template
fragments split out of `sanity_tool_prompt.j2` (the vacuity analysis, **P3b3**); and
`make_run_link_fetcher` and the give-up reason (**P7**).

---

## Deferred: the starting tuning directives

The starting inlining and summary files (`composer/spec/cvlr/envs/`) landed with P1. The branch
has since changed them four times: Anchor's account validation, Anchor's
`system_program::transfer`, and `AccountInfo::try_borrow_lamports` are now inlined, and
`AccountInfo::realloc` is inlined instead of summarized. More changes are certain to follow,
because each real target the branch is pointed at turns up another call the Prover's model handles
badly.

**They are held on the branch and submitted together, in one PR, at a time of our choosing.** No
slice needs them. A target is verified with whatever starting directives master has, and a missing
directive costs a precise verdict, not a working backend. Submitting them one PR per discovery
would give a reviewer a stream of one-line regex changes, each with its own story. One PR that
covers the lot can show the measurement for all of them together.

That measurement exists for two of the four so far. `tests/test_cvlr_dropped_writes.py`, with
`tests/data/dropped_writes_probe.rs`, scaffolds a probe program, so it reads the starting files.
It submits canary rules that verify only if the model drops a write the real program makes. That
covers `realloc` and `system_program::transfer`. It is the PR's usage, and it travels with it,
together with `rules.py` if P6 has not landed it by then, since the test reads `rule_names`. The
other two directives have no test on the branch, only their commit messages (`efd870b5`,
`e070d019`). Each wants a canary before it is submitted, or the PR description has to argue for it
instead.

While they are held, `tests/test_cvlr_env_paths.py`'s directive counts on the branch differ from
master's. They travel with the directives.

## Deferred: sound signed math

Without `-solanaTACSoundSignedMath`, the Prover models a `u64` compared against a constant at or
above `2^63` as a comparison against a negative number. So `x == u64::MAX` is unsatisfiable: a rule
that assumes the sentinel is vacuous, and a program branch on it is never taken
(`docs/upstream-defects.md` P11). The flag fixes that, but the Prover fixes that let it coexist with
`-solanaTACMathInt` are on its master branch and not all released. So the branch's `BASE_CONF`
turns the flag on *and* pins `prover_version: master` (`91f4261d`).

**Both are held on the branch until the Prover releases those fixes.** Then the flag and its
plumbing test go to master as a one-line follow-up, with no pin.

* **Not the pin to master.** It would run master's Solana end-to-end gate against an unreleased
  Prover. And the guard that keeps the pin visible cannot go with it.
  `test_the_master_prover_pin_is_still_tracked_as_a_ship_blocker` asserts the pin and its
  `cvlr-todo.md` item (U25) together, so whoever removes the pin also closes the item, and
  `cvlr-todo.md` is on the branch only. The guard is deleted with the pin, so it never goes to master.
* **Not the flag alone.** Without the unreleased fixes it does not coexist with
  `-solanaTACMathInt`, which every Solana conf turns on.
* **Nothing on master is a product yet.** Master's submission path reads `BASE_CONF`, but nothing
  ships it until P7. The defect costs master nothing in the meantime.

**P7 must not merge with the pin.** If the release has not happened by then, P7 waits for it, or
lands without the flag and the branch keeps it.

## Deferred: pinned runs

`composer/pipeline/pinned.py` writes a run's analysis *and* properties to disk (`--pin-to`) so a
later run can re-enter at formalization (`--properties`), skipping the two phases that on one real
target were 98% of the wall clock before the first prover job. Both halves are pinned because a unit
is an index into the analysis, so properties keyed by component name can attach to a component that
has changed under the name.

**It is held back deliberately, and the decision is the point.** Whether this is the right shape for
"start a run somewhere other than the beginning" is not settled, and nothing else in the branch needs
it — so the cost of deciding later is zero, while landing it early would make a 4,233-line checked-in
fixture and a second way to configure a run into things master has to keep working.

What its dependents must do while it is deferred:

* **P7** lands without `--pin-to` and `--properties` and without the `load_pinned_run` import —
  roughly eight lines of `composer/spec/cvlr/entry.py`. No test exercises them, so nothing else moves.
* The `pinned` / `pin_to` parameters stay out of `cli_pipeline` and `run_pipeline`, which is why the
  shared-pipeline caution above names two PRs rather than three.
* `tests/test_pinned_properties.py` and `tests/data/pins/` travel with the feature if it ever lands.

If it is dropped instead, the same paths are what to delete from the branch.

---

## Deferred: Solana's entry in the project-toolchain registry

`composer/cargo/toolchain.py` — `SolanaToolchain`, answering the two questions
`composer.rustapp.toolchain` asks a chain (which crate owns a file, and prepare a workspace) — and
the `PROJECT_TOOLCHAINS` entry binding it. [#243](https://github.com/Certora/AutoProver/pull/243)
was assembled with both and they came back out.

**It serves the Rust wheel path, not this branch.** The CVLR modules import `composer.cargo`'s
`metadata`, `sbf`, `session`, `symbols` and `depinfo`, and never `toolchain`; the only reader of the
registry is `composer/rustapp/adapter.py`, which is the wheel seam.

**Removed from the branch on 2026-10-14**, with the async `source_unit` it needed in
`composer/rustapp/`, so `composer/rustapp/` matches master except for `adapter.py`'s
`shared_unit`, which rides P7 with the hoist. The three `SolanaToolchain` cases in
`tests/test_cvlr_plumbing.py` went with it. Revive both from `eric/cargo-sbf` or this branch's
history if the Crucible work wants them.

**And it is a refactor with an owner already.**
[#98](https://github.com/Certora/AutoProver/pull/98) registers `{"solana": _Solana()}` — a lazy shim
over a `SolanaToolchain` in `composer.spec.solana.project`. So this was Crucible's toolchain lifted
into shared code, and landing it from here would put a second implementation on master with no
caller, on the line of the file that PR changes. It belongs with whatever moves Crucible off its own
copy; `d8ebccb7` on `eric/cargo-sbf` is the removal to revert for the text.

Keeping it out also keeps `composer/cargo/` purely additive wherever it lands, which is worth more
now than it was: under the re-cut the package arrives four files at a time, beside the CVLR module
that calls each one.

---

## Dropped: reporting whether the builds were confined

`AutoProverReport.build_environment` — `ConfinedBuilds | UnconfinedBuilds | None`, a `Builds` row in
the report header, a banner when a run was unconfined, and the `Formalizer.build_environment()` hook
that supplies it. It was written on the reasoning that an unconfined build makes every verdict in
the document a development result, and that stderr on the machine that ran it is not a record.

**It is not worth a schema field.** Nothing outside this backend reads it. S4 was opened with it,
and the field was removed before review.

An earlier version of this section said no formalizer on the branch overrides the hook. That was
wrong, and the deletion is larger than it said. `CvlrFormalizer.build_environment` in
`cvlr/pipeline.py` does override it. `tests/test_cvlr_findings.py` checks that override, and
`tests/test_cvlr_tape.py` asserts the field is present in a replayed report. So on a CVLR run the
field is set. The rebase kept it, because deleting a feature is not a rebase's call. What to
delete, if the drop stands: the field and its two types in `report/schema.py`; the
`build_environment` parameter in `report/build.py`; `report/render.py` and
`autoprove_report.html.j2`; the hook in `pipeline/core.py` and its override in `cvlr/pipeline.py`;
the three render tests; the `test_cvlr_findings.py` case; and the tape assertion.
`docs/cvlr-backend-plan.md` mentions the field in the record of a run that actually happened, and
should be left alone.

---

## Dropped: naming the CVLR corpus by tag

The branch treats `cvlr_kb` as a *tag*: an entry in `composer.rag.db.KNOWLEDGE_BASES`, a matching
factory in `composer/tools/rag_env.py`, a `--knowledge-base` flag on `ragbuild` that resolves the
first, and a `--rag-corpus` flag on `entry.py` that resolves both. None of that is how CVL, sanity or
Foundry reach a corpus, and asking why CVLR needs it has no answer that survives being asked twice.

A tag exists because a *wheel* has one: a Rust descriptor's `rag_db_default` crosses the FFI as a
string, so `rag_env` has to turn a string into a connection and a tool set. In-tree Python never
holds such a string unless a flag invents one. CVL's composition root builds a
`PostgreSQLRAGDatabase` from a constant and hands the object to `composer/spec/services.py`, which
never sees a name. The populate scripts pass `ragbuild --corpus <name>`. Since #244, that name
picks one of `db.py`'s connection constants inside `ragbuild` itself. It does not go through a
registry. The CVLR backend does both of those and needs no registry at either end.

So `db.py` gains one line — `CVLR_DEFAULT_CONNECTION`, beside the three constants that already name
a corpus's database — and nothing else. That line landed with #244. Not landing, in **P6** or anywhere: `ragbuild`'s
`--knowledge-base` flag, `rag_env`'s `_FACTORIES` entry, the `build_rag_tools(model=...)` parameter
added for a caller this drops, and the four `tests/test_rag_env.py` tests covering a registered
corpus. `tests/test_cvlr_rag.py` replaces the one of those worth keeping — that the three tools
bind, and that the prompt template names the tools that exist.

Only one `KNOWLEDGE_BASES` entry lands, `cvlr_api_kb`, in **C7b**, because that is where something
resolves a tag: `cvlr_api_docs.py` writes a manifest carrying `"knowledge_base": "cvlr_api_kb"`, and
`rag_import` has no other way to find the database it names. `cvlr_kb` gets no entry. It is filled
by `ragbuild --corpus cvlr`, which names its connection constant directly, and no manifest is
imported into it any more.

**The second corpus does not change the argument.** The branch went the other way after this
section was written, and was reworked back on 2026-10-13. The API-docs work (2026-09-24 onwards) registered both corpora in both maps,
gave `build_rag_tools` a `model` parameter so that two calls share one embedder, and made
`--rag-corpus` choose among `KNOWLEDGE_BASES` minus the API corpus. In practice that is
`{cvlr_kb, none}`, a boolean spelled as a tag. Two corpora make the in-tree case for a registry
weaker, not stronger. The researcher needs both corpora or neither, so nothing ever picks one
by name. A wheel's descriptor still could not declare two corpora, since `rag_db_default` is a
single string, so registering both does not serve that case either. The branch code is therefore
reworked rather than landed: see [decision 8](#decisions-to-make-before-starting).

`gen_docs.sh`'s `PROVENANCE` stamp was deferred to **C7b** until something read it. Its argument is
the one [cvlr-todo.md](./cvlr-todo.md) U6 gives: three manifests built in three places can be three
vintages, and one tag hides the seam. Under the new C7b two corpora are built on different
occasions, and `populate_cvlr_rag.sh` prints the manual's stamp beside the API manifest's `source`
line. It is still not CVLR-specific, since `gen_docs.sh` stamps all four manuals it builds. If it is
wanted for CVL too, rule 4 sends it to master on its own. **R3**'s Dockerfile writes its own copy in
its docs stage and does not call `gen_docs.sh`, so it is unaffected either way, at the cost of the
same `printf` living in two places.

The consequence lands in **P6** and **P7**: `cvlr_research.corpus_tools` opens both databases from
`db.py`'s constants and carries its own degrade-to-no-search for a corpus that will not open, and
`entry.py` has `--no-corpus` rather than `--rag-corpus`.

---

## Dropped: rescuing a grouping the model encoded as JSON text

Wave 1's S2, now reverted on the branch. A `field_validator` on `GroupingResult.groups` accepted a
string that parses as JSON and is either the group list or the whole `GroupingResult` wrapping it,
returning anything else untouched for pydantic to reject. One run on the reference project produced the second shape,
and the report fell back to a single `general` bucket.

**Its one observation predates the more general fix.** `ae5cc069` was authored 2026-09-08;
[#212](https://github.com/Certora/AutoProver/pull/212) — retry a rejected grouping once with the
rejection appended — merged 2026-09-14. A rebase replayed the branch and put the validator *after*
the retry in the history, which is misleading: it was written against a `call_grouping_llm` where a
single rejection went straight to the fallback. Whether the retry alone fixes this has never been
tested, and the mis-encoding has not recurred since.

Two arguments against landing it meanwhile. It widens `GroupingResult`'s contract everywhere the
type is validated, to rescue one provider-level serialization slip — and the wrapper the model
doubles is forced by the API, since a tool-input schema cannot be a bare array, so there is no
schema change that would prevent it. And it leaves the part that made the failure dangerous: a
degraded grouping is invisible in the artifact — `build.py` computes a `fallback_reason`, logs it,
and never puts it in the report, so a flattened eighty-nine-property run still reads as a
legitimate single-group report. That is filed as U8 in
[cvlr-todo.md](./cvlr-todo.md).

**How to revive it.** `eric/grouping-rescue` (local, branched from master at `0fcec7d1`) carries the
validator, a docstring built around the two accepted encodings, and four tests — both rescued shapes
and four strings that must still be rejected. Cherry-pick `1df970c8`. The condition is a recurrence
with #212 in place, which is also the thing the invisible fallback makes hard to notice.

---

## Dropped: a second read-only source mount for the explorer

Wave 1's S5, now removed from the branch in `8fec7fad`. `LibrarySource` paired a set of read-only
tools over the specification library the target depends on with the statement that tells an agent
they exist; `build_source_tools` took one and gave the tools to the code explorer's env and the
statement to its prompt, through a `crate_source` parameter threaded into the shared Rust explorer
fragment. The argument was that a sub-agent is where a broad read costs least, since it returns a
short answer instead of filling the caller's context.

**Nothing ever built one.** Not on master, not on the branch. `cvlr/entry.py`, the only caller that
could, leaves it unset because which crates the target resolves is not known until preflight has
run. The crates reached the author for a while by another route: `cvlr/pipeline.py` built
`cvlr_source_tools` and handed them through `CvlrDeps`, never touching this seam. That mount is gone
too (`5b2afeff`), replaced by the research sub-agent P6 carries. So this was a design for a mount
that was then built another way, and then retired.

Two defects only a first user would have found, both arguing the same thing. The
`{% if crate_source %}` block lives in the Rust fragment alone, so an EVM caller would have bound
the tools and dropped the statement — exactly the pairing failure `LibrarySource` exists to prevent.
And that fragment names `cvlr_source_*` tools it cannot see, in a file `solana.j2` and `soroban.j2`
both include.

**How to revive it.** Revert `8fec7fad`, which carries the type, the parameter, the prompt plumbing
and the four `test_cvlr_knowledge.py` tests that covered the rendering. The condition is a caller
that actually wants the explorer to read the library, which means resolving the crates before the
source tools are built. Weigh it against giving the explorer `cvlr_research` instead. That is the
direction [cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md) §1 points, and it needs no mount.

---

## Dropped: the practice manifest

The old **C7c**: project-derived CVLR idioms, imported into `cvlr_kb` from manifests built in
`certora-cvlr-kb`, with an `UNREVIEWED` disclosure in `cvlr_manual_search`'s description so that an
entry not yet signed off read as a lead rather than as authority.

**There is no such content left to disclose.** [cvlr-knowledge-plan.md](./cvlr-knowledge-plan.md)
§4 decided that CVLR practice knowledge is written by hand and delivered through the bundle (K1) and
the recipes (K2), not through corpus search, because a corpus is allowed to be absent at run time
and nothing load-bearing may depend on it. `26f7b9c9` then re-scoped `cvlr_kb` to the manual. On
2026-09-29 the live database held only the manual: 169 sections and 248 embedded documents, all
under `solana`. `populate_cvlr_rag.sh` no longer discovers manifests outside this repo, so the
disclosure has nothing to disclose and the discovery has nothing to find. `certora-cvlr-kb` is being
retired, and what survives of its ledger reaches AutoProver as K2's recipes.

**How to revive it.** Nothing to revert: the disclosure never landed on master, and the branch
dropped it with the discovery. The condition would be a second source of corpus rows that are
not signed off. If one appears, give it its own corpus and its own tools, as `cvlr_api_kb` has,
rather than an annotation inside `cvlr_kb`. A retrieval hit carries no provenance, and the tool
name is the only thing that can.

---

## Decisions to make before starting

**1. ~~[#238](https://github.com/Certora/AutoProver/pull/238) duplicates
[#223](https://github.com/Certora/AutoProver/pull/223).~~ Settled: #238 is closed, and #223 merged
on 2026-09-17 as `0fcec7d1`.** Of the two things worth carrying from the closed PR, one landed with
it: `_PERMANENT_FETCH_ERRORS` declines the four failures a second attempt cannot change — a bad
token, a malformed job reference, a missing job, an unparseable document. The other did not, and is
now a stated choice rather than an oversight: each attempt starts from an empty destination so a
truncated file cannot survive one, at the cost of re-downloading what already arrived, where the
client library's own completion markers would have let a retry resume. This branch never carried its
own copy, so its next rebase takes the fix rather than conflicting with it.

**2. Does the 3 MB tape belong in the repository?** P9 is the only PR here that a reviewer cannot
read. It is a generated artifact, and the argument for checking it in is that a gate nobody can run
protects nothing — but it is also 25,000 lines in every future clone and diff, and every
re-recording rewrites most of them.

**3. ~~When does the graphcore pin move?~~ Settled: with P3a1.** The bump was its own PR (R2) on the
reasoning that it changes the library under every consumer and deserves its own blast radius. It
does not, in practice: graphcore #39 is additive, and the one line it changes in an existing
function gives `fs_tools_layered` a `materializer` factory that defaults to today's behaviour, so no
existing caller moves. Nothing outside `cvlr/tree.py` uses what it adds. A two-file pin bump with no
user is not a review, so it lands with the code that needs it. P3a1's review tried a second bump,
to a `sync_base` for reusing a tree across runs (graphcore #40); the fresh tree per run made it
unnecessary, #40 was closed, and the pin stayed at `9f4e9fc`. It landed with #260.

**4. Three open PRs touch the same files as S4.**
[#185](https://github.com/Certora/AutoProver/pull/185) and
[#232](https://github.com/Certora/AutoProver/pull/232) are in the report package itself;
[#228](https://github.com/Certora/AutoProver/pull/228) is in `pipeline/core.py`, a few lines from
the give-up boundary S4 retypes. All three were open when this was written. Whichever landed second
would pay the merge in each pair, so the order was worth deciding deliberately rather than
discovering it.

*What happened:* #228 merged on 2026-09-18 and #232 on 2026-09-20, both ahead of S4. #228's merge
was mechanical. #232's was not, and it is why S4 was closed and folded into P7 (wave 1, *Why S4
was folded rather than rebuilt*). Only #185 is still open, and P7 now inherits that pairing.

**5. Is there an EVM-visible behaviour change anywhere in wave 1?** Two, and both PRs said so in
their own bodies rather than leaving a reviewer to find it. **S3** was the larger and is now on
master: every EVM trace renders through `TraceShape`, and `cex_dump` is a derived property whose
text gained a `<counterexample>` envelope, which two report tests had to be updated for. S4's was
smaller and deliberate: a component that gives up now records its reason in the report, on EVM runs
as much as any other. It now lands in **P7**, so P7 is the one wave-2 PR with an EVM-visible change.
Everything else in the wave either defaults to today's value or is reached
only by a caller that does not exist yet on master.

**6. What happens to the two open PRs the re-cut dissolves?** Both are answerable without closing
anything.

[#243](https://github.com/Certora/AutoProver/pull/243) — `composer/cargo/` — becomes **P1** by
*growing* rather than by closing: push `cvlr_reference`, `crates`, `conf`, `env_paths`, `scaffold`,
`preflight` and the `cvlr-preflight` script onto the same branch, retitle, and rewrite the body
around what the command does. `sbf` then moves out to P2, `symbols` to P3b and `depinfo` to P5,
which is most of the point — the eight files were never one reviewable unit, they were one
directory. The review history survives, and the reviewer who said the code needs its context gets
the context in the same thread. The alternative, closing it and opening P1 fresh, costs that thread
and buys nothing.

*What happened instead:* #243 was closed on 2026-09-18 and P1 opened fresh as
[#248](https://github.com/Certora/AutoProver/pull/248), from `eric/cvlr-preflight`.

[#244](https://github.com/Certora/AutoProver/pull/244) — the corpus — **stays as it is, minus
`composer/templates/cvlr_rag_tools.j2`**, which moves to P6 with the prompt that includes it. The
tools and the ingestion are a capability someone can run today; the template is a fragment with no
renderer. Dropping that one file also removes the old plan's only cross-wave ordering constraint.

*What happened instead:* #244 merged on 2026-09-22 with the template in it. That costs nothing
except one file on master that nothing renders until P6.

**7. ~~Does P3 land as one PR or two?~~ Settled: three, P3a1, P3a2 and P3b (2026-10-02, rejoined 2026-10-12).** One capability, +3,967 code and +2,280 tests, or two of
roughly half that at the cost of splitting `tests/test_cvlr_tree.py` and
`tests/test_cvlr_module_redirect.py` along the same line. The split is written into the tables above
as P3a/P3b because it is the better default, but it is the one place in the wave where the partition
is a judgement rather than a consequence of the DAG, and a reviewer who would rather read 6,000
lines of one story than two halves of it should say so before the split work is done.
*What happened:* P3a2 was dropped on 2026-10-14 with the munge records, and P3a1 merged as #260 with
the tree alone. The split of `tests/test_cvlr_tree.py` stands: its harness cases ride P3b1.
*And then:* P3b was split on 2026-10-16, first along the vacuity diagnostic (P3b3), which moves out
by whole files and one tool's entry in `gate_tools`, and then along what a stamp depends on:
submitting a draft (P3b1), and the summaries, edits and settings a verdict also depends on (P3b2).
P3b2 was split again on 2026-10-23, into those inputs (P3b2a) and how a verdict is read (P3b2b).

**8. ~~Does the "no tag registry" drop survive a second corpus?~~ Settled 2026-10-13: yes, and the
branch is reworked to match.** The API-docs work had registered `cvlr_kb` and `cvlr_api_kb` in both
`KNOWLEDGE_BASES` and `rag_env._FACTORIES`, opened both through `build_rag_tools`, and kept
`--rag-corpus`. The case for the drop is in
[Dropped: naming the CVLR corpus by tag](#dropped-naming-the-cvlr-corpus-by-tag): the flag's only
real choice was on or off, and a wheel could not declare two corpora anyway. Now
`cvlr_research.corpus_tools` opens both corpora by connection, `--no-corpus` replaces
`--rag-corpus`, `cvlr_tape.missing_corpora` reads the same list, `rag_env.py` and its tests are
master's, and `KNOWLEDGE_BASES` holds only `cvlr_api_kb`, for `rag_import`.

---

## Appendix: the paths each PR takes

What to hand `git checkout eric/solana-staging -- …` after branching from `origin/master`. Every file
the branch changes appears below, except those carrying only the dropped build-environment field.
A path under two PRs, and the shared pipeline files marked *(hunks)*, are the overlaps noted in
wave 1 — take the feature's hunks there, not the whole file. Rows for merged PRs are kept as the
record of what went where; nothing is left to check out for those.

Rows marked *(split)* are files whose contents straddle a slice boundary and have to be cut rather
than moved. That is the bill rule 5 runs up, and it is paid almost entirely in tests, because the
code was already cut this way — the exception is `tuning.py`, whose layer families and
`compose_env` the scaffold needs in P1 and whose summary directives and `TuningFiles` the authoring
loop needs in P3b2a. It is a small bill: five files, and in three of them the straddling imports are
already local to the functions that need them.

| PR | Paths |
|----|-------|
| S1 *(merged)* | `composer/layout.py` `composer/spec/gen_types.py` `composer/sandbox/recipes.py` `composer/pipeline/ecosystem.py` `composer/foundry/entry.py` `composer/spec/source/autoprove_common.py` `tests/test_fs_forbidden_read.py` `tests/test_sandbox_config.py` `scripts/docker-compose.sandbox.yml` `composer/pipeline/cli.py` *(hunks)* |
| S3 *(merged)* | `composer/certora_env.py` `composer/prover/{certoraRunWrapper,core,ptypes,results}.py` `analyzer/analysis.py` `composer/tools/{prover,thinking}.py` `composer/authoring/buffer.py` `composer/core/context.py` `composer/cvl/tools.py` `composer/workflow/executor.py` `composer/spec/source/{autoprove_common,harness}.py` `composer/spec/source/munge/compile_check.py` `tests/conftest.py` `tests/test_prover_app.py` `tests/test_prover_options.py` `tests/test_wrapped_prover_runner.py` `tests/test_solana_cex_trace.py` `tests/data/solana_cex/` `tests/test_tree_parsing.py` `tests/test_cex_analysis_failure_isolation.py` `tests/test_autoprove_report.py` *(hunks)* |
| S4 *(closed)* | nothing: the give-up reason goes to P7, `job_input` to P3b3, the fetcher to P7 |
| P1 *(merged)* | `composer/cargo/{__init__,features,manifest,metadata,session}.py` `composer/spec/cvlr/{__init__,conf,crates,env_paths,forks,preflight,reference,scaffold,tuning}.py` `composer/spec/cvlr/envs/` `composer/spec/cvlr/harness_files/` `composer/prover/conf.py` `composer/spec/source/{prover,author,artifacts}.py` *(hunks)* `composer/spec/natspec/task_description.py` *(hunks)* `tests/test_cvlr_{env_paths,forks,plumbing,preflight,reference,scaffold}.py` `tests/test_prover_conf.py` `tests/test_rules_striping.py` *(hunks)* `tests/test_stuck_rule_warnings.py` *(hunks)* `tests/data/vault_sbf_symbols.txt` `pyproject.toml` `uv.lock` |
| P1b, to master | `composer/spec/cvlr/{reference,scaffold,preflight,crates,env_paths,conf}.py` *(hunks: SDK 3, platform-tools v1.53, the host toolchain, Prover master, the writable cargo home)* `composer/cargo/{metadata,sbf,sbf_build_script,session}.py` *(hunks)* `composer/sandbox/{config,recipes}.py` *(hunks)* `tests/test_cvlr_{reference,scaffold,preflight,forks,env_paths,plumbing,end_to_end}.py` *(hunks)* `tests/test_sandbox_config.py` *(hunks)* `tests/data/stake_sdk3_sbf_symbols.txt` `tests/data/vault_sbf_symbols.txt` |
| P1 follow-up, to master now | `composer/spec/cvlr/preflight.py` *(hunk: `check(manifest_dir=…)`)* `composer/spec/cvlr/crates.py` *(hunk: drop `roots()`)* — plus the regression test, still to write |
| P2 *(merged)* | `composer/cargo/sbf.py` `composer/spec/cvlr/prover.py` `tests/test_cvlr_end_to_end.py` `tests/test_cvlr_plumbing.py` *(split: `sbf_argv`, the build script, `write_submission`)* |
| Toolchains, to master now | `composer/spec/cvlr/conf.py` *(hunks: `PLATFORM_TOOLS_VERSION`, `HOST_RUST_TOOLCHAIN`)* `composer/cargo/{metadata,sbf,sbf_build_script,session}.py` *(hunks: the host toolchain)* `composer/sandbox/{config,recipes}.py` *(hunks: the host toolchain)* `composer/spec/cvlr/preflight.py` *(hunk: the host toolchain)* `tests/data/vault_sbf_symbols.txt` `tests/test_cvlr_env_paths.py` *(hunk: the v1.53 coverage counts, not the directive counts)* `tests/test_cvlr_plumbing.py` `tests/test_cvlr_scaffold.py` `tests/test_sandbox_config.py` *(hunks: the host toolchain)* |
| Sound signed math *(held)* | `composer/spec/cvlr/conf.py` *(hunk: `-solanaTACSoundSignedMath`; not `prover_version`)* `tests/test_cvlr_plumbing.py` *(hunk: `test_the_base_models_top_bit_constants_soundly`)* |
| Tuning directives *(held)* | `composer/spec/cvlr/envs/` *(hunks)* `tests/test_cvlr_env_paths.py` *(hunk: the directive counts)* `tests/test_cvlr_dropped_writes.py` `tests/data/dropped_writes_probe.rs` |
| P3a1 *(merged)* | `composer/spec/cvlr/tree.py` `composer/spec/cvlr/preflight.py` *(hunk: `source`)* `composer/layout.py` `composer/pipeline/ecosystem.py` *(hunks: `RUST_BUILD_DIRS`)* `graphcore` `pyproject.toml` *(hunk: the pin)* `tests/test_cvlr_tree.py` *(split: the cases with no harness)* `tests/test_fs_forbidden_read.py` *(hunk)* |
| P3a1 follow-up, to master now | `composer/spec/cvlr/tree.py` *(hunks: drop `.cvlr_work`, the `materialize` docstring, the dangling symlink)* `tests/test_cvlr_tree.py` *(hunk: the dangling symlink)* |
| P3a2 *(dropped)* | nothing: `munge.py` and the module-redirect tests are deleted on the branch |
| P3b1 *(merged, #267; `composer/prover/accounting.py` new in review, `UnanalyzedCexHandler` dropped)* | `composer/spec/cvlr/harness.py` *(split: `HarnessModule`, `CvlrArtifactStore` with `declare_modules`, `GeneratedHarness` without its published fields)* `composer/spec/cvlr/manifest.py` *(new work)* `composer/spec/cvlr/verify.py` *(the gate: `BuildPermit`, `HarnessTarget`, `_RunAccounting`, `CargoCheck`, `VerifyRules` without analysis or the notes P3b2 adds)* `composer/spec/cvlr/state.py` *(split: `PROVER_VALIDATION_KEY`, the generation state)* `composer/prover/core.py` *(hunk: `UnanalyzedCexHandler`)* `composer/spec/cvlr/tree.py` *(docstring: no placeholder module)* `composer/spec/util.py` `composer/pipeline/ecosystem.py` `composer/spec/{solana,soroban}/model.py` *(hunks: `slugify_rust_module`)* `pyproject.toml` `uv.lock` *(hunks: `pyelftools`)* `tests/test_cvlr_verify.py` `tests/test_cvlr_manifest.py` `tests/test_slugify_rust_module.py` `tests/test_{solana,soroban}_components.py` *(hunks)* `tests/test_cvlr_tree.py` *(split: the feature-gating case)* `tests/test_cvlr_plumbing.py` *(split: `_RunAccounting`)* |
| P3b2a *(merged, #268; `optimistic_loop` landed as a bool, `OptimisticLoop` rides P6)* | `composer/spec/cvlr/author_tools.py` *(split: `SummarizeForProver`, `AdjustProverConfig`)* `composer/spec/cvlr/tuning.py` *(split: `SummaryDirective`, `TuningFiles`, appended to what P1 landed)* `composer/spec/cvlr/conf.py` *(hunks: `SettingsEdit`, `merge_settings`)* `composer/spec/cvlr/state.py` *(split: summaries, edits, settings, `tuning_history`)* `composer/spec/cvlr/verify.py` *(the staging of summaries and edits, the settings in the submission, the inert-summaries note)* `composer/cargo/symbols.py` `composer/templates/cvlr/inert_summaries.j2` `template_manifest.json` *(hunk: `_InertSummaries`)* `tests/test_cvlr_tuning.py` `tests/test_cvlr_symbols.py` `tests/test_cvlr_prover_settings.py` `tests/test_cvlr_verify.py` *(additions)* `tests/test_cvlr_plumbing.py` *(split: the `optimistic_loop` conf case)* |
| P3b2b *(branch `eric/cvlr-verdict-meaning`, #280)* | `composer/spec/cvlr/author_tools.py` *(split: the markings, `unaccounted`, `stamp_under`)* `composer/spec/cvlr/state.py` *(split: `LastVerdicts`, `expected_failures`, external functions)* `composer/spec/cvlr/verify.py` *(the report's notes and the stamp decision; less P3b3's vacuity tool)* `composer/templates/cvlr/` *(the other notes)* `composer/prover/core.py` *(hunks: `ProverReport.incomplete`, `ProverReport.external_functions`)* `composer/prover/results.py` *(hunk: `external_functions`)* `composer/prover/cloud.py` *(hunk: `_fetch_alert_report`)* `composer/prover/ptypes.py` *(hunk: `LoopUnwound`, `OverflowUnproven`)* `tests/test_cvlr_restamp.py` `tests/test_prover_external_functions.py` `tests/test_solana_cex_trace.py` *(hunk)* `tests/test_cvlr_verify.py` *(additions)* |
| P3b3 | `composer/spec/cvlr/vacuity.py` `composer/templates/cvlr_vacuity_{prompt,system_prompt}.j2` `composer/templates/unsat_core_{format,reading_steps}.j2` `composer/templates/sanity_tool_prompt.j2` `composer/spec/cvlr/verify.py` *(the rest: `_DiagnosticAccounting`, `VacuityDeps`, `ExplainVacuity`, the `explain_vacuity` entry in `gate_tools`)* `composer/spec/cvlr/conf.py` *(hunks: `CheckVerdicts`, `CollectUnsatCore`, `RunOverlay.purpose`, the `solana_conf` case)* `composer/spec/cvlr/prover.py` *(hunk: `Submission.purpose`)* `composer/spec/source/report_prover.py` *(hunks: `job_input`, `fetch_unsat_cores`)* `composer/prover/cloud.py` *(hunk: `_results_api` → `results_api`)* `composer/spec/cvlr/verify.py` *(the `_record_link` hook)* `tests/test_cvlr_vacuity.py` |
| Anchor-reach probe *(held; may never land)* | `tests/test_cvlr_anchor_reach.py` `tests/data/anchor_reach_probe.rs` |
| P4 | `composer/spec/cvlr/{anchor_surface,example,guidance,rust_source}.py` `tests/test_cvlr_anchor_surface.py` `tests/test_cvlr_worked_example.py` |
| C7b | `composer/scripts/cvlr_api_docs.py` `composer/cargo/doctest.py` `composer/tools/cvlr_api_rag.py` `composer/tools/cvlr_rag.py` *(hunks: the docstrings)* `composer/scripts/init-db.sql` *(hunks: `cvlr_api_rag`)* `composer/rag/db.py` *(hunks: `CVLR_API_DEFAULT_CONNECTION`, the `cvlr_api_kb` entry and its comment — not `cvlr_kb`'s; decision 8)* `scripts/populate_cvlr_rag.sh` `scripts/gen_docs.sh` `.gitignore` `docs/rag-import-format.md` *(hunks, after the fix above)* `tests/test_cvlr_api_docs.py` `tests/data/cvlr_rustdoc/` `tests/test_cvlr_research.py` *(split: the tool cases, through `test_the_surface_heading_is_shared_by_the_producer_and_the_tool`)* `tests/test_cargo_doctest.py` `tests/test_cvlr_recipe_examples.py` |
| K1 | `composer/kb/kb_context.py` `composer/kb/knowledge_base.py` `composer/kb/resources/cvlr_baseline_facts.md` `composer/templates/kb_index.j2` `composer/templates/cvl_kb_index.j2` *(deleted: `git rm` it; `kb_index.j2` replaces it)* `tests/test_kb_bundle.py` `tests/test_cvlr_bundle.py` `composer/kb/resources/cvlr_recipes_index.yaml` `composer/kb/resources/recipes/cvlr-*.md` |
| P5 | `composer/spec/cvlr/editor.py` `composer/cargo/depinfo.py` `composer/templates/cvlr_munge_editor_system.j2` `composer/templates/cvlr_munge_review_system.j2` `composer/spec/source/munge/{edit_tools,munge_agent,vfs_diff}.py` `composer/spec/source/munge/edit_store.py` *(less `AppliedEdit`, which P6 takes)* `composer/spec/context.py` *(hunks: `CvlrGeneration`, `CvlrJudge`; `editor.py` is their first importer)* `composer/spec/source/{author,live_explorer}.py` *(hunks: onto the lifted core)* `composer/spec/source/report/{schema,collect,build,render}.py` *(hunks: `SourceEditRecord`)* `tests/test_cvlr_editor.py` `tests/test_cvlr_source_edits.py` |
| P6 | `composer/spec/cvlr/author.py` `composer/spec/cvlr/rules.py` `tests/test_cvlr_rules.py` `composer/spec/cvlr/verify.py` *(hunks: `AuthorModel`, `CexAnalysis`, `_CaptureCallbacks`, `VerifyDeps.analysis`)* `tests/test_cvlr_plumbing.py` *(split: the capture and bound-model cases)* `composer/spec/cvlr/conf.py` *(hunk: `OptimisticLoop`, the stored reason, in place of #268's bool)* `composer/spec/cvlr/state.py` *(the remainder: the publish gate — `PropertyRuleMapping`, `RuleSubject`, `validate_property_rules`, `validate_rule_subjects`, `check_cvlr_completion`, `HarnessAssumptions`, `harness_assumptions`, `CVLR_JUDGE_KEY`, `FEEDBACK`, `CvlrGenerationInput`)* `composer/spec/cvlr/harness.py` *(hunk: `GeneratedHarness`'s published fields: `skipped`, `final_link`, `output_link`)* `tests/test_cvlr_tree.py` *(hunk: the `output_link` case)* `composer/spec/source/munge/edit_store.py` *(hunk: `AppliedEdit`)* `composer/spec/cvl_generation.py` *(hunk: `AppliedEdit` moved out)* `composer/spec/cvlr/verify.py` *(hunk: `_nothing_to_submit`, the all-skipped stamp)* `tests/test_cvlr_verify.py` *(hunk: the publish-gate fields in `_state`)* `composer/spec/cvlr_research.py` `composer/templates/cvlr_{feedback_prompt,property_generation_prompt,property_generation_system_prompt,property_judge_system_prompt,research,research_system_prompt}.j2` `composer/templates/cvlr_rag_tools.j2` *(new work: delete)* `composer/ui/tool_display.py` *(hunks: `cvlr_research` and the `cvlr_api_*` / manual entries)* `composer/spec/source/source_env.py` `template_manifest.json` `tests/test_cvlr_judge_input.py` `tests/test_cvlr_knowledge.py` `tests/test_cvlr_research.py` *(split: the corpora, researcher and prompt cases)* `tests/test_cvlr_rag.py` *(new work: drop the template case)* `tests/data/cvlr_judge/` `tests/test_cvlr_author_cache_ttl.py` `tests/test_cvlr_loop_bound.py` `tests/data/loop_bound_probe.rs` |
| P7 | `composer/spec/source/report/{schema,collect,build}.py` *(hunks: `Abandoned`, `reason`, 3.2)* `composer/pipeline/core.py` *(hunks: `_abandonment`)* `tests/test_autoprove_report.py` *(hunks: the give-up cases)* `composer/spec/cvlr/pipeline.py` `composer/spec/cvlr/entry.py` *(the whole file, now including the parser P1 did not take)* `composer/spec/cvlr/harness.py` *(hunk: `CvlrArtifactStore._job_info_payload`)* the `cvlr-preflight` console script *(new work, still to write)* `composer/spec/cvlr/reference.py` *(hunks: `ProgramModel`, `withholding`)* `tests/test_cvlr_reference.py` *(hunks)* `composer/spec/source/report_prover.py` *(hunk: `make_run_link_fetcher`)* `composer/cli/console_solana.py` `composer/cli/tui_solana.py` `composer/spec/context.py` *(the remainder)* `composer/spec/services.py` `composer/ui/tool_display.py` *(the remainder)* `tests/test_cvlr_entry.py` `tests/test_cvlr_findings.py` `tests/test_cvlr_author.py` `tests/test_cvlr_judge_round_cost.py` `tests/test_cvlr_plumbing.py` *(the remainder)* `tests/test_autoprove_integration.py` `.github/workflows/integration-tests.yml` `composer/spec/cvlr/patch.py` `tests/test_cvlr_patch.py` `composer/spec/hoist.py` `composer/templates/property_hoisting_{prompt,system}.j2` `tests/test_hoist.py` `composer/pipeline/{core,keys,ecosystem}.py` *(hunks: the hoist, the formalizer hooks)* `composer/foundry/pipeline.py` `composer/rustapp/adapter.py` `composer/spec/source/pipeline.py` `composer/spec/{solana,soroban}/null_backend.py` *(hunks: `shared_unit`)* `composer/spec/solana/model.py` `composer/templates/solana/{analysis_prompt,component_context,property_prompt}.j2` `tests/test_solana_components.py` `composer/spec/source/report/{schema,build,render}.py` `composer/templates/autoprove_report.html.j2` *(hunks: hoisted claims, the deliverable)* `tests/test_pipeline_{staged_formalizer,overlap}.py` `tests/test_context_cache.py` `composer/cli/ap_trail.py` *(hunk)* `composer/cli/diagnostics/ap_trail_recover.py` `tests/test_ap_trail_recover.py` `README.md` `README-solana.md` `ARCHITECTURE.md` `budget_integration.md` `composer/pipeline/PLUGIN.md` |
| P8 | `tests/test_cvlr_gate.py` `test_scenarios/solana_vault_idl/` |
| P9 | `composer/testing/` `scripts/record_cvlr_tape.sh` `tests/test_cvlr_tape.py` `tests/test_tape_setup.py` `composer/io/context.py` *(hunks: sub-agent lanes)* `tests/test_tape_subagent_lanes.py` `.claude/skills/generate-tape/SKILL.md` |
| R3 | `scripts/Dockerfile` `scripts/Dockerfile.solana` `scripts/docker-compose.yml` `scripts/docker-compose.common.yml` `scripts/docker-compose.solana.yml` `scripts/autoprove-entrypoint.sh` `tests/test_cvlr_image.py` `composer/scripts/solana_toolchain.py` `scripts/verify-solana-image.sh` |
| P5 *(docs)* | `source_edits_integration.md` |
| Miscellaneous, to master | `composer/prover/results.py` *(hunks: `flatten_tree_view`, the alert errors)* `tests/test_tree_view_static_verdict.py` `composer/prover/{cloud,core}.py` *(hunks: the results fetch, the job failure's link and alerts)* `tests/test_cloud_fetch_retry.py` `tests/test_cloud_results_fetch.py` `tests/test_alert_errors.py` `composer/templates/cex_instructions.j2` `composer/pipeline/cli.py` *(hunks)* `composer/spec/source/prover.py` *(docstring)* `composer/authoring/judge.py` `composer/spec/cvl_research.py` `composer/rag/wipe.py` `composer/scripts/{ragbuild,rag_import}.py` *(hunks)* `scripts/wipe_rag.py` `tests/test_rag_wipe.py` `scripts/autoprove-entrypoint.sh` *(hunk: `setup-db`)* `composer/pipeline/ptypes.py` `composer/cli/{console_autoprove,console_foundry}.py` `composer/scripts/budget_math.py` `tests/test_pipeline_result.py` `composer/spec/source/report/{build,findings,grouping,render,schema}.py` `composer/templates/autoprove_report.html.j2` `tests/test_autoprove_report.py` *(hunks: U8, U21, the finding drafts)* |
| Pinned runs *(deferred)* | `composer/pipeline/pinned.py` `tests/test_pinned_properties.py` `tests/data/pins/` |
| D | `docs/` *(less the sections that ride their slice, and less `rag-import-format.md`, which goes with C7b)* |

---

## Appendix: the old partition, and where it went

The first draft's names appear in commit messages, in two open PR bodies and in
[cvlr-todo.md](./cvlr-todo.md). This is the map.

| Old | Where it went | Why |
|-----|---------------|-----|
| **S6** | K1 | A one-instance refactor; merges with the instance that justifies it. |
| **R1** | P1 *(metadata, session)*, P2 *(sbf)*, P3b2a *(symbols)*, P5 *(depinfo)* | One directory, four unrelated consumers. The PR the feedback was about. |
| **C1a** | P1 | The conf vocabulary lands with the preflight that writes it. |
| **C1c** | P1 | Scaffold and preflight were already one thing; C1a was their only missing input. |
| **C3a** | nothing: the munge vocabulary was deleted (2026-10-14); its fork half, now `forks.py`, went to P1 | The dependency-fork half landed with the `scaffold` that calls it. The vocabulary was planned for P3a1 and P3a2, beside the `tree` that applied it, until text edits replaced it. |
| **C2** | P3a1 *(tree, the graphcore pin)*; its crate mount, `crate_mount` and `source_tools`, is deleted (`5b2afeff`) | The working copy and the agents' view of the crates are two subjects, not one. The second is now the research sub-agent in P6. |
| **C5a** | P2 *(prover)*, P3b2a *(tuning, less the layer families, which go to P1)*, P4 *(anchor_surface)* | Three modules with three different first callers. |
| **C4a1** | P2 *(rules)*, P3b1 *(state, harness)*, P6 *(the publish gate)* | `rules` belongs with submission; `state` and `harness` belong with the `verify` that reads them, and the publish gate with the author that runs it. |
| **C5b** | P3b1, P3b2, P3b3 *(`explain_vacuity`)* | `verify` lands with its three inputs and with the `symbols` it exists to match against. |
| **C4b** | P4 | The worked example lands with the surface analysis it renders. |
| **C3b** | P5 | Unchanged in content; gains `depinfo`, its only caller. |
| **C4a2** | P6 | Gains the research sub-agent its prompts name, which replaced the crate mount. |
| **C6** | P7 | Unchanged in content, less the argument parser, which goes to P1 with `preflight`. |
| **C7a** | P6 *(the template)*, otherwise unchanged as [#244](https://github.com/Certora/AutoProver/pull/244) | The tools are usable today; only the fragment lacked a renderer. |
| **C7b** | C7b, moved to wave 2 ahead of P6 | Now the rustdoc producer, the `cvlr_api_kb` corpus and its tools. The discovery seam it was going to land is deleted. |
| **C7c** | dropped | `cvlr_kb` is now the manual alone; see *Dropped: the practice manifest*. |
| **C8a**, **C8b** | P8, P9 | Renamed only. |
| **R3** | unchanged | Still after the CLI exists. |
| **K2** | K1 | The bundle loads the recipe index, so the index and its two bodies cannot land after it. |
| **D** | distributed, then D | A document describing code is the clearest case rule 5 has. |
