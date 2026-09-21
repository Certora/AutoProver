# CVLR backend — what is left to do

A scannable index of open CVLR work. Each entry says what the thing is, why it matters, and where
the argument lives. It deliberately does **not** repeat the reasoning: [§7.12 of the backend
plan](./cvlr-backend-plan.md) holds the evidence, the measurements and the failed attempts, and an
item here that matters to you should be read there before it is picked up.

Two kinds of entry appear below. Most are the plan's own items, restated in one paragraph.
**Unfiled** ones are not in the plan at all — they were noticed while doing other work and have no
section anywhere else, so this document is their only record until someone gives them one.

If you want somewhere to start: **U12** is a silent-staleness hole in two shipped munge kinds, and
the one with a decision in it rather than a task. **U2** decides whether the smoke gate protects
anything at all, and
**U7** is a silent-data-loss risk on the same path whose loud half **U4** was; that half is
fixed now, and the fix cannot reach U7. **U8** is the same shape one layer up, in the artifact a
reader actually keeps.

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
docs half is ingested in-tree by `ragbuild --output "$CVLR_DEFAULT_CONNECTION"` — it and
`rag_import` write through the same two database calls, so no new code was needed at all. The shared
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
upstream code (`certora-prover-cli`), so the work is to confirm the reading against the installed
version, decide
whether a completeness check belongs on our side, and route it with the rest of
[upstream-defects.md](./upstream-defects.md).

**U8. A report whose grouping failed does not say so.**
[build.py](../composer/spec/source/report/build.py) degrades to a single `general` bucket when the
grouping LLM raises, when validation rejects the grouping, or when it covers no properties. It
computes a `fallback_reason` describing which, logs it at warning, and puts it in no field of
`AutoProverReport`. A single-group report is a shape a report can legitimately have, so the artifact
is indistinguishable from one whose grouping worked — a run that flattens eighty-nine properties
into `general` reads as a considered editorial choice. The same blindness is why a recurrence of the
mis-encoding behind the reverted S2 (`1df970c8` on `eric/grouping-rescue`) would go unnoticed: the
evidence for it was a log line on somebody's terminal, and the artifact that outlives the terminal
says nothing. The work is to carry the reason onto the schema and render it, which is a
`schema_version` bump, and to decide whether it belongs as a report-level field or as a mark on the
fallback group itself. Kin to U7 — both are a degraded result that presents as a complete one.

**~~U9. Should `optimistic_loop` be the default, as it is on the CVL side?~~** — **resolved: no.**
*The escape hatch shipped; the default stays false. Settled 2026-09-21 on the normative stake-pool verification and two measurements, not on the survey. What is left over is **U10**.*
[`conf.py`](../composer/spec/cvlr/conf.py)'s `TEMPLATE_BASE` sets `"optimistic_loop": False` and
argues it well: the Solana spec template says false, and a survey of 354 confs across fifteen Solana
projects finds it true in exactly one. It assumes a loop's halt conditions rather than proving them,
so it hides any violation reachable only after more iterations. The stated remedies, in order, are
to constrain the loop, munge it, and only then raise `loop_iter` — which is why the default bound is
2 rather than the template's 1. It is also deliberately outside the author's action space:
`adjust_prover_config` moves the loop bound and the solver portfolio, both sound, and its docstring
names `optimistic_loop` as one of the settings kept away precisely because it changes what a green
verdict means.

The EVM side does the opposite without comment.
[`source/prover.py`](../composer/spec/source/prover.py)'s `make_prover_config` sets
`"optimistic_loop": True` on every run, unconditionally, alongside `rule_sanity: "basic"`. Nothing
in either file acknowledges the other, so the divergence is currently a fact about two codebases
rather than a decision anybody made.

