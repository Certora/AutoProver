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
| **S1** Confined builds: one scratch directory, a readable git config, an unreadable output — [#239](https://github.com/Certora/AutoProver/pull/239), draft | 10 | +247 −37 | Three findings from making Rust builds run under the sandbox, and one story. `composer/layout.py` declares `CERTORA_DIR` / `INTERNAL_DIR` where `composer.sandbox` can name them without importing pydantic, which that package stays free of. The sandbox's scratch (`CARGO_HOME`, tmp) moves under `INTERNAL_DIR`; `RUST_FORBIDDEN_READ` withholds that directory — and the entry itself, so graphcore prunes the subtree instead of rejecting a 730 MB registry file by file — wherever it sits; and the rule is read off the ecosystem `cli_pipeline` is handed rather than passed beside it, so the two cannot disagree. And `git_config_ro_paths` grants the global git config, without which libgit2 refuses to open a fully warm cached git dependency and reports it as an offline-mode *network* error. |
| **S2** Rescue a mis-encoded grouping | 1 | +26 −1 | A `field_validator` that accepts the whole grouping object JSON-encoded into its own `groups` field. Observed on a real run; the existing fallback silently flattens a report to one group. |
| **S3** The prover layer learns there is more than one chain — [#240](https://github.com/Certora/AutoProver/pull/240), draft | 16 | +577 −80 | Two halves of one seam. *Which CLI:* `ProverApp` names the three entry points `certora_cli` ships, `import_prover_entry` resolves one honouring `$CERTORA`, and `prover_app` narrows an untrusted string at the single boundary where one arrives. *Which frames:* a counterexample stops being a rendered string and becomes data — trace, assertion, source span — so `classify_violation` can decide whether a violation says anything about the program; `TraceShape` then says which frames of a chain's trace survive rendering. Between those two points nothing learns which chain ran, which is the claim `tests/data/solana_cex` measures. |
| **S4** Report: what a component gave up on, and how its builds were confined — [#241](https://github.com/Certora/AutoProver/pull/241), draft | 8 | +251 −37 | `Abandoned` replacing a `None` that discarded the reason, `GaveUpComponent.reason`, and the `BuildEnvironment` discriminated union (`ConfinedBuilds \| UnconfinedBuilds \| None`) so a report says how the builds behind its verdicts were confined. Includes the `pipeline/core.py` hook that supplies it, and `make_prover_fetcher` typed at `ReportableResult` rather than at CVL — plus `job_input`, the one part of the PR with no caller on master. |
| **S5** A second read-only source mount | 4 | +84 −9 | `build_layered_source_tools` and `LibrarySource` — tools over a library the analyzed project depends on, and the statement that tells an agent they exist, which travel together because either alone is worse than neither. Plus `crate_source` on the code explorer's prompt. Carries a stray docstring correction in `source/prover.py` that belongs nowhere in particular. |

Dependencies inside the wave: none — the one that remained was the CLI seam before the trace
parser, and they are now one PR. Merging the sandbox work into
one PR removed the wave's other ordering constraint, which had been an artefact of the split rather
than of the code: the forbidden-read test imports the sandbox's own scratch-directory constants, so
the two could never have been reviewed apart.

**A caution about `pipeline/core.py` and `pipeline/cli.py`.** Two of these PRs touch them, each for
its own feature — the `ecosystem` parameter the exclusion rule is read off (S1) and the
build-environment hook (S4). Take the hunks, not the files, and land them in that order; whichever
goes second will want a rebase. One unrelated hunk in `cli.py` is a
genuine bug fix — a main contract path resolved against the process's cwd rather than the project
root — and goes alone rather than riding a themed PR; S1 was opened without it.

---

## Wave 2 — Rust and Solana machinery, still backend-agnostic

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **R1** Cargo, SBF and symbols | 10 | +1552 | `composer/cargo/`: workspace metadata, a build session, dep-info parsing, the SBF toolchain and the symbol reader, plus the `SolanaToolchain` registration in `PROJECT_TOOLCHAINS`. Nothing in it knows what CVLR is; it knows how to build and inspect a Solana crate. |
| **R2** Bump the graphcore pin | 2 | +10 −2 | `85be3db` → `9f4e9fc`, which carries graphcore #39 (incremental VFS dumps into a reused build directory). Affects every consumer of graphcore, so it is its own PR with its own justification, and it is a prerequisite for C2. |
| **R3** The image grows a Rust toolchain | 4 | +281 −32 | Dockerfile, entrypoint and compose changes for the Solana platform tools, and the test that asserts the image has them. Gated behind `SOLANA_TOOLCHAIN` so an EVM-only build does not pay for it. The docs stage's one-element `for name in cvl` loop becomes `cvl solana`, so the image's corpus has the manual the backend is for. |

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
| **C2** The working copy and the crate mount | 6 | +1219 | The per-unit working tree, the read-only mount of the CVLR crates the target resolves, and the source tools over both. Needs **R2**. |
| **C3a** The munge vocabulary | 5 | +2982 | The six kinds of source modification a harness may need, and how each is expressed against a Rust crate. |
| **C3b** The munge editor | 3 | +2542 | The agent that proposes and applies them, and the review that accepts or rejects. |
| **C4a** The authoring loop | 10 | +2642 | Author, state, rule extraction, the judge's prompts, and the feedback round. Carries the `CvlrJudge` / `CvlrGeneration` cache markers in `spec/context.py`, which are CVLR-specific and have no business in a shared PR. |
| **C4b** The prompt corpus, and a mark for one-off measurements | 10 | +3186 | Guidance, the worked example rendered against the analyzed program, and the knowledge tests that pin what the prompts must and must not claim. Carries the `measurement` pytest mark and the CI selector `expensive and not measurement`, because `test_cvlr_judge_round_cost.py` — which lands here — is the only test that has it: a mark registered before its first user would deselect nothing and give a reviewer no way to judge the CI change. |
| **C5** Verification and tuning | 10 | +3105 | Submission, the prover-side tuning directives, loop bounds, and the Anchor surface analysis. |
| **C6** Pipeline and entry | 12 | +3083 −6 | `CvlrBackend`, the CLI entry points, the artifact store, and the plumbing tests. The PR that makes the backend exist. Lands without the two pinned-run flags — see *Deferred* below. |

Order inside the wave: **C1a** before **C1b** (the env refresher imports the scaffold's constants),
**R2** before **C2**, and **C6** last. C3a/C3b, C4a/C4b and C5 are independent of each other.

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

## Decisions to make before starting

**1. ~~[#238](https://github.com/Certora/AutoProver/pull/238) duplicates
[#223](https://github.com/Certora/AutoProver/pull/223).~~ Settled: #238 is closed and #223 is the
one that lands, and this branch no longer carries its own copy of the change.** Two things from the
closed PR are still worth raising as review comments on #223: it declined to retry the four failures a second attempt cannot change
(a missing job, a bad token, a malformed reference, an unparseable document), where #223 retries
every exception; and it relied on the client library's own completion markers to resume, where #223
wipes the destination between attempts and re-downloads what already arrived.

**2. Does the 5 MB tape belong in the repository?** C8b is the only PR here that a reviewer cannot
read. It is a generated artifact, and the argument for checking it in is that a gate nobody can run
protects nothing — but it is also 51,000 lines in every future clone and diff.

**3. When does the graphcore pin move?** R2 changes the library under every consumer. Landing it
early unblocks C2 and gives the bump its own blast radius; landing it late keeps master still while
the shared seams go in.

**4. Three open PRs touch the same files as S4.**
[#185](https://github.com/Certora/AutoProver/pull/185) and
[#232](https://github.com/Certora/AutoProver/pull/232) are in the report package itself;
[#228](https://github.com/Certora/AutoProver/pull/228) is in `pipeline/core.py`, a few lines from
the give-up boundary S4 retypes. Whichever lands second pays the merge in each pair. Worth deciding
the order deliberately rather than discovering it.

**5. Is there an EVM-visible behaviour change anywhere in wave 1?** The claim is *nearly* no —
every seam either defaults to today's value or is reached only by a caller that does not exist yet
on master. **S3 is the exception and should be reviewed as if the claim were false**: every EVM
trace now renders through `TraceShape`, and `cex_dump` becomes a derived property whose text gains a
`<counterexample>` envelope, which two report tests had to be updated for. The CLI-selection half of
that same PR is inert by comparison, and its body separates the two for exactly this reason.

---

## Appendix: the paths each PR takes

What to hand `git checkout eric/solanaProver -- …` after branching from `origin/master`. Every file
the branch changes appears exactly once below; the four shared pipeline files marked *(hunks)* are
the exception noted in wave 1 — take the feature's hunks, not the whole file.

| PR | Paths |
|----|-------|
| S1 | `composer/layout.py` `composer/spec/gen_types.py` `composer/sandbox/recipes.py` `composer/pipeline/ecosystem.py` `tests/test_fs_forbidden_read.py` `tests/test_sandbox_config.py` `scripts/docker-compose.sandbox.yml` `composer/pipeline/cli.py` *(hunks)* |
| S2 | `composer/spec/source/report/grouping.py` |
| S3 | `composer/certora_env.py` `composer/prover/{certoraRunWrapper,core,ptypes,results}.py` `analyzer/analysis.py` `tests/test_prover_app.py` `tests/test_solana_cex_trace.py` `tests/data/solana_cex/` `tests/test_tree_parsing.py` `tests/test_cex_analysis_failure_isolation.py` `tests/test_autoprove_report.py` *(hunks: the `_violated` helper and its expectation)* |
| S4 | `composer/spec/source/report/{schema,collect,build,render}.py` `composer/spec/source/report_prover.py` `composer/templates/autoprove_report.html.j2` `tests/test_autoprove_report.py` `composer/pipeline/core.py` *(hunks)* |
| S5 | `composer/spec/source/source_env.py` `composer/spec/code_explorer.py` `composer/templates/code_explorer/rust/common_fragment.j2` `composer/spec/source/prover.py` |
| R1 | `composer/cargo/` `composer/rustapp/toolchain.py` `tests/test_cvlr_symbols.py` `tests/data/vault_sbf_symbols.txt` |
| R2 | `graphcore` `pyproject.toml` *(the pin only)* |
| R3 | `scripts/Dockerfile` `scripts/autoprove-entrypoint.sh` `scripts/docker-compose.yml` `tests/test_cvlr_image.py` |
| C1a | `composer/spec/cvlr/{preflight,scaffold,conf,crates}.py` `tests/test_cvlr_scaffold.py` |
| C1b | `composer/spec/cvlr_reference.py` `composer/spec/cvlr/env_paths.py` `composer/spec/cvlr/envs/` `composer/scripts/refresh_cvlr_envs.py` `tests/test_cvlr_env_paths.py` `tests/test_cvlr_reference.py` |
| C2 | `composer/spec/cvlr/{tree,crate_mount,rust_source,source_tools}.py` `composer/templates/cvlr_source_tools.j2` `tests/test_cvlr_tree.py` |
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
