# Landing the CVLR backend

`eric/solanaProver` is 128 commits over 165 files — about 95,000 inserted lines, of which one
recorded tape is 51,000 and one pinned fixture is 4,200. It cannot be reviewed as a branch. This
document breaks it into pull requests that each stand on their own, ordered so that the work with
no CVLR dependency lands first.

It is a decomposition, not a schedule. Nothing here says who does the work or when, and the sizes
are the argument for the split rather than an estimate of effort.

---

## Four rules, one of them learned the expensive way

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

**4. From here on, shared-code work goes to master directly.** The rough-draft gate fix
([#233](https://github.com/Certora/AutoProver/pull/233)) was written on this branch and landed on
master as its own PR, and the branch then dropped it in a rebase at no cost. That is the pattern.
Every shared fix that instead waits on this branch is a rebase conflict being saved up.

---

## Wave 1 — shared code, no CVLR dependency

These are reviewable by anyone who knows the subsystem, and none of them mentions Solana except in
motivation. Sizes are insertions/deletions against master.

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **S1** Confined builds: one scratch directory, a readable git config, an unreadable output — [#239](https://github.com/Certora/AutoProver/pull/239), **merged** `27f2fe93` | 10 | +247 −37 | Three findings from making Rust builds run under the sandbox, and one story. `composer/layout.py` declares `CERTORA_DIR` / `INTERNAL_DIR` where `composer.sandbox` can name them without importing pydantic, which that package stays free of. The sandbox's scratch (`CARGO_HOME`, tmp) moves under `INTERNAL_DIR`; `RUST_FORBIDDEN_READ` withholds that directory — and the entry itself, so graphcore prunes the subtree instead of rejecting a 730 MB registry file by file — wherever it sits; and the rule is read off the ecosystem `cli_pipeline` is handed rather than passed beside it, so the two cannot disagree. And `git_config_ro_paths` grants the global git config, without which libgit2 refuses to open a fully warm cached git dependency and reports it as an offline-mode *network* error. |
| **S3** The prover layer learns there is more than one chain — [#240](https://github.com/Certora/AutoProver/pull/240), **merged** `568ba02d` | 28 | +703 −164 | Three parts, two of them one seam. *Which CLI:* `ProverApp` names the three entry points `certora_cli` ships, `import_prover_entry` resolves one honouring `$CERTORA`, and `prover_app` narrows an untrusted string at the single boundary where one arrives. *Which frames:* a counterexample stops being a rendered string and becomes data — trace, assertion, source span — so `classify_violation` can decide whether a violation says anything about the program; `TraceShape` then says which frames of a chain's trace survive rendering. Between those two points nothing learns which chain ran, which is the claim `tests/data/solana_cex` measures. *How a run is configured:* `ProverOptions` carries the app, the server and the Prover's budget as fields, instead of a `list[str]` of CLI flags it read its own meaning back out of, and one `ProverOptions` reaches the codegen tool rather than being reassembled from parts. That part grew out of the review, and is most of the difference between the size this PR opened at and its size now. |
| **S4** Report: what a component gave up on — [#241](https://github.com/Certora/AutoProver/pull/241), open | 6 | +160 −32 | `Abandoned` replacing a `None` that discarded the reason, and `GaveUpComponent.reason` where it lands — the only change in wave 1 that alters an EVM run's output. Plus `make_prover_fetcher` typed at `ReportableResult` rather than at CVL, and `job_input`, the one part of the PR with no caller on master: POU cannot parse a Solana job link, and the best-effort fetch turns that into every rule UNKNOWN. |

**Where the wave stands.** S1 and S3 merged on 2026-09-17, in that order, half an hour apart. S3
answered a changes-requested review by absorbing the `ProverOptions` rework rather than by argument,
which is most of why it nearly doubled between opening and merging. S2 is dropped (below). S4 is the
one PR still open, unreviewed. S5 is gone: its mount is dropped (below), its one live function
folds into C2, and its docstring fix goes to master on its own — so wave 1 ends with S4.

Dependencies inside the wave: none — the one that remained was the CLI seam before the trace
parser, and they were one PR. Merging the sandbox work into
one PR removed the wave's other ordering constraint, which had been an artefact of the split rather
than of the code: the forbidden-read test imports the sandbox's own scratch-directory constants, so
the two could never have been reviewed apart.

**What S1 and S3 left for S4 to rebase past.** Both landed in `pipeline/core.py` (S1's `ecosystem`
parameter, a few lines from the give-up boundary S4 retypes) and in `tests/test_autoprove_report.py`
(S3's `cex_dump` envelope, in a file S4 otherwise rewrites). `git merge-tree` says #241 still merges
into master without textual conflict, but it has not been re-gated against the new head and should
be before it is reviewed. The `autoprove_common.py` collision between S1 and S3 — the `EVM` argument
to `cont` and `app=EVM.name` on the options built three lines above it — resolved itself in the
merge order.

Two unrelated fixes are still unlanded and go alone rather than riding a themed PR. One hunk in
`cli.py` resolves a main contract path against the process's cwd rather than the project root; S1
was merged without it. And `source/prover.py`'s `OVERLAY_OWNED_KEYS` docstring names the author's
flag registry as `author.EDITABLE_FLAGS`, which has been `author._FLAG_KEYS` on master for some
time — the only part of the former S5 that master can use today.

---

## Wave 2 — Rust and Solana machinery, still backend-agnostic

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **R1** Cargo, SBF and symbols — [#243](https://github.com/Certora/AutoProver/pull/243), open | 8 | +1242 | `composer/cargo/`: workspace metadata, a build session, the SBF build, dep-info parsing and the symbol reader. Nothing in it knows what CVLR is; it knows how to build and inspect a Solana crate. Eight new files and no change to an existing one — the `PROJECT_TOOLCHAINS` registration that would have made it nine went to *Deferred* below. |

---

## Wave 3 — the CVLR backend

Every PR here is `composer/spec/cvlr/` plus its own tests, and each is unit-tested in isolation —
which is what makes the split possible. It is also the honest objection to the split: **nothing in
C1–C5 is reachable by a user until C6 wires it up.** The alternative is one PR of roughly 23,000
lines, which is not a review. The recommendation is to accept a few weeks of unreachable-but-tested
code and land C6 last, after which the backend appears all at once and works.

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **C1a** Preflight, scaffold, conf | 5 | +2611 | Selecting the package under verification, scaffolding a CVLR workspace into a project that has none, and generating the prover conf. |
| **C1b** The reference set and its envs | 10 | +1453 | Which cvlr / cvlr-solana versions a run is bound to, the checked-in inlining and summary env files, and the script that refreshes them. |
| **C2** The working copy and the crate mount | 9 | +1253 −2 | The per-unit working tree, the read-only mount of the CVLR crates the target resolves, and the source tools over both. Carries the graphcore pin bump `85be3db` → `9f4e9fc` (graphcore #39, incremental VFS dumps into a reused build directory) and `build_layered_source_tools`. Both are shared code whose only caller anywhere is in this PR — `cvlr/tree.py` is the sole user of `PersistentMaterializer` and `DictBackend`, `cvlr/entry.py` the sole user of the layered builder — and a seam reviewed beside its caller beats the same seam alone a wave earlier. |
| **C3a** The munge vocabulary | 5 | +2982 | The six kinds of source modification a harness may need, and how each is expressed against a Rust crate. |
| **C3b** The munge editor | 3 | +2542 | The agent that proposes and applies them, and the review that accepts or rejects. |
| **C4a** The authoring loop | 10 | +2642 | Author, state, rule extraction, the judge's prompts, and the feedback round. Carries the `CvlrJudge` / `CvlrGeneration` cache markers in `spec/context.py`, which are CVLR-specific and have no business in a shared PR. |
| **C4b** The prompt corpus, and a mark for one-off measurements | 10 | +3186 | Guidance, the worked example rendered against the analyzed program, and the knowledge tests that pin what the prompts must and must not claim. Carries the `measurement` pytest mark and the CI selector `expensive and not measurement`, because `test_cvlr_judge_round_cost.py` — which lands here — is the only test that has it: a mark registered before its first user would deselect nothing and give a reviewer no way to judge the CI change. |
| **C5** Verification and tuning | 10 | +3105 | Submission, the prover-side tuning directives, loop bounds, and the Anchor surface analysis. |
| **C6** Pipeline and entry | 12 | +3083 −6 | `CvlrBackend`, the CLI entry points, the artifact store, and the plumbing tests. The PR that makes the backend exist. Lands without the two pinned-run flags — see *Deferred: pinned runs* below. |
| **R3** A Solana container | 8 | +400 −60 | `scripts/Dockerfile.solana` and `scripts/docker-compose.solana.yml`: a second AutoProver container, `autoprove-solana`, carrying the Rust toolchain and the platform-tools release, layered on the base image — which stays lean and loses nothing but its docs stage, where the one-element `for name in cvl` loop becomes `cvl solana` so the shared postgres gets the manual the backend is for. The shared service body moves to `scripts/docker-compose.common.yml`, which the EVM and Solana services both extend. |

Order inside the wave: **C1a** before **C1b** (the env refresher imports the scaffold's constants),
then **C6**, then **R3**. C3a/C3b, C4a/C4b and C5 are independent of each other.

**R3** was wave 2, and goes last instead — after **C6**, not merely after **C1a**. C1a is the hard
floor: `tests/test_cvlr_image.py` imports `composer.spec.cvlr.conf` to check that the image bakes
the platform-tools version the conf template asks for. But the container exists to run
`console-solana`, which is not a console script until C6, and nothing builds the image in CI, so
landing it earlier would ship an entrypoint guarding a command that does not exist and would not
even buy a build-breakage signal in exchange. It is the one PR in this wave that touches no
`composer/spec/cvlr/` file.

The container layout is the one `eric/crucible-app` already ships: one lean base image, and a
sibling image per toolchain layered on it with `FROM ${BASE_IMAGE}`. Crucible, Solana and a future
Soroban are then peers rather than flags on a single image, and none of them obliges an EVM run to
carry a toolchain it never invokes. The two branches disagreed on where the toolchain goes — under
`$HOME`, which the base image makes world-writable, or under `$AUTOPROVE_HOME`, root-owned and
world-readable. Solana's answer is the one to follow: a read-only Landlock grant over a tree the
confined build can also write grants nothing.

Checked rather than assumed: the only modules outside `composer/spec/cvlr/` that import from it are
the two Solana CLI entry points, the tape driver and the env refresher — all of them CVLR-specific
themselves. No shared module reaches into the backend.

---

## Wave 4 — corpus, gates, documentation

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **C7** Register the CVLR corpus, and build its documentation half here | 11 | +390 −30 | The `cvlr_kb` knowledge base: the tools module, both registry halves, the DB role, and the populate script — which ingests the Solana manual `gen_docs.sh` already builds, through `ragbuild --knowledge-base cvlr_kb`. No manifest and no second producer for that half (U6); the crate reference and practice manifests still come from the private repo. Carries the `PROVENANCE` stamp in `gen_docs.sh`, the `<blockquote>` case the Solana manual needs, and the `rag-import-format.md` revisions. |
| **C8a** The end-to-end gate and its scenario | 10 | +2956 | `test_cvlr_gate.py` and the `solana_vault_idl` Anchor program it runs against. Real models, real cargo, real cloud jobs. Carries the change that makes `token_cost_budget` yield its counter instead of `None`: the gate is its only reader, and it reads it to report what the run cost rather than only to trip on the ceiling. |
| **C8b** The replay tape | 7 | +52251 | The recorded run that lets the gate's shape be re-checked for the price of the builds and prover jobs alone. 51,000 of those lines are one generated file. |
| **D** Documentation | 10 | +8153 | The backend plan, the capture plan, the upstream-defect record, the working-copy and VFS notes, and the to-do index. |

---

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

* **C6** lands without `--pin-to` and `--properties` and without the `load_pinned_run` import —
  roughly eight lines of `composer/spec/cvlr/entry.py`. No test exercises them, so nothing else moves.
* The `pinned` / `pin_to` parameters stay out of `cli_pipeline` and `run_pipeline`, which is why the
  shared-pipeline caution above names two PRs rather than three.
* `tests/test_pinned_properties.py` and `tests/data/pins/` travel with the feature if it ever lands.

If it is dropped instead, the same paths are what to delete from the branch.

---

## Deferred: Solana's entry in the project-toolchain registry

`composer/cargo/toolchain.py` — `SolanaToolchain`, answering the two questions
`composer.rustapp.toolchain` asks a chain (which crate owns a file, and prepare a workspace) — and
the `PROJECT_TOOLCHAINS` entry binding it. R1 was assembled with both and they came back out.

**It serves the Rust wheel path, not this branch.** The CVLR modules import `composer.cargo`'s
`metadata`, `sbf`, `session`, `symbols` and `depinfo`, and never `toolchain`; the only reader of the
registry is `composer/rustapp/adapter.py`, which is the wheel seam. `SolanaToolchain`'s one other
mention anywhere is `tests/test_cvlr_plumbing.py`, which constructs it directly rather than through
the registry, and which travels with C6 regardless.

**And it is a refactor with an owner already.**
[#98](https://github.com/Certora/AutoProver/pull/98) registers `{"solana": _Solana()}` — a lazy shim
over a `SolanaToolchain` in `composer.spec.solana.project`. So this was Crucible's toolchain lifted
into shared code, and landing it from here would put a second implementation on master with no
caller, on the line of the file that PR changes. It belongs with whatever moves Crucible off its own
copy; `d8ebccb7` on `eric/cargo-sbf` is the removal to revert for the text.

Keeping it out also leaves R1 purely additive, which is most of what makes a 1,392-line PR readable.

---

## Dropped: reporting whether the builds were confined

`AutoProverReport.build_environment` — `ConfinedBuilds | UnconfinedBuilds | None`, a `Builds` row in
the report header, a banner when a run was unconfined, and the `Formalizer.build_environment()` hook
that supplies it. It was written on the reasoning that an unconfined build makes every verdict in
the document a development result, and that stderr on the machine that ran it is not a record.

**It is not worth a schema field.** Nothing reads it, and no formalizer on this branch overrides the
hook either — so the field renders absent on every run that exists, here as much as on master. S4
was opened with it and the field was removed before review.

The branch still carries it in `report/schema.py`, `report/render.py`, `autoprove_report.html.j2`,
`pipeline/core.py` and three render tests; those are what to delete. `docs/cvlr-backend-plan.md`
mentions it in the record of a run that actually happened and should be left alone.

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

**2. Does the 5 MB tape belong in the repository?** C8b is the only PR here that a reviewer cannot
read. It is a generated artifact, and the argument for checking it in is that a gate nobody can run
protects nothing — but it is also 51,000 lines in every future clone and diff.

**3. ~~When does the graphcore pin move?~~ Settled: with C2.** The bump was its own PR (R2) on the
reasoning that it changes the library under every consumer and deserves its own blast radius. It
does not, in practice: graphcore #39 is additive, and the one line it changes in an existing
function gives `fs_tools_layered` a `materializer` factory that defaults to today's behaviour, so no
existing caller moves. Nothing outside `cvlr/tree.py` uses what it adds. A two-file pin bump with no
user is not a review, so it lands with the code that needs it.

**4. Three open PRs touch the same files as S4.**
[#185](https://github.com/Certora/AutoProver/pull/185) and
[#232](https://github.com/Certora/AutoProver/pull/232) are in the report package itself;
[#228](https://github.com/Certora/AutoProver/pull/228) is in `pipeline/core.py`, a few lines from
the give-up boundary S4 retypes. All three are still open. Whichever lands second pays the merge in
each pair, and S4 now also carries whatever S1 and S3 left in those files. Worth deciding the order
deliberately rather than discovering it.

**5. Is there an EVM-visible behaviour change anywhere in wave 1?** Two, and both PRs said so in
their own bodies rather than leaving a reviewer to find it. **S3** was the larger and is now on
master: every EVM trace renders through `TraceShape`, and `cex_dump` is a derived property whose
text gained a `<counterexample>` envelope, which two report tests had to be updated for. **S4** is
smaller and deliberate: a component that gives up now records its reason in the report, on EVM runs
as much as any other. Everything else in the wave either defaults to today's value or is reached
only by a caller that does not exist yet on master.

---

## Appendix: the paths each PR takes

What to hand `git checkout eric/solanaProver -- …` after branching from `origin/master`. Every file
the branch changes appears below, except those carrying only the dropped build-environment field.
A path under two PRs, and the shared pipeline files marked *(hunks)*, are the overlaps noted in
wave 1 — take the feature's hunks there, not the whole file. Rows for merged PRs are kept as the
record of what went where; nothing is left to check out for those.

| PR | Paths |
|----|-------|
| S1 *(merged)* | `composer/layout.py` `composer/spec/gen_types.py` `composer/sandbox/recipes.py` `composer/pipeline/ecosystem.py` `composer/foundry/entry.py` `composer/spec/source/autoprove_common.py` `tests/test_fs_forbidden_read.py` `tests/test_sandbox_config.py` `scripts/docker-compose.sandbox.yml` `composer/pipeline/cli.py` *(hunks)* |
| S3 *(merged)* | `composer/certora_env.py` `composer/prover/{certoraRunWrapper,core,ptypes,results}.py` `analyzer/analysis.py` `composer/tools/{prover,thinking}.py` `composer/authoring/buffer.py` `composer/core/context.py` `composer/cvl/tools.py` `composer/workflow/executor.py` `composer/spec/source/{autoprove_common,harness}.py` `composer/spec/source/munge/compile_check.py` `tests/conftest.py` `tests/test_prover_app.py` `tests/test_prover_options.py` `tests/test_wrapped_prover_runner.py` `tests/test_solana_cex_trace.py` `tests/data/solana_cex/` `tests/test_tree_parsing.py` `tests/test_cex_analysis_failure_isolation.py` `tests/test_autoprove_report.py` *(hunks: the `_violated` helper and its expectation)* |
| S4 | `composer/spec/source/report/{schema,collect,build}.py` `composer/spec/source/report_prover.py` `tests/test_autoprove_report.py` `composer/pipeline/core.py` *(hunks)* |
| R1 | `composer/cargo/` *(less `toolchain.py`)* `tests/test_cvlr_symbols.py` `tests/data/vault_sbf_symbols.txt` |
| R3 | `scripts/Dockerfile.solana` `scripts/docker-compose.{common,solana}.yml` `scripts/Dockerfile` *(hunks: the docs stage and the header)* `scripts/docker-compose.yml` `scripts/docker-compose.sandbox.yml` `scripts/autoprove-entrypoint.sh` `tests/test_cvlr_image.py` |
| C1a | `composer/spec/cvlr/{preflight,scaffold,conf,crates}.py` `tests/test_cvlr_scaffold.py` |
| C1b | `composer/spec/cvlr_reference.py` `composer/spec/cvlr/env_paths.py` `composer/spec/cvlr/envs/` `composer/scripts/refresh_cvlr_envs.py` `tests/test_cvlr_env_paths.py` `tests/test_cvlr_reference.py` |
| C2 | `composer/spec/cvlr/{tree,crate_mount,rust_source,source_tools}.py` `composer/templates/cvlr_source_tools.j2` `tests/test_cvlr_tree.py` `composer/spec/source/source_env.py` *(hunks: `build_layered_source_tools`)* `graphcore` `pyproject.toml` *(the pin only)* |
| C3a | `composer/spec/cvlr/munge.py` `composer/templates/cvlr_munge_{editor,review}_system.j2` `tests/test_cvlr_munge.py` `tests/test_cvlr_module_redirect.py` |
| C3b | `composer/spec/cvlr/editor.py` `tests/test_cvlr_editor.py` `tests/test_cvlr_derive_swap.py` |
| C4a | `composer/spec/cvlr/{author,state,rules}.py` `composer/spec/context.py` `composer/templates/cvlr_feedback_prompt.j2` `composer/templates/cvlr_property_judge_system_prompt.j2` `tests/test_cvlr_{author,judge_input,rules}.py` `template_manifest.json` |
| C4b | `composer/spec/cvlr/{guidance,example}.py` `.github/workflows/integration-tests.yml` `pyproject.toml` *(the marker registration only)* `composer/templates/cvlr_property_generation{,_system}_prompt.j2` `tests/test_cvlr_{worked_example,knowledge,judge_round_cost}.py` `tests/data/cvlr_judge/` |
| C5 | `composer/spec/cvlr/{verify,prover,tuning,anchor_surface}.py` `tests/test_cvlr_{tuning,anchor_surface,anchor_reach,loop_bound}.py` `tests/data/{anchor_reach,loop_bound}_probe.rs` |
| C6 | `composer/spec/cvlr/{pipeline,entry,harness,__init__}.py` `composer/cli/{console,tui}_solana.py` `tests/test_cvlr_{entry,plumbing,end_to_end,findings}.py` `tests/conftest.py` `tests/test_autoprove_integration.py` `pyproject.toml` *(the console scripts and package data)* |
| C7 | `composer/tools/cvlr_rag.py` `composer/rag/db.py` `composer/tools/rag_env.py` `composer/scripts/init-db.sql` `scripts/populate_cvlr_rag.sh` `composer/templates/cvlr_rag_tools.j2` `tests/test_rag_env.py` `scripts/gen_docs.sh` `.gitignore` `composer/scripts/ragbuild.py` `docs/rag-import-format.md` |
| C8a | `tests/test_cvlr_gate.py` `test_scenarios/solana_vault_idl/` `composer/diagnostics/budget.py` |
| C8b | `composer/testing/` `scripts/record_cvlr_tape.sh` `tests/test_cvlr_tape.py` `tests/test_tape_setup.py` |
| D | `docs/` *(except `rag-import-format.md`, which goes with C7)* |