**What is new is that the ladder has a rung missing.** The vault run of 2026-09-18
(`cvlr_d71e0ec8c99f`) hit *Unwinding condition in a loop* on all five Deposits rules, three times
over, and the author's diagnosis — recorded in the harness it shipped — is that no finite bound
discharges it: bound 2 reported "Loop Iteration 3", bound 3 reported "Loop Iteration 4", so the
trip count is one the analysis cannot fix. The loop is in `system_instruction::transfer`'s own
`Vec`/bincode construction, evaluated as an argument before the CPI and inlined into the handler,
where no summary directive can name it. Constraining it is not available, munging it is not
available, and raising the bound provably does not terminate. The unit ended up stating its balance
properties over `vault_accounting::apply_deposit` instead, which cost it the handler entirely: what
`deposit` passes on, how often, and to which account are all outside the proof it shipped.

So the question is not only "default or last resort" but "what happens when every sound rung is
exhausted". Three shapes, and the evidence does not yet pick one:

* **Default true, as CVL does.** Cheapest to state, and it would have let today's Deposits rules
  reach their own assertions. It also makes every Solana verdict quietly weaker than the corpus's
  own practice, against a survey that is one-sided.
* **A third editable key, reached after the existing ladder.** Keeps the default honest and gives
  the author a rung it currently does not have. The cost is that `adjust_prover_config`'s promise —
  *every setting here is sound* — stops being true, so the tool would need to distinguish the sound
  edits from the one that invalidates more than the prover stamp, and the report would need to
  carry that distinction to a reader.
* **Neither: make the unwinding condition a first-class outcome.** The pipeline already classifies
  it as prover-generated and excludes it from evidence. It could also be the trigger for a
  `record_skip` naming the loop, which is the honest answer when no sound remedy exists and is what
  the author effectively did by hand.

**The survey is weaker than it reads, by this project's own standard.** The expert-written Solana
specs were authored by a small group who hold strong and shared views about this setting; the CVL
specs had other writers with different preferences, which is where the unconditional true on that
side comes from. So the two numbers are not 354 decisions against one — they are a handful of
opinions, replicated across the confs and projects each author touched, against a differently
composed handful. [cvlr-capture-plan.md](./cvlr-capture-plan.md) §4.5 already says to count this
way: *"same-client projects share tooling, layout and house style — so a convention local to one
team can appear in two repos and read as an industry idiom. Two repos from one client is one vote
plus a note, not two votes; the honest ≥2 bar is two teams."* `conf.py` counts **confs**, two levels
below the unit §4.5 names, and `guidance.py`'s own docstring cites §4.5 for why recurrence alone is
the wrong axis. The argument in that comment should be revised to say what it actually rests on —
the spec template's position, and the judgement of the authors who set it — whichever way U9 is
decided.

That leaves nothing on either side that is evidence about the *decision* rather than about who made
it, so the deciding evidence has to be produced. The cheapest form is the run we already know how to
do: the vault pin re-entered with `optimistic_loop` true, against the same 30 properties, read for
two things — whether the Deposits rules reach their own assertions, and whether any rule that
verifies under it would have failed at a higher bound. Evidence so far in
[cvlr-backend-plan.md](./cvlr-backend-plan.md) §7.6.2 and in `conf.py`'s own commentary.

**Measured, 2026-09-21, and it reverses the expected answer.** One rule — the real `deposit`
handler, `balance_post == balance_pre + amount` over `NativeInt` — submitted four times against the
delivered `Deposits` harness, changing nothing but the conf:

| conf | verdict |
|---|---|
| `loop_iter 2`, `optimistic_loop` off | VIOLATED — *Unwinding condition in a loop* |
| `loop_iter 8`, `optimistic_loop` off | VIOLATED — identical trace; the Prover's advice just says "higher than 8" |
| `loop_iter 2`, `optimistic_loop` **on** | **VERIFIED** |
| the same, with `cvlr_satisfy!(true)` in place of the assertion | VERIFIED — so the path is live and the proof above is not vacuous |

The prediction this was run to test was the opposite: that the assumption would clear the unwinding
condition and land on a rule whose post-state upstream defect P6 had havoced, turning a correctly
excluded non-result into a counterexample against a correct program. It does not. The property is
proved, and the reachability probe rules out the vacuity that would have explained it away.

