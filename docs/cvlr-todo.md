# CVLR backend — what is left to do

A scannable index of open CVLR work. Each entry says what the thing is, why it matters, and where
the argument lives. It deliberately does **not** repeat the reasoning: [§7.12 of the backend
plan](./cvlr-backend-plan.md) holds the evidence, the measurements and the failed attempts, and an
item here that matters to you should be read there before it is picked up.

Two kinds of entry appear below. Most are the plan's own items, restated in one paragraph.
**Unfiled** ones are not in the plan at all — they were noticed while doing other work and have no
section anywhere else, so this document is their only record until someone gives them one.

If you want somewhere to start: **U2** decides whether the smoke gate protects anything at all, and
**U7** is a silent-data-loss risk on the same path whose loud half **U4** was; that half is
fixed now, and the fix cannot reach U7.

---

## Unfiled — no section anywhere else

**~~U1. The judge round-cost test bills a real model call on every expensive CI run.~~** —
**fixed.** [test_cvlr_judge_round_cost.py](../tests/test_cvlr_judge_round_cost.py) now carries a
`measurement` mark alongside `expensive`, and the nightly sweep selects `expensive and not
measurement`, so the number is paid for only when someone names the file. Of the three options
recorded here, guarding it like its siblings would have been a lie — it needs neither cargo nor the
platform tools, and skipping for their absence would have made CI's silence mean something it does
not. Retiring it would have thrown away the ability to retake the measurement, which is the only
thing that answers "did the gate regress on a real judge".

**U2. The smoke gate does not run in CI, and nothing says whether that is intended.**
The expensive CI job installs uv, JDK and solc. The Solana platform tools are a Certora-specific
install it has no step for, so [test_cvlr_tape.py](../tests/test_cvlr_tape.py) and
[test_cvlr_gate.py](../tests/test_cvlr_gate.py) both skip — silently, as a pass. The hour-long gate
we built therefore fires only when somebody runs it by hand on a suitably equipped machine. Either
the toolchain goes into that job and the expensive suite grows by about an hour, or CVLR is
deliberately a local-only gate and that decision gets written down. At present it is neither.

**U3. Re-recording the tape has no trigger, no owner and no budget line.**
Tape lanes are keyed by `run_task` task id, so any deliberate change to the pipeline's *shape*
invalidates the tape, while a change to a prompt does not. A re-record costs roughly $213 and four
hours ([scripts/record_cvlr_tape.sh](../scripts/record_cvlr_tape.sh)). Nothing says who decides that
a re-record is due, or how you find out the tape is stale short of an hour-long failure in the
middle of a run.

