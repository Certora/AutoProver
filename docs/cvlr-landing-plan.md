# Landing the CVLR backend

`eric/solanaProver` is 128 commits over 165 files — about 95,000 inserted lines, of which one
recorded tape is 51,000 and one pinned fixture is 4,200. It cannot be reviewed as a branch. This
document breaks it into pull requests that each stand on their own, ordered so that the work with
no CVLR dependency lands first.

It is a decomposition, not a schedule. Nothing here says who does the work or when, and the sizes
are the argument for the split rather than an estimate of effort.

**Re-cut 2026-09-18.** The first draft split the branch along its module DAG — leaves first, wiring
last. [#243](https://github.com/Certora/AutoProver/pull/243) is what that produces: `composer/cargo/`,
1,242 lines, eight new files, no caller anywhere in the diff. The review feedback was that it cannot
be evaluated out of the context of its usage, and that is a correct reading of the partition rather
than of the PR — the same draft asked for three more like it, and made every one of twelve backend
PRs unreachable until the last. Wave 2 is re-cut below so that each feature arrives in the first PR
that uses it. Rule 5 states the criterion; the [old-name map](#appendix-the-old-partition-and-where-it-went)
is the last appendix. The rules, the deferrals and the drops are otherwise unchanged.

**Where it stands, 2026-10-01.** Wave 1 is finished: S4 was closed and folded into P7. Of wave 2, **P1 has merged**
([#248](https://github.com/Certora/AutoProver/pull/248), `2eb4a391`), and the corpus half of P6
merged ahead of it as [#244](https://github.com/Certora/AutoProver/pull/244). The branch has been
rebased onto that master: 271 commits, 189 files, about 100,000 inserted lines. P1 changed a lot in
review, and the branch carries 727 lines of later work on files P1 now owns. The
[list below](#what-the-branch-carries-on-top-of-p1s-files) assigns each of those to the PR it rides.
The next PR is **P2**.

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
PR is built by branching from `origin/master`, `git checkout eric/solanaProver -- <paths>`, and
committing once with a message written for a reader who has no Solana context. Cherry-picking the
branch's commits does not produce these PRs and will fight you.

**3. Each PR must pass on master alone.** `uv run --no-sync pytest tests/ -m "not expensive" -q` and
`uv run --no-sync pyright`, on a branch off `origin/master` with only that PR applied. The structural
half of the same rule: nothing outside `composer/spec/cvlr/` may import from it. Where a shared seam
needs to know about CVLR, it takes a parameter or a registry entry instead — that is why the seams in
wave 1 look the way they do.

**4. Shared-code work goes to master directly.** The rough-draft gate fix
([#233](https://github.com/Certora/AutoProver/pull/233)) was written on this branch and landed on
master as its own PR, and the branch then dropped it in a rebase at no cost. That is the pattern.
Every shared fix that instead waits on this branch is a rebase conflict being saved up. The
exception rule 5 carves out is a *refactor* with no second user, which is not a fix.

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
  in **S4**. Since #232 it has no caller on master at all (wave 1, below), so it waits for P3b.

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
unreviewed on 2026-10-01, and its parts went to P3b and P7 (below). S5 is gone: its mount is
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
  as given. That is **P3b**.
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
| **P2** A hand-written rule in, verdicts out — [#258](https://github.com/Certora/AutoProver/pull/258), draft | +822 | +900 | Build a CVLR harness for SBF and submit it, and read the verdicts back. The deterministic submission path, end to end, with no agent in it. |
| **P3a** The working copy and what may be changed in it | +2,048 | +1,310 | Materialize a per-unit working tree over the project and apply a source modification to it — the nine munge kinds, expressed over the Rust parsing primitives. |
| **P3b** A generated harness, and what its verdict means | +1,919 | +1,180 | Take a generated harness module through tuning, build and submission, and decide what the run proved — including when a rule passed for a reason that says nothing about the program. |
| **P4** The Anchor surface and the worked example | +715 | +720 | Read an Anchor program's account and handler surface, and render the worked example an author is shown against that program rather than against a generic one. |
| **K1** A knowledge bundle, and the second instance that justifies it | +1,100 | +170 | Hold two corpora behind one context mechanism instead of a fork of it, and carry the run-invariant CVLR fact the author's prompt would otherwise restate. |
| **P5** The munge editor | +2,030 | +920 | Ask an agent to make a source modification, and have a second one accept or reject it. |
| **P6** The authoring loop | +2,050 | +2,200 | Give the author a program and get CVLR rules, judged, with the CVLR corpus and the crate source behind it. |
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
and `compose_env`, which the scaffold writes composites with — and **P3b** appends the summaries the
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
`tests/test_cvlr_plumbing.py` are on master. The plumbing file's P2 and P3b cases remain on the
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
| `OptimisticLoop` (the author's reason for switching it on), and the `CollectUnsatCore` purpose | `conf.py` | **P3b**: `state` and `verify` read them |
| `SummaryDirective`, `TuningFiles`, and the rest of the tuning layer | `tuning.py` | **P3b**, as planned |
| Starting-directive additions: Anchor account validation, `system_program::transfer`, `try_borrow_lamports`, and `realloc` moved from summarized to inlined | `envs/*.txt`, `tests/test_cvlr_env_paths.py` | **Held** on the branch, and submitted together later (see *Deferred: the starting tuning directives*) |
| `ProgramModel`, `models` / `companions` replacing `specializations`, and `withholding` (`67f2215e`, `eb18bc80`) | `reference.py`, `tests/test_cvlr_reference.py` | **P7**: `entry.py`'s `--withhold-crate` is the caller |
| The build, conf and submission cases | `tests/test_cvlr_plumbing.py` | **P2** / **P3b**, as planned |

**The manifest-path fix is now one hunk for master.** `2c852922` stops the compile checks naming
their package with a bare `--package <name>`. Cargo refuses that whenever the name is ambiguous. It
is ambiguous whenever the program under verification is *also* a published crate that one of its own
dev-dependencies pulls back in: the local and published copies then share a name at two versions.
`--manifest-path` names exactly one package. Passing it alongside `--package` does not help; that
was measured. #248 landed the `manifest_dir` parameter on `CargoSession.check`, but master's
`preflight.py` still calls `check(package=...)`. The rebased branch's one-line hunk is the fix. The
`verify.py` and `editor.py` halves ride **P3b** and **P5**. Nothing on master exercises the fix,
because no corpus project is a published crate. It was found by pointing preflight at the Solana
stake program (`docs/stake-benchmark.md`). A regression test needs a fixture whose package name
collides with its own graph, and that test is still to write.

### P2 — A hand-written rule in, verdicts out

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
it moves to **P3b**, where `state` and `verify` read it.

Review also took `UnanalyzedCexHandler` out. `run_submission` now requires its caller to choose a
counterexample handler instead of defaulting to one that skips analysis. The end-to-end test,
which reads only verdicts, passes a local stub. The handler's first production caller is P3b's
unsat-core diagnostic run, so it lands there. P2 no longer changes `composer/prover/`.

The tuning-directive additions came out as well. Nothing in P2 reads the starting tuning files: they
reach a build only through the scaffold, and nothing in P2 scaffolds. They are held instead (see
*Deferred: the starting tuning directives*).

Marked `expensive` and skipping, named, when there is no toolchain — so the routine gate still
passes on a machine without one.

### P3a — The working copy and what may be changed in it

`composer/spec/cvlr/{rust_source,munge,tree}.py`.

`rust_source` is the Rust parsing primitive and `munge` is the vocabulary expressed over it; `tree`
is the per-unit working copy that *applies* a munge. They land together because that application is
the usage: the old **C3a** landed the vocabulary with no applier, two PRs before the test that
exercised it. Carries the graphcore pin bump `85be3db` → `9f4e9fc` (graphcore #39), whose only
caller anywhere is `tree.py`.

**`munge.py` lands whole.** It used to be two subjects under one name: pointing a target at the
verification fork of a dependency, and the vocabulary of edits to the program's own functions. The
fork half is now its own module, [forks.py](../composer/spec/cvlr/forks.py) (`plan_overrides`,
`already_patched`, `manifest_additions`), which **P1** carries because `scaffold.py` calls it. So
`munge.py` is only the source-edit vocabulary and arrives here as a new file. The tests divide the
same way: `tests/test_cvlr_forks.py` with P1, `tests/test_cvlr_munge.py` here.

The old plan's worst test placement disappears here. It had `munge.py` landing in C3a and
`tests/test_cvlr_munge.py` landing in **C6**, four PRs and a wave later, on the grounds that the
test reaches the pipeline. It does — in three functions, through imports local to them. Everything
`test_cvlr_munge.py` and `test_cvlr_module_redirect.py` import at module scope is `cargo.metadata`,
`munge` and `tree`, so all of both but those three functions land here, with the code they test.
The three pipeline-reaching functions go to P7.

The one file the P3a/P3b line does cut is `tests/test_cvlr_tree.py`, which imports
`harness.CvlrArtifactStore` at module scope. Decline the split and P3 is one +3,967/+2,280 PR — still
one capability and still runnable, so this is a size judgement rather than a structural one.

### P3b — A generated harness, and what its verdict means

`composer/spec/cvlr/{tuning,state,harness,verify}.py` and `composer/cargo/symbols.py`.

`verify` is the reason `symbols` exists — it matches the functions a built program defines against
the names the prover's tuning files use — and in the old partition they were in **C5b** and **R1**,
two waves apart. `tuning` and `harness` are `verify`'s other two inputs, and `state` is what a unit
carries between rounds. The old **C4a1** split `state` and `harness` from the `verify` that reads
them; this does not.

The largest test set in the wave, for the reason the old plan already gave: this is the layer the
earlier slices' tests were waiting on.

### P4 — The Anchor surface and the worked example

`composer/spec/cvlr/{anchor_surface,example,guidance}.py`.

`anchor_surface` reads the program; `example` renders the worked example against what it read;
`guidance` is the static prose that ships beside it. The old **C5a** put `anchor_surface` with the
prover and **C4b** put `example` two PRs later, which separated the analysis from its only reader.
`tests/test_cvlr_worked_example.py` renders the example against a real analyzed program, so the
usage is in the PR even though the agent that will be *shown* it is P6.

### K1 — A knowledge bundle, and the second instance that justifies it

The old **S6** and **K1** as one PR: `KnowledgeBundle`, a `KBRecipe` generic over its channel
vocabulary, `@cache`s keyed by bundle, `kb_tools(bundle)`, a `kb_index.j2` that does not name CVL in
prose a second corpus would inherit — *and* `CVLR_BUNDLE`, `with_cvlr_context`, `cvlr_kb_tools()`
and `composer/kb/resources/cvlr_baseline_facts.md`, the ~400 lines of run-invariant CVLR and prover
fact factored out of the author's system prompt.

**This is the one PR that lands ahead of its reader on purpose, and the reason is the criterion
itself.** S6 alone is a refactor with one instance, which is the shape the feedback is about: a
reviewer is asked to judge a generalization against a future second user. S6 *with* `CVLR_BUNDLE` is
the generalization and its second instance in the same diff — `test_kb_bundle.py` renders a bundle
that is not CVL's, using the API the way production will. What it lacks is an agent calling
`with_cvlr_context`, and that is P5, immediately after.

Splitting it the other way — bundle to master, CVLR content later — is rule 4's instinct and it is
wrong here: it produces exactly the caller-less abstraction this re-partition exists to stop.
Keeping them together also keeps the two review audiences in one diff, where the CVL owners can see
what their eight agents' context mechanism is being generalized *for*. See
[cvlr-knowledge-bundle-plan.md](./cvlr-knowledge-bundle-plan.md) §2.

### P5 — The munge editor

`composer/spec/cvlr/editor.py`, `composer/cargo/depinfo.py`, and the two prompt templates.

`depinfo` — which sources a build actually compiled, from rustc's `.d` files — has exactly one
caller anywhere, and it is the editor: an edit to a file the `certora` feature gates out does not
reach the build, and the editor is what needs to know. In the old plan `depinfo` was in **R1** and
the editor was in **C3b**, wave 2 and wave 3.

First reader of `with_cvlr_context` (K1), at both of its prompts.

### P6 — The authoring loop

`composer/spec/cvlr/{author,crate_mount,source_tools}.py`, the four authoring prompt templates, and
the CVLR corpus.

The old **C7a** is folded in here, minus one file. Its argument for landing early was that
`cvlr_property_generation_system_prompt.j2` carries an unguarded
`{% include "cvlr_rag_tools.j2" %}`, so C4a2 could not render without it. Under this partition the
prompt and the fragment are the same PR and the constraint disappears. `crate_mount` and
`source_tools` come here rather than with the pipeline that builds them, because
`tests/test_cvlr_knowledge.py` is what exercises them and what they are *for* is the author's
context.

**The corpus half has landed.** [#244](https://github.com/Certora/AutoProver/pull/244) merged on
2026-09-22 as `7d435150`, and it carried `cvlr_rag_tools.j2` despite the advice to hold the template
back for the prompt that includes it. That advice is moot now. The template is on master with no
renderer until this PR. #244 also landed the pieces this section used to list as P6 hunks:
`CVLR_DEFAULT_CONNECTION`, the `cvlr_rag` schema in `init-db.sql`, `ragbuild`'s `<blockquote>`
case, the manual half of `populate_cvlr_rag.sh`, and `tests/test_cvlr_rag.py`. #244 also added
two things the plan had not: `ragbuild --corpus {cvl,extended,cvlr}`, and one search-result
renderer, `composer/rag/render.py`, shared by every corpus. What remains on the branch in those
files is C7b's and C7c's.

### P7 — The backend, the pipeline and the CLI

`composer/spec/cvlr/{pipeline,entry}.py` (the executor half), `composer/cli/console_solana.py`,
`composer/cli/tui_solana.py`, the `CvlrJudge` / `CvlrGeneration` cache markers, and the plumbing
tests.

Still the PR that makes the backend a product, and still last in the wave — but it is now the PR
that *composes* seven working things rather than the PR that makes twelve dead ones reachable. Lands
without the two pinned-run flags; see *Deferred: pinned runs*.

It now also carries three things that were meant to land earlier: the `cvlr-preflight` script (still to
write) and `entry.py`'s parser, both of which P1 merged without; `make_run_link_fetcher`, which S4 can no longer
provide; and the reference set's `withholding`, which `--withhold-crate` is the only caller of.

And it carries S4's give-up reason (`Abandoned`, `GaveUpComponent.reason`, `schema_version` 3.2).
That one is not CVLR-specific: it changes every backend's report, EVM included, and P7's
description has to say so rather than leave a reviewer to find it.

### Order, and what is forced

**~~P1~~ → P2 → P3a → P3b → P4 → K1 → P5 → P6 → P7**

P4 and K1 are the only two with slack: P4 needs `rust_source` (P3a) and nothing after it, and K1
needs nothing in `composer/spec/cvlr/` at all, so either can move earlier if that helps scheduling.
Everything else is the DAG.

Checked rather than assumed: the only modules outside `composer/spec/cvlr/` that import from it are
the two Solana CLI entry points, the tape driver and the env refresher — all of them CVLR-specific
themselves. No shared module reaches into the backend.

**What the re-partition costs.** Four of these — P1, P3a, P3b and P6 — are larger than any PR the
old wave 3 had except C6, and P1 and P6 are larger than C6, which was that wave's biggest. That is
the trade, and it should be made with the number in view: the old wave 3 was 12 PRs averaging 1,900
lines, not one of which a reviewer could run; this is 9 averaging 2,800, each with an exit criterion
in it. If a reviewer wants the smaller unit back, P1 is the place to ask — `scaffold.py` and its env
files are 1,430 of it and could go alone, at the price of landing the scaffold one PR ahead of the
`preflight` that is its only caller, which is the trade this whole re-cut exists to refuse.

**What it does not fix.** `composer/spec/cvlr/` is still eleven layers deep, and a slice still has
to be a *prefix* of that DAG. So P3a's `tree` and P4's `example` still land before the production
code that calls them, and their justification is still a test rather than a caller. The claim this
partition makes is narrower than "everything has a caller": it is that **no PR asks a reviewer to
judge a module with nothing in the diff that uses it**, which is a thing the old partition asked
four times over.

---

## Wave 3 — gates, the corpus's other producers, the container, documentation

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **P8** The end-to-end gate and its scenario | 10 | +2,950 | `test_cvlr_gate.py` and the `solana_vault_idl` Anchor program it runs against. Real models, real cargo, real cloud jobs. Carries the change that makes `token_cost_budget` yield its counter instead of `None`: the gate is its only reader, and it reads it to report what the run cost rather than only to trip on the ceiling. |
| **P9** The replay tape | 7 | +52,251 | The recorded run that lets the gate's shape be re-checked for the price of the builds and prover jobs alone. 51,000 of those lines are one generated file. |
| **R3** A Solana container | 8 | +400 −60 | `scripts/Dockerfile.solana` and `scripts/docker-compose.solana.yml`: a second AutoProver container, `autoprove-solana`, carrying the Rust toolchain and the platform-tools release, layered on the base image — which stays lean and loses nothing but its docs stage, where the one-element `for name in cvl` loop becomes `cvl solana` so the shared postgres gets the manual the backend is for. The shared service body moves to `scripts/docker-compose.common.yml`, which the EVM and Solana services both extend. |
| **C7b** The CVLR crate reference | 3 *(hunks)* | ≈ +80 | Manifest ingestion in `populate_cvlr_rag.sh`: paths given on the command line, else `$CVLR_KB_REPO`, else the installed `certora_cvlr_kb` package. The manifest itself — every public item of the pinned reference set, with compile-gated examples — is produced in the private repo, so what lands here is the discovery and the `rag_import` call. Held back from the corpus because that producer is the part expected to be reworked, and this is the seam it would move. |
| **C7c** The practice manifest | 4 *(hunks)* | ≈ +20 | Project-derived idioms, under the same tag and found by the same discovery C7b lands, so here it is almost only what an unreviewed entry has to make visible: an entry can be `proposed` rather than signed off, and `cvlr_manual_search`'s description says to treat an UNREVIEWED result as a lead worth compiling rather than as authority. |
| **K2** CVLR recipes | ~10 | +121 | `cvlr_recipes_index.yaml` and the recipe bodies, with channels taken from the author's tool list (`RULE` / `MOCK` / `EDIT` / `CONF` / `SKIP`) rather than from the capture taxonomy — a recipe naming an action the agent cannot take cannot be followed. Content for a mechanism K1 shipped and P6 reads, which is why it can land last: its content is whatever survives triage against K1's document, and that triage is work in `certora-cvlr-kb`. |
| **D** Documentation | ~6 | ≈ +4,000 | What is left after each slice carries its own section: the capture plan, the upstream-defect record, this document, and the to-do index. |

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
section boundaries and each part rides the slice it describes; `munge-and-working-copies.md` and
`the-tree-is-a-vfs.md` go with P3a; `who-edits-the-program.md` with P5;
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
| A cloud job failure names its link | `composer/prover/core.py` *(hunk: the `CloudJobError` message)* | A job that fails server-side leaves nothing on disk, so the status alone tells the agent nothing it can act on. An authoring agent then blames its own draft and simplifies it toward a tautology. | One string. Visible on EVM runs. |
| A counterexample analysis claims only what the counterexample shows (`bcfeaaaa`) | `composer/templates/cex_instructions.j2` | The single-shot analyzer called two sibling rules passing controls, inferring their verdict from the absence of a counterexample. Both were SANITY_FAILED. | A CVL prompt change, so it changes EVM runs' counterexample analysis. Its motivation was CVLR, but nothing in it is. |
| A relative main-contract path resolves against the project root | `composer/pipeline/cli.py` *(hunk)* | Running the CLI from outside the project failed outright. | S1 was merged without it. Both readings agree wherever the old one worked. |
| `cli_pipeline` reaches `get_provider_for` through its module | `composer/pipeline/cli.py` *(hunk)* | Both the fake-LLM tape and the tape recorder install themselves by replacing `composer.llm.registry.get_provider_for`. A name bound at import time keeps the original. | **Check master first.** Master's `harness_tape.py` and `record_tape.py` patch that attribute, while master's `cli.py` still binds it at import. If a taped test goes through `cli_pipeline`, it may be calling a real, paid model. P9 needs this regardless. |
| `OVERLAY_OWNED_KEYS` names the right registry | `composer/spec/source/prover.py` *(docstring)* | It says `author.EDITABLE_FLAGS`; the registry has been `author._FLAG_KEYS` for some time. | From the former S5. |
| Two ignored callback parameters typed `object` | `composer/authoring/judge.py`, `composer/spec/cvl_research.py` | `Any` where nothing reads the value. | Annotation only. Could ride with the docstring fix. |

Changes that look shared but are not on this list, because a CVLR slice is their first reader:
`prover/results.py`'s `external_functions` and the alert-report fetch in `cloud.py` (read by
`verify` and carried in the judge's briefing by `state`, **P3b**); `UnanalyzedCexHandler` (the unsat-core diagnostic run, **P3b**); the shared
unsat-core template fragments split out of `sanity_tool_prompt.j2` (the vacuity analysis,
**P3b**); and `make_run_link_fetcher` and the give-up reason (**P7**).

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
together with `rules.py` if P3b has not landed it by then, since the test reads `rule_names`. The
other two directives have no test on the branch, only their commit messages (`efd870b5`,
`e070d019`). Each wants a canary before it is submitted, or the PR description has to argue for it
instead.

While they are held, `tests/test_cvlr_env_paths.py`'s directive counts on the branch differ from
master's. They travel with the directives.

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
registry is `composer/rustapp/adapter.py`, which is the wheel seam. `SolanaToolchain`'s one other
mention anywhere is `tests/test_cvlr_plumbing.py`, which constructs it directly rather than through
the registry, and which travels with P7 regardless. That import is at module scope, so the two
`SolanaToolchain` cases in it have to be dropped along with the module and revived with it — the one
concrete thing this deferral costs, and it costs it in P7 rather than in any of the PRs the module
would otherwise have ridden.

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

The `KNOWLEDGE_BASES` entry is not dropped but deferred to **C7b**, which is where something finally
resolves the tag: a manifest carries its `knowledge_base` in the JSON, and `rag_import` has no other
way to find the database it names. The corpus writes to that database without ever naming it.

`gen_docs.sh`'s `PROVENANCE` stamp defers to **C7b** for the same kind of reason. Its argument is the
one [cvlr-todo.md](./cvlr-todo.md) U6 gives — three manifests built in three places can be three
vintages, and one tag hides the seam — which needs a second manifest to bite. With one source there
is nothing to be out of step with, and nothing reads the stamp: `populate_cvlr_rag.sh` prints it and
the database never records it. It is also not CVLR-specific, since `gen_docs.sh` stamps all four
manuals it builds; if it is wanted for CVL too, rule 4 sends it to master on its own. **R3**'s
Dockerfile writes its own copy in its docs stage and does not call `gen_docs.sh`, so it is unaffected
either way — at the cost of the same `printf` living in two places.

**P7** carries the consequence: no `--rag-corpus`, `cvlr_rag.get_tools()` on a database it opens
itself, and its own handling for a corpus that will not open, since `build_rag_tools`'s
degrade-to-no-RAG path is what it stops using.

---

## Dropped: rescuing a grouping the model encoded as JSON text

Wave 1's S2, now reverted on the branch. A `field_validator` on `GroupingResult.groups` accepted a
string that parses as JSON and is either the group list or the whole `GroupingResult` wrapping it,
returning anything else untouched for pydantic to reject. One Fluid run produced the second shape,
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
run. The crates do reach the author — `cvlr/pipeline.py` builds `cvlr_source_tools` and hands them
through `CvlrDeps` — but by a route that never touches this seam. It is a design for a mount that
was then built another way.

Two defects only a first user would have found, both arguing the same thing. The
`{% if crate_source %}` block lives in the Rust fragment alone, so an EVM caller would have bound
the tools and dropped the statement — exactly the pairing failure `LibrarySource` exists to prevent.
And that fragment names `cvlr_source_*` tools it cannot see, in a file `solana.j2` and `soroban.j2`
both include.

**How to revive it.** Revert `8fec7fad`, which carries the type, the parameter, the prompt plumbing
and the four `test_cvlr_knowledge.py` tests that covered the rendering. The condition is a caller
that actually wants the explorer to read the library — which means resolving the crates before the
source tools are built, or rebuilding the seam against `cvlr/source_tools.py` as it now exists.

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

**2. Does the 5 MB tape belong in the repository?** P9 is the only PR here that a reviewer cannot
read. It is a generated artifact, and the argument for checking it in is that a gate nobody can run
protects nothing — but it is also 51,000 lines in every future clone and diff.

**3. ~~When does the graphcore pin move?~~ Settled: with P3a.** The bump was its own PR (R2) on the
reasoning that it changes the library under every consumer and deserves its own blast radius. It
does not, in practice: graphcore #39 is additive, and the one line it changes in an existing
function gives `fs_tools_layered` a `materializer` factory that defaults to today's behaviour, so no
existing caller moves. Nothing outside `cvlr/tree.py` uses what it adds. A two-file pin bump with no
user is not a review, so it lands with the code that needs it.

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

**7. Does P3 land as one PR or two?** One capability, +3,967 code and +2,280 tests, or two of
roughly half that at the cost of splitting `tests/test_cvlr_tree.py` and
`tests/test_cvlr_module_redirect.py` along the same line. The split is written into the tables above
as P3a/P3b because it is the better default, but it is the one place in the wave where the partition
is a judgement rather than a consequence of the DAG, and a reviewer who would rather read 6,000
lines of one story than two halves of it should say so before the split work is done.

---

## Appendix: the paths each PR takes

What to hand `git checkout eric/solanaProver -- …` after branching from `origin/master`. Every file
the branch changes appears below, except those carrying only the dropped build-environment field.
A path under two PRs, and the shared pipeline files marked *(hunks)*, are the overlaps noted in
wave 1 — take the feature's hunks there, not the whole file. Rows for merged PRs are kept as the
record of what went where; nothing is left to check out for those.

Rows marked *(split)* are files whose contents straddle a slice boundary and have to be cut rather
than moved. That is the bill rule 5 runs up, and it is paid almost entirely in tests, because the
code was already cut this way — the exception is `tuning.py`, whose layer families and
`compose_env` the scaffold needs in P1 and whose summary directives and `TuningFiles` the authoring
loop needs in P3b. It is a small bill: five files, and in three of them the straddling imports are
already local to the functions that need them.

| PR | Paths |
|----|-------|
| S1 *(merged)* | `composer/layout.py` `composer/spec/gen_types.py` `composer/sandbox/recipes.py` `composer/pipeline/ecosystem.py` `composer/foundry/entry.py` `composer/spec/source/autoprove_common.py` `tests/test_fs_forbidden_read.py` `tests/test_sandbox_config.py` `scripts/docker-compose.sandbox.yml` `composer/pipeline/cli.py` *(hunks)* |
| S3 *(merged)* | `composer/certora_env.py` `composer/prover/{certoraRunWrapper,core,ptypes,results}.py` `analyzer/analysis.py` `composer/tools/{prover,thinking}.py` `composer/authoring/buffer.py` `composer/core/context.py` `composer/cvl/tools.py` `composer/workflow/executor.py` `composer/spec/source/{autoprove_common,harness}.py` `composer/spec/source/munge/compile_check.py` `tests/conftest.py` `tests/test_prover_app.py` `tests/test_prover_options.py` `tests/test_wrapped_prover_runner.py` `tests/test_solana_cex_trace.py` `tests/data/solana_cex/` `tests/test_tree_parsing.py` `tests/test_cex_analysis_failure_isolation.py` `tests/test_autoprove_report.py` *(hunks)* |
| S4 *(closed)* | nothing: the give-up reason goes to P7, `job_input` to P3b, the fetcher to P7 |
| P1 *(merged)* | `composer/cargo/{__init__,features,manifest,metadata,session}.py` `composer/spec/cvlr/{__init__,conf,crates,env_paths,forks,preflight,reference,scaffold,tuning}.py` `composer/spec/cvlr/envs/` `composer/spec/cvlr/harness_files/` `composer/prover/conf.py` `composer/spec/source/{prover,author,artifacts}.py` *(hunks)* `composer/spec/natspec/task_description.py` *(hunks)* `tests/test_cvlr_{env_paths,forks,plumbing,preflight,reference,scaffold}.py` `tests/test_prover_conf.py` `tests/test_rules_striping.py` *(hunks)* `tests/test_stuck_rule_warnings.py` *(hunks)* `tests/data/vault_sbf_symbols.txt` `pyproject.toml` `uv.lock` |
| P1 follow-up, to master now | `composer/spec/cvlr/preflight.py` *(hunk: `check(manifest_dir=…)`)* `composer/spec/cvlr/crates.py` *(hunk: drop `roots()`)* — plus the regression test, still to write |
| P2 | `composer/cargo/sbf.py` `composer/spec/cvlr/prover.py` `tests/test_cvlr_end_to_end.py` `tests/test_cvlr_plumbing.py` *(split: `sbf_argv`, the build script, `write_submission`)* |
| Tuning directives *(held)* | `composer/spec/cvlr/envs/` *(hunks)* `tests/test_cvlr_env_paths.py` *(hunk: the directive counts)* `tests/test_cvlr_dropped_writes.py` `tests/data/dropped_writes_probe.rs` |
| P3a | `composer/spec/cvlr/{rust_source,munge,tree}.py` `graphcore` `pyproject.toml` `tests/test_cvlr_munge.py` *(split: the source half, less the three `CvlrFormalizer` cases)* `tests/test_cvlr_module_redirect.py` `tests/test_cvlr_import_swap.py` `tests/test_cvlr_anchor_reach.py` `tests/data/anchor_reach_probe.rs` |
| P3b | `composer/spec/cvlr/{state,harness,verify,rules}.py` `tests/test_cvlr_rules.py` `composer/prover/core.py` *(hunk: `UnanalyzedCexHandler`)* `composer/spec/cvlr/tuning.py` *(split: `SummaryDirective`, `TuningFiles`, appended to what P1 landed)* `composer/spec/cvlr/conf.py` *(hunks: `OptimisticLoop`, `CollectUnsatCore`)* `composer/spec/source/report_prover.py` *(hunks: `job_input`, `fetch_unsat_cores`)* `composer/cargo/symbols.py` `tests/test_cvlr_symbols.py` `tests/test_cvlr_tuning.py` `tests/test_cvlr_tree.py` `tests/test_cvlr_plumbing.py` *(split: `_CaptureCallbacks`, `_RunAccounting`)* |
| P4 | `composer/spec/cvlr/{anchor_surface,example,guidance}.py` `tests/test_cvlr_anchor_surface.py` `tests/test_cvlr_worked_example.py` |
| K1 | `composer/kb/kb_context.py` `composer/kb/knowledge_base.py` `composer/kb/resources/cvlr_baseline_facts.md` `composer/templates/kb_index.j2` `composer/templates/cvl_kb_index.j2` `tests/test_kb_bundle.py` `tests/test_cvlr_bundle.py` |
| P5 | `composer/spec/cvlr/editor.py` `composer/cargo/depinfo.py` `composer/templates/cvlr_munge_editor_system.j2` `composer/templates/cvlr_munge_review_system.j2` `tests/test_cvlr_editor.py` `tests/test_cvlr_derive_swap.py` |
| P6 | `composer/spec/cvlr/{author,crate_mount,source_tools}.py` `composer/templates/cvlr_{feedback_prompt,property_generation_prompt,property_generation_system_prompt,property_judge_system_prompt,source_tools}.j2` `composer/spec/source/source_env.py` `template_manifest.json` `tests/test_cvlr_judge_input.py` `tests/test_cvlr_knowledge.py` `tests/data/cvlr_judge/` |
| P7 | `composer/spec/source/report/{schema,collect,build}.py` *(hunks: `Abandoned`, `reason`, 3.2)* `composer/pipeline/core.py` *(hunks: `_abandonment`)* `tests/test_autoprove_report.py` *(hunks: the give-up cases)* `composer/spec/cvlr/pipeline.py` `composer/spec/cvlr/entry.py` *(the whole file, now including the parser P1 did not take)* the `cvlr-preflight` console script *(new work, still to write)* `composer/spec/cvlr/reference.py` *(hunks: `ProgramModel`, `withholding`)* `tests/test_cvlr_reference.py` *(hunks)* `composer/spec/source/report_prover.py` *(hunk: `make_run_link_fetcher`)* `composer/cli/console_solana.py` `composer/cli/tui_solana.py` `composer/spec/context.py` `composer/spec/services.py` `composer/ui/tool_display.py` `composer/rustapp/toolchain.py` `tests/test_cvlr_entry.py` `tests/test_cvlr_findings.py` `tests/test_cvlr_author.py` `tests/test_cvlr_munge.py` *(split: the three `CvlrFormalizer` cases)* `tests/test_cvlr_judge_round_cost.py` `tests/test_cvlr_plumbing.py` *(the remainder)* `tests/test_autoprove_integration.py` `.github/workflows/integration-tests.yml` |
| P8 | `tests/test_cvlr_gate.py` `test_scenarios/solana_vault_idl/` `composer/diagnostics/budget.py` |
| P9 | `composer/testing/` `scripts/record_cvlr_tape.sh` `tests/test_cvlr_tape.py` `tests/test_tape_setup.py` |
| R3 | `scripts/Dockerfile` `scripts/Dockerfile.solana` `scripts/docker-compose.yml` `scripts/docker-compose.common.yml` `scripts/docker-compose.solana.yml` `scripts/autoprove-entrypoint.sh` `tests/test_cvlr_image.py` |
| C7b | `scripts/populate_cvlr_rag.sh` *(hunks: manifest discovery)* `composer/rag/db.py` *(hunks: the `KNOWLEDGE_BASES` entry)* `scripts/gen_docs.sh` `.gitignore` `composer/templates/cvlr_rag_tools.j2` *(hunks)* `docs/rag-import-format.md` *(hunks: the producer paragraphs in §6 and §7)* |
| C7c | `composer/tools/cvlr_rag.py` *(hunks: the unreviewed disclosure)* `scripts/populate_cvlr_rag.sh` *(hunks)* `composer/templates/cvlr_rag_tools.j2` *(hunks)* `docs/rag-import-format.md` *(hunks)* |
| K2 | `composer/kb/resources/cvlr_recipes_index.yaml` `composer/kb/resources/recipes/cvlr-*.md` |
| D | `docs/` *(less the sections that ride their slice, and less `rag-import-format.md`, which goes with C7b and C7c)* |

---

## Appendix: the old partition, and where it went

The first draft's names appear in commit messages, in two open PR bodies and in
[cvlr-todo.md](./cvlr-todo.md). This is the map.

| Old | Where it went | Why |
|-----|---------------|-----|
| **S6** | K1 | A one-instance refactor; merges with the instance that justifies it. |
| **R1** | P1 *(metadata, session)*, P2 *(sbf)*, P3b *(symbols)*, P5 *(depinfo)* | One directory, four unrelated consumers. The PR the feedback was about. |
| **C1a** | P1 | The conf vocabulary lands with the preflight that writes it. |
| **C1c** | P1 | Scaffold and preflight were already one thing; C1a was their only missing input. |
| **C3a** | P3a, less the fork half to P1 | The munge vocabulary lands with `tree`, which applies it; the dependency-fork half, now `forks.py`, lands with the `scaffold` that calls it. |
| **C2** | P3a *(tree, the graphcore pin)*, P6 *(crate_mount, source_tools)* | The working copy and the agents' view of the crates are two subjects, not one. |
| **C5a** | P2 *(prover)*, P3b *(tuning, less the layer families, which go to P1)*, P4 *(anchor_surface)* | Three modules with three different first callers. |
| **C4a1** | P2 *(rules)*, P3b *(state, harness)* | `rules` belongs with submission; `state` and `harness` belong with the `verify` that reads them. |
| **C5b** | P3b | `verify` lands with its three inputs and with the `symbols` it exists to match against. |
| **C4b** | P4 | The worked example lands with the surface analysis it renders. |
| **C3b** | P5 | Unchanged in content; gains `depinfo`, its only caller. |
| **C4a2** | P6 | Unchanged in content; gains the corpus and the crate mount its prompts name. |
| **C6** | P7 | Unchanged in content, less the argument parser, which goes to P1 with `preflight`. |
| **C7a** | P6 *(the template)*, otherwise unchanged as [#244](https://github.com/Certora/AutoProver/pull/244) | The tools are usable today; only the fragment lacked a renderer. |
| **C7b**, **C7c** | unchanged | |
| **C8a**, **C8b** | P8, P9 | Renamed only. |
| **R3** | unchanged | Still after the CLI exists. |
| **K2** | unchanged | Content for a mechanism K1 ships and P6 reads. |
| **D** | distributed, then D | A document describing code is the clearest case rule 5 has. |
