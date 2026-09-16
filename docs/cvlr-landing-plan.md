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
| **S1** Confined builds: one scratch directory, a readable git config, an unreadable output — [#239](https://github.com/Certora/AutoProver/pull/239), draft | 8 | +265 −22 | Three findings from making Rust builds run under the sandbox, and one story. `composer/layout.py` declares `CERTORA_DIR` / `INTERNAL_DIR` where `composer.sandbox` can import them without pulling in pydantic — the escape suite runs it in a guest that has only pytest. The sandbox's scratch (`CARGO_HOME`, tmp) moves under `INTERNAL_DIR`, and `RUST_FORBIDDEN_READ` withholds that directory anywhere in the tree: a build's private cargo registry was 730 MB, one `list_files` returned 28,904 lines with 28,739 of them from it, and the next request was 2.2M tokens against a 1M limit. And `git_config_ro_paths` grants the global git config, without which libgit2 refuses to open a fully warm cached git dependency and reports it as an offline-mode *network* error. |
| **S2** Rescue a mis-encoded grouping | 1 | +26 −1 | A `field_validator` that accepts the whole grouping object JSON-encoded into its own `groups` field. Observed on a real run; the existing fallback silently flattens a report to one group. |
| **S3** A readable cost budget | 1 | +11 −2 | `token_cost_budget` yields its counter instead of `None`, so a caller can report what it spent rather than only trip on the cap. |
| **S4** A `measurement` pytest mark | 1 | +4 −1 | The nightly expensive sweep selects `expensive and not measurement`, so a test that exists to produce a one-off number is not billed every night. Already committed here as `da211b2a`. |
| **S5** Split the manual parser from the chunker | 5 | +504 −238 | `composer/rag/html_manual.py` — sphinx HTML to a tree of typed blocks, importing neither spaCy nor a DB — with `ragbuild` reduced to the chunking half that drives it. What makes an out-of-tree corpus producer possible at all. |
| **S6** Select the Prover CLI by app | 4 | +203 −30 | `ProverApp = "evm" \| "solana" \| "soroban"`, a registry of entry points, and `certoraRunWrapper` taking the app as `argv[2]`. Everything downstream of submission is already chain-neutral; this is the one place they differ. |
| **S7** Counterexamples, and which frames to show | 10 | +411 −48 | `Counterexample`/`SourceSpan` as data rather than a rendered string, `classify_violation`, and `TraceShape` — per-chain rules for which call-trace frames survive rendering. Carries the Solana treeView fixtures that prove the parser is chain-neutral. |
| **S8** Report: what a component gave up on | 7 | +280 −37 | `Abandoned` replacing a `None` that discarded the reason, `GaveUpComponent.reason`, and the `BuildEnvironment` discriminated union (`ConfinedBuilds \| UnconfinedBuilds \| None`) so a report says how the builds behind its verdicts were confined. Includes the `pipeline/core.py` hook that supplies it. |
| **S9** Pinned runs | 4 | +4752 | `composer/pipeline/pinned.py`: write a run's analysis *and* properties to disk with `--pin-to`, start a later run at formalization with `--properties`. Both halves, because a unit is an index into the analysis. 4,233 of those lines are one checked-in fixture — worth asking whether it belongs in the repo. |
| **S10** A second read-only source mount | 4 | +84 −9 | `build_layered_source_tools` and `LibrarySource` — tools over a library the analyzed project depends on, and the statement that tells an agent they exist, which travel together because either alone is worse than neither. Plus `crate_source` on the code explorer's prompt. Carries a stray docstring correction in `source/prover.py` that belongs nowhere in particular. |

Dependencies inside the wave: **S6** before **S7**, and nothing else. Merging the sandbox work into
one PR removed the wave's other ordering constraint, which had been an artefact of the split rather
than of the code: the forbidden-read test imports the sandbox's own scratch-directory constants, so
the two could never have been reviewed apart.

**A caution about `pipeline/core.py` and `pipeline/cli.py`.** Three of these PRs touch them, each
for its own feature — the `forbidden_read` parameter that lets a caller pass its ecosystem's rule
(S1), the build-environment hook (S8), the pinned fixture (S9). Take the hunks, not the files, and
land them in that order; whichever goes last will want a rebase. One unrelated hunk in `cli.py` is a
genuine bug fix — a main contract path resolved against the process's cwd rather than the project
root — and goes alone rather than riding a themed PR; S1 was opened without it.

---

## Wave 2 — Rust and Solana machinery, still backend-agnostic

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **R1** Cargo, SBF and symbols | 10 | +1552 | `composer/cargo/`: workspace metadata, a build session, dep-info parsing, the SBF toolchain and the symbol reader, plus the `SolanaToolchain` registration in `PROJECT_TOOLCHAINS`. Nothing in it knows what CVLR is; it knows how to build and inspect a Solana crate. |
| **R2** Bump the graphcore pin | 2 | +10 −2 | `85be3db` → `9f4e9fc`, which carries graphcore #39 (incremental VFS dumps into a reused build directory). Affects every consumer of graphcore, so it is its own PR with its own justification, and it is a prerequisite for C2. |
| **R3** The image grows a Rust toolchain | 4 | +281 −32 | Dockerfile, entrypoint and compose changes for the Solana platform tools, and the test that asserts the image has them. Gated behind `SOLANA_TOOLCHAIN` so an EVM-only build does not pay for it. |

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
| **C4b** The prompt corpus | 8 | +3179 | Guidance, the worked example rendered against the analyzed program, and the knowledge tests that pin what the prompts must and must not claim. |
| **C5** Verification and tuning | 10 | +3105 | Submission, the prover-side tuning directives, loop bounds, and the Anchor surface analysis. |
| **C6** Pipeline and entry | 12 | +3091 −6 | `CvlrBackend`, the CLI entry points, the artifact store, and the plumbing tests. The PR that makes the backend exist. |

Order inside the wave: **C1a** before **C1b** (the env refresher imports the scaffold's constants),
**R2** before **C2**, and **C6** last. C3a/C3b, C4a/C4b and C5 are independent of each other.

Checked rather than assumed: the only modules outside `composer/spec/cvlr/` that import from it are
the two Solana CLI entry points, the tape driver and the env refresher — all of them CVLR-specific
themselves. No shared module reaches into the backend.

---

## Wave 4 — corpus, gates, documentation

| PR | Files | Size | What it is |
|----|-------|------|------------|
| **C7** Register the CVLR corpus | 7 | +322 −19 | The `cvlr_kb` knowledge base: the tools module, both registry halves, the DB role, and the populate script. The corpus content itself lives in a separate repo — see U6 in [cvlr-todo.md](./cvlr-todo.md). |
| **C8a** The end-to-end gate and its scenario | 9 | +2945 | `test_cvlr_gate.py` and the `solana_vault_idl` Anchor program it runs against. Real models, real cargo, real cloud jobs. |
| **C8b** The replay tape | 7 | +52251 | The recorded run that lets the gate's shape be re-checked for the price of the builds and prover jobs alone. 51,000 of those lines are one generated file. |
| **D** Documentation | 10 | +8153 | The backend plan, the capture plan, the upstream-defect record, the working-copy and VFS notes, and the to-do index. |

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

**4. [#185](https://github.com/Certora/AutoProver/pull/185) is open and touches the same report
files** as S8. Whichever lands second pays the merge. Worth deciding the order deliberately rather
than discovering it.

**5. Is there an EVM-visible behaviour change anywhere in wave 1?** The claim is no — every seam
either defaults to today's value or is reached only by a caller that does not exist yet on master.
S6 and S7 are where that claim is least obvious and should be reviewed as if it were false.

---

## Appendix: the paths each PR takes

What to hand `git checkout eric/solanaProver -- …` after branching from `origin/master`. Every file
the branch changes appears exactly once below; the four shared pipeline files marked *(hunks)* are
the exception noted in wave 1 — take the feature's hunks, not the whole file.

| PR | Paths |
|----|-------|
| S1 | `composer/layout.py` `composer/spec/gen_types.py` `composer/sandbox/recipes.py` `composer/pipeline/ecosystem.py` `tests/test_fs_forbidden_read.py` `tests/test_sandbox_config.py` `scripts/docker-compose.sandbox.yml` `composer/pipeline/cli.py` *(hunks)* |
| S2 | `composer/spec/source/report/grouping.py` |
| S3 | `composer/diagnostics/budget.py` |
| S4 | `.github/workflows/integration-tests.yml` `pyproject.toml` *(the marker registration only)* |
| S5 | `composer/rag/html_manual.py` `composer/scripts/ragbuild.py` `tests/test_html_manual.py` `docs/rag-import-format.md` `scripts/gen_docs.sh` `.gitignore` |
| S6 | `composer/certora_env.py` `composer/prover/certoraRunWrapper.py` `composer/prover/core.py` `composer/prover/ptypes.py` |
| S7 | `composer/prover/results.py` `analyzer/analysis.py` `tests/test_solana_cex_trace.py` `tests/data/solana_cex/` `tests/test_tree_parsing.py` `tests/test_cex_analysis_failure_isolation.py` |
| S8 | `composer/spec/source/report/{schema,collect,build,render}.py` `composer/spec/source/report_prover.py` `composer/templates/autoprove_report.html.j2` `tests/test_autoprove_report.py` `composer/pipeline/core.py` *(hunks)* |
| S9 | `composer/pipeline/pinned.py` `tests/test_pinned_properties.py` `tests/data/pins/` `composer/pipeline/{core,cli}.py` *(hunks)* |
| S10 | `composer/spec/source/source_env.py` `composer/spec/code_explorer.py` `composer/templates/code_explorer/rust/common_fragment.j2` `composer/spec/source/prover.py` |
| R1 | `composer/cargo/` `composer/rustapp/toolchain.py` `tests/test_cvlr_symbols.py` `tests/data/vault_sbf_symbols.txt` |
| R2 | `graphcore` `pyproject.toml` *(the pin only)* |
| R3 | `scripts/Dockerfile` `scripts/autoprove-entrypoint.sh` `scripts/docker-compose.yml` `tests/test_cvlr_image.py` |
| C1a | `composer/spec/cvlr/{preflight,scaffold,conf,crates}.py` `tests/test_cvlr_scaffold.py` |
| C1b | `composer/spec/cvlr_reference.py` `composer/spec/cvlr/env_paths.py` `composer/spec/cvlr/envs/` `composer/scripts/refresh_cvlr_envs.py` `tests/test_cvlr_env_paths.py` `tests/test_cvlr_reference.py` |
| C2 | `composer/spec/cvlr/{tree,crate_mount,rust_source,source_tools}.py` `composer/templates/cvlr_source_tools.j2` `tests/test_cvlr_tree.py` |
| C3a | `composer/spec/cvlr/munge.py` `composer/templates/cvlr_munge_{editor,review}_system.j2` `tests/test_cvlr_munge.py` `tests/test_cvlr_module_redirect.py` |
| C3b | `composer/spec/cvlr/editor.py` `tests/test_cvlr_editor.py` `tests/test_cvlr_derive_swap.py` |
| C4a | `composer/spec/cvlr/{author,state,rules}.py` `composer/spec/context.py` `composer/templates/cvlr_feedback_prompt.j2` `composer/templates/cvlr_property_judge_system_prompt.j2` `tests/test_cvlr_{author,judge_input,rules}.py` `template_manifest.json` |
| C4b | `composer/spec/cvlr/{guidance,example}.py` `composer/templates/cvlr_property_generation{,_system}_prompt.j2` `tests/test_cvlr_{worked_example,knowledge,judge_round_cost}.py` `tests/data/cvlr_judge/` |
| C5 | `composer/spec/cvlr/{verify,prover,tuning,anchor_surface}.py` `tests/test_cvlr_{tuning,anchor_surface,anchor_reach,loop_bound}.py` `tests/data/{anchor_reach,loop_bound}_probe.rs` |
| C6 | `composer/spec/cvlr/{pipeline,entry,harness,__init__}.py` `composer/cli/{console,tui}_solana.py` `tests/test_cvlr_{entry,plumbing,end_to_end,findings}.py` `tests/conftest.py` `tests/test_autoprove_integration.py` `pyproject.toml` *(the console scripts and package data)* |
| C7 | `composer/tools/cvlr_rag.py` `composer/rag/db.py` `composer/tools/rag_env.py` `composer/scripts/init-db.sql` `scripts/populate_cvlr_rag.sh` `composer/templates/cvlr_rag_tools.j2` `tests/test_rag_env.py` |
| C8a | `tests/test_cvlr_gate.py` `test_scenarios/solana_vault_idl/` |
| C8b | `composer/testing/` `scripts/record_cvlr_tape.sh` `tests/test_cvlr_tape.py` `tests/test_tape_setup.py` |
| D | `docs/` *(except `rag-import-format.md`, which goes with S5)* |