**~~U4. A dropped prover-API connection has no retry, and it has already cost a unit.~~** —
**fixed on master.** `fetch_sources_and_treeview_files` in
[cloud.py](../composer/prover/cloud.py) was called without a bounded retry, and a transient `Remote
end closed connection without response` lost an entire formalization unit during the first tape
recording. [#223](https://github.com/Certora/AutoProver/pull/223) merged on 2026-09-17: three
attempts with exponential backoff around the fetch of a job that has already polled `SUCCEEDED`,
re-raising at once the four failures a second attempt cannot change. A duplicate written on this
branch was closed in its favour, so a rebase picks the fix up rather than conflicting with it. One
carried comment was not taken: each attempt still starts from an empty destination and re-downloads
what already arrived, rather than resuming from the client library's completion markers.

**~~U5. Landing this branch has no plan.~~** — **written.**
[cvlr-landing-plan.md](./cvlr-landing-plan.md) breaks the branch into PRs that can be reviewed on
their own, shared code first, with a coverage table that accounts for every changed file. What it
leaves open are five decisions it cannot make on its own — the duplicate retry PR, whether the 5 MB
tape belongs in the repository, when the graphcore pin moves, the order against the open findings
PR, and whether any of wave 1 changes EVM behaviour.

**U6. The published-docs half is decided; the rest of the corpus is not.**
The CVLR corpus is produced outside this repo, in the private
[certora-cvlr-kb](https://github.com/Certora/certora-cvlr-kb), by three producers that emit a
self-describing `<kb>.rag.json` and hand it to the generic importer — a deliberate producer/importer
split, argued for in [rag-import-format.md](./rag-import-format.md) §6-7. CVL content is produced two
other ways: in-tree by [ragbuild.py](../composer/scripts/ragbuild.py), which parses sphinx HTML and
writes the DB itself, and separately by [certorag](https://github.com/Certora/certorag).

**Done: the Solana Prover manual is generated here, as it already was.**
[gen_docs.sh](../scripts/gen_docs.sh) has always built `solana.html` alongside `cvl.html` — four
manuals, of which one is published — so the documentation half of the corpus needs no producer
anywhere else. `certora-cvlr-kb` drops `tools/docs_manifest.py` and `cvlr-docs.rag.json`, and the
docs half is ingested in-tree by `ragbuild --knowledge-base cvlr_kb` — it and `rag_import` write
through the same two database calls, so the registry lookup was the only code needed. The shared
HTML parser that existed to let a second repo do this is reverted with it.
This reverses half of [the backend plan](./cvlr-backend-plan.md) §7.3.3, and the reason that section
gave is the cost to watch: three manifests built in three places can be three vintages, and the
corpus carries one tag that hides the seam. The `PROVENANCE` stamp beside the built HTML is what
answers that for the half now built here.

**Still open: the other two manifests.** The crate reference and the project-derived practice
corpus are genuinely expensive to build — an API key, cargo, a model — and nothing about this
decision says where they belong. Nor does it say how any of it relates to `certorag`. The remaining
questions are whether that generator adopts the manifest-and-importer split, whether what is left of
the CVLR corpus belongs in it rather than its own repo, and — if they stay apart — what the boundary
is.

**U7. A partial tree-view fetch reports itself as complete.**
POU's `fetch_job_treeview` downloads each output file in a thread pool and *swallows* per-file
failures — it logs `Warning: Failed to fetch <name>` and then writes the completion marker anyway.
So the connection drop U4 was about is the visible half of the problem: the same blip during the
tree-view half produces no exception at all, just a results directory missing rules, which
[read_and_format_run_result](../composer/prover/results.py) parses as a smaller run. U4's retry
wraps this call and so cannot help — it fires on a raise, and the whole point here is that nothing
raises. Nothing downstream can tell that from a job that genuinely had fewer rules. This is
upstream code (`certora-prover-cli`), so the work is to confirm the reading against the installed version, decide
whether a completeness check belongs on our side, and route it with the rest of
[upstream-defects.md](./upstream-defects.md).

---

## Blocked on upstream

**1. A summarized CPI havocs the caller's deserialized `Account<T>`.**
The most serious constraint on this backend. It defeats any handler that performs a CPI and then
updates its own state, which is most of them. Three remedies were tried and recorded; none works.
What ships is a scope reduction rather than a fix — the prompts teach descending to the program's
own accounting core — and that is only available to programs that have such a core. See
[upstream-defects.md](./upstream-defects.md) P6.

**2. Sixteen upstream defects are filed nowhere.**
[upstream-defects.md](./upstream-defects.md) is a well-evidenced document that no upstream team has
been asked to read. Until these are routed, every one of them is rediscovered by the next engagement.

**3. `mock_fn` cannot reach an inherent method.**
It replaces the item with a `use` statement, and a `use` inside an `impl` block is not a method, so
the editor's sixth munge kind reaches free functions only — while much Solana state logic lives in
`impl` blocks. Two normative projects work around it with a trait carrying the method's name, but
**how that takes precedence over inherent-method resolution is not established**, which is what
blocks copying the technique.

---

## Ours, open

**5. The rest of Phase 7 — user-facing documentation.**
The Docker toolchain, the confinement assertion and the replay tape are all built and the plan's
wording for this item is stale. What is genuinely left is documentation for people outside this
project who want to run the backend.

**6. Cross-unit learning: the measurement was never taken.**
The deterministic half shipped — the authoring prompt's worked example is rendered against the
analyzed program, so a unit reads its own handler and accounts. What it was built to enable was a
measurement: do units still spend turns reaching the program, and is the gated probe half worth its
serial submission? Nobody has looked.

**9. A malformed finding draft is data loss rather than a retry.**
`_one` in [findings.py](../composer/spec/source/report/findings.py) catches every exception, logs a
warning and drops the finding. Observed twice: the model omitted the required `FindingDraft.title`
and two of three findings vanished, on a partly different rule set each run. A structured-output
slip is the most recoverable failure there is, and this converts it into the silent deletion of a
confirmed vulnerability write-up — worse because an empty findings list reads exactly like "no
counterexamples". Wants a bounded re-ask, and a report that says how many drafts failed. This is
shared report code reached through `composer/pipeline/core.py`, so it is not CVLR-specific.

---

## Checks never run against a live run

**10. Multi-variant caching under `cargo certora-sbf`.** Proven on host cargo — a third build across
two unit features ran zero rustc invocations. The SBF triple ought to behave identically and has not
been shown to.

**11. The disposability invariant end to end.** `rm -rf .cvlr_work`, resume, reach the same
submission. Unit-tested, never done live, and the mechanism beneath it has changed since — which
makes the end-to-end form the only check that has not been re-run.

**12. Residue from the VFS migration.** Its task half is now **done**: the persistent materializer
has landed on graphcore master and `pyproject.toml` pins it there, so this is no longer a private
fork of a shared library. Three open questions remain, all answered only by a real run — listing
caches over a directory the build is writing, materialization completeness before the prover globs
the real filesystem, and non-UTF-8 files being outside the model. See
[the-tree-is-a-vfs.md](./the-tree-is-a-vfs.md) §6.

**13. Latency under contention.** One shared tree is predicted to win cold and lose warm. Neither
half has been timed.

---

## Open questions and later phases

**14. Do parametric rules actually cost less than per-handler restatements?** Both forms are offered
and the prompt prefers parametric for cross-handler properties. The cost claim needs runs.

**15. Capture Phase B has not run.** The question ledger exists and no expert time has been spent on
it. Three rule idioms reached the prompt by hand; the general form is still unextracted. See
[cvlr-capture-plan.md](./cvlr-capture-plan.md).

**16. Soroban (Phase 8).** Deliberately untouched until Solana is done. `project_toolchain` still has
no Soroban entry.

---

## Documentation debt

**17.** [munge-and-working-copies.md](./munge-and-working-copies.md) §4 needs rewriting against the
wider corpus survey — its counts are one project's where the evidence is nine of eleven.

**18.** Several shipped changes have no section in the plan: the `composer/layout.py` path
consolidation, `cvlr-spl-token` entering the reference set, and `preflight.select_package`.

**19. The plan has two stale items, and they should be corrected there rather than only here.**
§7.12 item 5 says the replay tape "does not exist" — it exists, is curated and has passed twice.
§7.12 item 12 says the persistent materializer lives on a graphcore branch that must land upstream —
it has landed.