**What the two sound-looking arguments against it both miss is which loop this is.** The trace names
it `unknown loop source code` inside `vault_program::deposit`: it is `system_instruction::transfer`'s
own bincode/`Vec` construction, serializing a fixed-size payload. It terminates in reality and the
analysis cannot fix the count — which is why no bound discharges it and why assuming it finishes is
assuming something *true*. `conf.py`'s rationale ("hides any violation reachable only after more
iterations") describes a risk that attaches to loops **in the program under verification**. This one
is library plumbing no property here mentions, and without the assumption the handler cannot be
verified at all: both runs of this component descended to `vault_accounting::apply_deposit` and gave
up the handler, which is a real loss of coverage traded for nothing.

So the setting's defect is not that it is unsound in principle — it is that it is **per submission,
not per loop**. There is no way to say "assume the serialization loop terminates, keep asserting the
program's own". That is a good argument for it being an escape hatch rather than a default, which is
what shipped, and a poor one for withholding it, which is what the code used to do.

Two limits on this evidence, both real: it is one property on one target, and it says nothing about
a program whose *own* loops bound its properties — which is the case the survey's authors are most
likely to have had in mind, and the case the spl-stake-pool pin exists to reach.

**Also measured: making the rung reachable is not enough.** Re-running the pinned `Deposits`
component on the shipped code (1h 40m, $45), the author met the unwinding condition, worked the
lower rungs — three `summarize_for_prover` directives, two `code_editor` requests for a substitutable
boundary around the `invoke` — and when those were refused it **skipped rather than climbing the last
rung**: `adjust_prover_config` was called zero times. Its harness header records the diagnosis the
ladder asks for ("a measured result, not a guess: every rule that called the handler came back on the
Prover's own *Unwinding condition in a loop* assertion") and then descends to the accounting core
anyway. Given the probe above shows the rung would have worked, the gap is between what the ladder
authorizes and what the author does with it — the fourth rung is framed by what it costs, and an
author weighing that against a skip takes the skip.

**What settles it: the normative verification of stake-pool solves this without the flag.**
`Certora/solana-program-stake-pool-audit` is the hand-written verification of a program whose
properties genuinely do quantify over a list — *"`add_validator_to_pool` must abort if any entry
already in the list carries the same `vote_account_address`"* — and whose `BigVec::find` is a
`while current != len` scan over exactly that list. It is the case `conf.py`'s rationale describes,
and every conf there that sets the key says `"optimistic_loop": false`, with `loop_iter` at 1 and 2.
What it does instead is mock the scan (`program/src/certora/mocks/big_vec.rs`): the predicate is
evaluated on the first element for real, and for the rest —

```rust
// if second element exists, assume it does not satisfy the predicate
cvlr::cvlr_assume!(!predicate(slice));
```

That is the per-loop granularity the flag lacks, built by hand: one scan, one assumption, written
where a reviewer reads it. It also explains the survey rather than contradicting it — those authors
set the key false because for the case that motivates it they had something better, which is a
different fact from a house preference and a stronger one.

**And this backend already teaches that technique.** The bundle's pointer-analysis section tells the
author, for a variable-length collection, to *"ask the editor to `mock_fn` its accessors — the find /
find_mut / retain methods, not the handler that calls them"* — the same three methods the stake-pool
harness mocks.

So the two cases separate cleanly, and the answer differs by case:

* **A loop the property is about** — a list scan, an accumulation. `mock_fn` is the remedy, it is
  already taught, and it keeps the assumption local and legible. `optimistic_loop` would be a blunt
  substitute for a sharp tool, and applied to the whole submission rather than the loop.
* **A loop no mock can reach** — the vault's, inlined into the handler from
  `system_instruction::transfer` with no nameable symbol. Here the flag is the only remedy, and the
  probe above shows it recovers a real proof of a real property.

Which is an argument for the escape hatch and against the default, on both halves. No spl-stake-pool
run is needed to close this; the artifact answers it.

**Done so far: the third shape, the one that needed code.** `adjust_prover_config` takes a
`SetOptimisticLoop` edit, so the author has the rung and the ladder in the bundle has a fourth step
saying when it is the honest one. The tool's charter no longer claims every setting on it is sound;
it says two are and one is not, and why the unsound one is there. **The default is unchanged** —
`TEMPLATE_BASE` still says false, which is what a test now pins — so this closes the "no rung"
half and leaves the "default or not" half open. The comparison run above is still what decides it,
and `conf.py`'s survey argument still needs the revision described above either way.

**~~U10. There is no munge kind that puts a boundary around a CPI, and the author spends two editor
rounds finding that out.~~** — **built.** The ninth kind is `swap_import`
([munge.py](../composer/spec/cvlr/munge.py), [editor.py](../composer/spec/cvlr/editor.py),
[test_cvlr_import_swap.py](../tests/test_cvlr_import_swap.py)), and
[who-edits-the-program.md](./who-edits-the-program.md) §13 is the design record. What is left over
is **U11**.

The report this came from: on the vault re-run the author asked the code editor twice for a
substitutable boundary around `vault_program::deposit`'s inline `invoke`, was refused both times,
and recorded the reason in its own skip — *"Unstatable without a model of the System Program
transfer CPI, and the program cannot be given one with the munge kinds available."* Two properties
skipped, and the handler given up.

**Both of the separated questions came back yes, and the corpus answered them, not a run.**

*Can a munge name an inline `invoke` at all?* Not by its definition — that is `solana_program`'s,
and LTO had left no symbol to summarize either. But the `use` line that puts the name in scope is
the program's, and swapping it is an item-level edit with a compile gate behind it. `mock_fn`
replaces a definition with a `use`; this replaces a `use` with a `use`.
`solana-program-stake-pool-audit`'s `processor.rs` has written exactly that pair by hand for
`solana_program::msg` since before this backend existed, so the kind was the fifth corpus idiom
rather than an invention — it arrived late because the first four were all about *items*, and this
one is about a call site's view of one.

*Is a CPI stand-in sound enough to publish behind?* Yes, and the framing in the original report was
wrong in a way worth recording. It assumed the stand-in would have to **claim** what the transfer
did to two lamport balances. It does not: it returns `Ok(())` and claims nothing. The value is
entirely in what stops happening — the Prover's unconstrained replacement havocs the *caller's*
deserialized `Account<T>` (`upstream-defects.md` P6), and a stand-in removes that, so the handler's
own bookkeeping becomes provable with real account validation and the real `Context`. That is the
fidelity the shipped P6 remedy gives up by descending to the accounting core. Every CPI mock in the
corpus works this way — `stake-pool`'s `create_stake_account` assumes a shape and returns `Ok(())`;
Fluid's liquidity-layer stand-in reimplements the accounting it wants and nothing else — so the
question of publishability was settled by practice before it was asked here.

The line the author now has to draw, and which the prompts, the reviewer and the judge are all told
to hold: a property about the program's recorded state is sound under a swap; a property about the
lamports the CPI actually moved is **false** under one rather than unproven, and is a skip.

**~~U11. `swap_import` has never been used by an author on a real run.~~** — **run, 2026-09-21.**
Thread `cvlr_e924521c4199` off [vault-deposits.pin.json](../tests/data/pins/vault-deposits.pin.json):
1h36m, nine prover jobs, ~$118. **Seven of nine properties verified, three skipped, none faked** —
including `deposit_credits_exactly_amount`, the property the 2026-09-18 run recorded as unstatable.
[who-edits-the-program.md](./who-edits-the-program.md) §13.4 is the account.

Every layer did its job, which is the part that could not be tested. The author reached the kind from
the prompt *before* its first draft and described the effect it needed without naming the kind; the
editor chose `swap_import` and argued it from the charter; the munge reviewer opened `lib.rs` and
counted call sites instead of accepting the editor's scope claim; the judge blocked a draft of seven
green rules on disclosure and was right to. The author also declined the one cheat available to it —
`deposit(..).unwrap(); cvlr_satisfy!(true)` mapped to an acceptance property — and the judge recorded
that it would have rejected the draft had it been there.

Three things came out of it that are not about the kind, and each has its own home: the two stand-in
constraints are now in the author prompt, the encoding defect the three skips share is **P9** in
[upstream-defects.md](./upstream-defects.md), and the staleness hole the judge found is **U12**.

**U12. A munge that points at an author-written stand-in can have a justification that is no longer
true, and nothing detects it.**
Found by the harness judge on the U11 run, blocking a draft: *"The `invoke` munge record materially
misdescribes the stand-in it redirects to. … The redirect points at whatever the harness currently
defines, so the record is stale."* The author had rewritten the stand-in twice after the munge was
recorded and approved; read against the rules shipped beside it, the stale `why` would have made one
assertion false and another double-count. The same judge found a second stale record in the same
draft.

`ModuleRedirect` and `FunctionExtraction` are immune by construction — their `edit_id` digests the
code they carry, so re-authoring it is a different edit that inherits neither the review nor the
prover stamp. `ImportSwap` and `MockFn` carry a *path* into the author's own harness, which the
author may rewrite freely. **`mock_fn` has shipped with this since the beginning**, so this is not a
regression from the new kind; it is a property of the design that the new kind made visible.

It is a decision rather than a patch, and the options are not equivalent. Digesting the stand-in's
source into `edit_id` invalidates a review on every unrelated edit to that harness file. Digesting
only the named item needs a resolver `munge.py` does not have and would tie it to Rust parsing it has
so far avoided. Prompting the author to `amend_munge` after rewriting a stand-in is what already
happened — one judge round late, and only because that judge chose to reconcile the record against
the artifact. A fourth option is to make the *report* state that a stand-in's description is the
author's claim rather than a checked fact, which is cheap and honest and fixes nothing.
[who-edits-the-program.md](./who-edits-the-program.md) §13.4 has the argument.

**U13. The vault scenario's `withdraw` has an unexercised aliasing case, and the harness judge found
it before we did.**
Not a defect in the backend — a gap in the scenario and in what a rule about it covers. `withdraw`'s
`fee_collector` is an `UncheckedAccount` with no constraint beyond `mut`, so passing the vault itself
as the collector is a legal call and the classic aliasing shape a solvency invariant is asked about.
The U11 run's rule assumed it away (`accts[0].key != accts[2].key`) and the judge flagged the
exclusion as unexplained, noting it had checked the arithmetic and the invariant does still hold
(`cushion' = cushion + fee`). The open question is whether `cvlr_deserialize_nondet_accounts` can
model aliasing at all — it gives each slot its own lamport cell, so two equal keys may not share a
balance — which decides whether the assumption is a narrowing to disclose or a modelling limit to
record. Cheap to settle, and it generalizes: any rule over two accounts of one type has this
question.

---

## Blocked on upstream

**1. A summarized CPI havocs the caller's deserialized `Account<T>`.**
The most serious constraint on this backend. It defeats any handler that performs a CPI and then
updates its own state, which is most of them. Three remedies were tried and recorded; none works.
What ships is a scope reduction rather than a fix — the prompts teach descending to the program's
own accounting core — and that is only available to programs that have such a core. See
[upstream-defects.md](./upstream-defects.md) P6.

**2. An acceptance property cannot be stated at all.**
Second only to item 1, and found the same way — by a run, not by reasoning. A rule that binds a
handler's `Result` and asserts `is_ok()` comes back vacuous, while six rules with an identical
prologue that `.unwrap()` it verify and pass their vacuity checks. So the only way to consume the
result assumes success, which is what an acceptance property is trying to establish. It takes with it
every property enforced by Anchor's generated account validation, whose outcome is unobservable in
the same way and demonstrably unstable across builds. The suspected mechanism is the niche-encoded
`Result<_, anchor_lang::error::Error>` with the Certora fork's unboxed `Error` — the same fork item 1
of *Blocked on upstream* and P1 say we depend on. See [upstream-defects.md](./upstream-defects.md)
P9.

**3. Seventeen upstream defects are filed nowhere.**
[upstream-defects.md](./upstream-defects.md) is a well-evidenced document that no upstream team has
been asked to read. Until these are routed, every one of them is rediscovered by the next engagement.

**4. `mock_fn` cannot reach an inherent method.**
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
