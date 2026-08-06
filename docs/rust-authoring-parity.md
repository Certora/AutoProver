# Proposal: one authoring workflow for CVL, Foundry and Rust wheels

Status: proposal, for the Rust-framework PR on `eric/rust`. Transient — §6 step 4 folds it into
[rust-applications.md](rust-applications.md) and deletes it, so nothing here should be written as
though it will outlive the branch.

## 1. The diagnosis

The Rust loop and the two Python authoring workflows are not two implementations of the same
shape — they are two *different* shapes, and that is where every gap on the review list comes from.

**The Python apps run one stateful agent session, gated by stamps.** The agent owns a buffer
(`curr_spec` / `curr_test`), edits it surgically, calls a checker tool (`verify_spec` /
`forge_test`) and a judge tool (`feedback_tool`) which *stamp a digest of the current buffer* into
`validations`, and finally calls `result` — which is rejected unless every required stamp matches
the buffer as it stands now. Failure feedback is a tool result inside the same conversation, so
the model keeps everything it learned. `give_up` is the honest exit.

**The Rust loop runs N stateless authoring turns, gated by Python.** [`adapter.py`
`formalize`](../composer/rustapp/adapter.py#L754-L863) calls `_author_turn` up to seven times; each
turn compiles a *fresh* graph with a *fresh* thread id, whose only memory of the previous attempt
is the failing draft echoed back through `Failure { draft, errors }`. The gate is the Python `for`
loop, not a stamp. There is no buffer to edit, so every attempt re-emits the whole spec; there
is nothing for a `give_up` tool to return to; there is no state for a validation stamp to live in.

So the six review points are not six independent omissions. Five of them are consequences of the
loop shape, and the sixth (structured judge output) is a consequence of the judge being reached
through a prompt-string callout rather than through the shared judge sub-graph:

| Review point | Why it's missing today | Fixed by |
|---|---|---|
| Persistent buffer with edit tools | no session state to hold a buffer | shared session |
| Give-up tool | the loop's only exit is exhausting attempts | shared session |
| Validation stamping | no state; the Python loop *is* the gate | shared session |
| Rebuttal mechanism | needs a judge that is re-invoked in one session | shared session + shared judge |
| Structured judge output | verdict is recovered from prose by [`_parse_judge`](../composer/rustapp/adapter.py#L405-L420) | shared judge (`PropertyFeedback` via `bind_standard`) |
| Code-explorer doc-ref protocol | the tools *are* bound (`build_default_env` → `build_source_tools` wires `code_document_ref`); only the prompt protocol is absent | composed system prompt |

The conclusion this points at: don't add six features to the Rust loop. Replace the Rust loop with
the session the other two already run, and make that session shared code.

## 2. Target shape

One authoring core, three parameterizations. The core owns the session: state, buffer tools, skip
tools, expected-failure tools, publish/give-up gate, the feedback judge, compaction config, and the
last-attempt resume cache. A backend supplies five things:

1. **Buffer semantics** — an optional put-time validator (CVL: the `emv.jar` parser; Foundry: none;
   Rust: the wheel's new `check_syntax` callout, also optional).
2. **Gate tools** — the tools that stamp `validations`, and the keys they stamp.
3. **Ground truth for the mapping** — the property→unit mapping declared at publish is checked
   against what actually ran (Foundry: forge's test names; Rust: the wheel's `units()`; CVL: only
   the batch's titles, as today).
4. **Prompts** — templates for the Python apps, wheel callouts for Rust.
5. **Vocabulary** — tool names, rebuttal evidence kinds, summary wording.

Nothing above is Rust-specific plumbing bolted onto a Python design: items 1–3 are exactly the axes
on which CVL and Foundry already differ from each other, and which
[`composer/foundry/state.py`](../composer/foundry/state.py) duplicates from
[`composer/spec/cvl_generation.py`](../composer/spec/cvl_generation.py) today — right down to
importing that module's private `_merge_skips`.

## 3. The shared module

New package `composer/authoring/`:

- **`state.py`** — `SkippedProperty`, `RebuttalBase`, `PropertyUnitMapping`, the skip/expected-failure
  reducers, the buffer digest, `make_validation_stamper`, `check_completion`, and one
  `validate_unit_mapping(mapping, skipped, titles, ran=None)` that generalizes
  `validate_property_rules` (no ground truth) and `validate_property_tests` (ground truth in both
  directions) — the `ran=None` case is precisely today's CVL behaviour, so the generalization is a
  parameter, not a compromise.

  The state key stays **`curr_spec`**: "spec" is the vocabulary for the authored buffer across all
  three backends, and it is generic enough for whatever a future app authors. Only Foundry's
  `curr_test` renames, which is a handful of call sites in
  [`foundry/state.py`](../composer/foundry/state.py), [`author.py`](../composer/foundry/author.py)
  and [`runner.py`](../composer/foundry/runner.py). Nothing in CVL, the prover, natspec or autosetup
  moves.

  Scope note: this settles the name of the *buffer and the tools over it*. It does not touch
  `FormalResult.artifact_text` / `ReportableResult`, nor the descriptor's `ArtifactLayout` — those
  are pipeline-wide names for the delivered *file*, shared with backends outside this proposal, and
  renaming them would be a much wider change for no gain here.

- **`buffer.py`** — `get_spec` / `put_spec` / `edit_spec` factories over `curr_spec`, parameterized
  by tool name and an optional `Validator = Callable[[str], str | None]`.
  [`composer/core/edit.py`](../composer/core/edit.py) already isolated the string operation for
  exactly this; `edit_cvl`'s "replace, then re-validate exactly like put" body becomes the generic
  one and CVL keeps only its parser validator and its AST-shaped `put_cvl`. Existing tool *names*
  (`put_cvl_raw`, `get_test`, …) are per-backend and unchanged, so no prompt template or recorded
  tape moves.

- **`judge.py`** — one `build_feedback_judge(...)` replacing
  [`property_feedback_judge`](../composer/spec/feedback.py) and Foundry's near-identical
  `_build_feedback_thunk`: rough-draft tools, the memory tool, spec read-back with the
  `did_read` completion validator, `PropertyFeedback` as structured output, rebuttals rendered into
  the judge's input, and an `exclude_tools` knob (this is where Crucible's `code_explorer` exclusion
  survives — as a shared cost lever available to all three backends, not a Rust special case).

- **`session.py`** — `run_authoring_session(...)`: assembles the builder (`env.all_tools` + buffer
  tools + skip tools + expected-failure tools + gate tools + `feedback_tool` + `result` + `give_up`
  + memory), applies the summary config, resumes the last-attempt cache, runs to completion, and
  returns `AuthoredSpec { commentary, spec, skipped, mapping, expected_failures } | GaveUp`.

- **`templates/authoring_protocol.j2`** — the protocol section every author's system prompt gets:
  the buffer/edit contract, what publish requires, when to skip, when to give up, and the
  `doc_ref_author.j2` / `doc_ref_reader.j2` includes that are already shared but that only the
  Python prompts pull in today.

CVL's `batch_cvl_generation` and Foundry's `batch_foundry_test_generation` become thin: they build
their gate tool, their prompts and their validator, and call `run_authoring_session`. Their existing
tests are the proof the extraction was behaviour-preserving, so they should not need edits.

## 4. What the Rust seam has to change

The wheel stays a passive service and stays blocking; what changes is that the *gate* becomes a
tool the agent calls instead of a Python `for` loop.

**Wire / trait, removed.** `Failure`, `FailureKind`, and `author_prompt`'s `failure` parameter.
Revise context is now conversational — build errors and judge feedback arrive as tool results in a
session that remembers. `author_prompt(&self, input) -> Prompt`.

**Wire / trait, added.**

- `check_syntax(&self, input, spec) -> Option<String>` — optional, default `None`. The cheap
  put-time validator; the analog of CVL's parser. Pure, no toolchain.
- `Delivered.skipped: Vec<SkippedProperty>` — so `finalize` and the report see what the author
  declined and why. `RustFormalResult.skipped` exists already and is never populated.
- Descriptor: `evidence_kinds: Vec<String>` — the rebuttal vocabulary. The `Literal[...]` in the
  `Rebuttal` schema is built with `create_model` from this, defaulting to
  `build_failure | check_output | counterexample | manual_citation | reasoned`.

**Unchanged.** `units`, `judge_prompt`, `compile`, `validate`, `workspace_prep`, `sandbox_grants`,
`validate_preconditions`, `finalize`, and the whole preflight/setup phase structure. `units()` stays
pure and pre-authoring — and it gets *stronger*: it becomes the ground truth the publish-time
mapping is checked against, which is more than CVL has.

**The gate tool.** One per session kind, wrapping an existing callout off the event loop under the
existing `command_sem`:

- Component sessions get `validate_spec(units=None)` → the wheel's `validate`, per distinct target,
  exactly the grouping [`formalize`](../composer/rustapp/adapter.py#L814-L855) computes today. It
  records the verdicts in state and stamps `validations["validate"]` only when the run covered *all*
  live targets and every unit came back `GOOD` or is marked expected-to-fail. A partial run never
  stamps — the same rule as the prover tool's `rules is None` and forge's unseeded run.
- Setup sessions get `compile_spec` → the wheel's `compile`, stamping `validations["compile"]`.
  A setup artifact has no report units, so a build gate is the only gate it can have; this is
  `author_and_compile`'s existing behaviour moved into the session.

No *third*, non-stamping build tool alongside `validate_spec` in the component belt. Crucible's
`validate` already fuses build and run, so a separate `check_build` would only save the checker's
runtime on a draft that doesn't compile — worth revisiting if a real Crucible run shows the author
burning fuzz budget on build errors, and cheap to add then (the callout is already there and already
wrapped for the setup session).

**Publish gate for Rust**: `validate` stamp + `feedback` stamp on the current buffer, and the
declared mapping covers exactly the non-skipped properties' units from `units()`. The verdicts from
the stamping run become `RustFormalResult.verdicts`; `commentary` finally gets a value.

## 5. Design points that change from the branch as it stands

None of these is a production behaviour change: `composer/rustapp/` is unshipped work on
`eric/rust`, so what follows revises code that has been *reviewed* but never run for a user. They
are listed because each one reverses something the current branch documents as deliberate, and a
reviewer who read that reasoning is owed the counter-reasoning.

1. **A counterexample must be marked, not just reported** — the CVL/Foundry semantics, adopted
   as-is. Today `formalize` returns whatever verdicts `validate` produced, so a `BAD` flows straight
   into the report unexamined. Under a stamping gate a `BAD` blocks publish until the author either
   fixes the spec or calls `expect_unit_failure(unit, reason)`, exactly as
   `expect_rule_failure` / `expect_test_failure` work; an author that can do neither calls
   `give_up`. This is the parity feature that makes a counterexample *reasoned about* rather than
   merely recorded: the report gains the author's justification, and the tally does not change.

2. **The judge stops failing open.** [`_parse_judge`](../composer/rustapp/adapter.py#L405-L420)
   reads unparseable prose as acceptance because it has no structure to rely on. With
   `PropertyFeedback` as the judge's structured output there is no unparseable case: `good` is a
   bool the judge had to set. `docs/rust-applications.md` §5 and §12 need rewriting.

3. **The review budget goes away.** `MAX_REVIEW_ROUNDS`, `_ReviewBudget`, `_budgeted`'s relenting
   `Accepted`, `_review_gate` and `_RequestReview` all delete. The unwinnable-loop concern they
   answer is real, and the Python apps answer it differently: the agent decides when it has had
   enough feedback, and the recursion limit plus `give_up` bound the session. Adopting the shared
   session means adopting that answer. `tests/test_rustapp_review_budget.py` is replaced by
   stamping-gate tests.

4. **The system prompt is composed, not wheel-owned.** Today `Prompt.system` is the whole system
   prompt (or the host's neutral `_DEFAULT_SYS_PROMPT`). It becomes the *domain* section, appended
   to the shared protocol section — otherwise every wheel hand-rolls the tool protocol and the
   doc-ref rules, and they drift again. Same as `foundry_property_generation_system_prompt.j2`
   including the shared partials.

5. **The initial prompt carries the property/unit listing.** The publish gate validates the
   mapping against exact title strings and exact `units()` names, so the host renders that listing
   rather than trusting each wheel's free-form instruction to spell them identically.

## 6. Work sequence

All of this lands on `eric/rust`, in the PR that introduces the Rust framework. That is the right
place for it: `composer/rustapp/` and `rust-applications.md` do not exist on `master`, so the loop
being replaced has never shipped and there is no migration to stage. The four steps below are a
commit order within one branch, not four reviews.

The split that does matter is a different one, and it runs through every step: **the CVL/Foundry
half touches shipped code, the Rust half does not.** Step 1 is the only place where a mistake can
regress something a user has today, which is what makes its success criterion worth stating
precisely.

1. **Extract the core.** Create `composer/authoring/` — `state.py` (skips, digest, stamps, publish
   gate, mapping validation), `buffer.py` (the `curr_spec` tools), `judge.py` (the feedback judge),
   `tools.py` (give-up) — move that machinery out of `cvl_generation.py` / `foundry/state.py` /
   `foundry/author.py` / `spec/feedback.py`, rename `curr_test` → `curr_spec`, rewire CVL, natspec
   and Foundry onto it. Also `composer/foundry/state.py` stops reaching into another package's
   privates.

   No `session.py` yet: the only session-level machinery that exists is CVL's `run_cvl_generator`,
   whose last-attempt cache is keyed by a CVL-typed `CacheKey` and whose cached field name is
   live on disk. It gets generalized in step 3, where the Rust session is its second caller.

   The skip tools (`record_skip` / `unskip_property`) and the expected-failure tools also stay
   per-backend for now. CVL reads its property titles from a langgraph runtime context and Foundry
   from a bound dependency; unifying those means changing one of the two call sites' shape, which
   is a change to shipped code with no consumer yet. Step 3 adds the third copy — that is the
   point to unify, with Foundry's bound-dependency idiom as the target.

   Success criterion: the existing CVL and Foundry tests pass with **no assertion or fixture
   changes** — an edit to what a test *checks* is the signal that this step changed behaviour it
   was supposed to preserve. Import lines move with the code, and the one fixture that spells the
   renamed state key follows the rename.

2. **The seam.** SDK trait + wire changes from §4, both mirrors and
   `tests/test_wire_roundtrip.py` / `tests/test_rustapp_wire.py` in lockstep, `example-app` updated
   as the reference implementation.

3. **Rework the adapter.** `RustFormalizer.formalize` and `author_and_compile` become
   `run_authoring_session` calls with their gate tools. Delete `run_llm_agent`, `_author_turn`,
   `_judge_turn`, `_make_judge_hook`, `_parse_judge`, `_RequestReview`, `_ReviewBudget`,
   `_review_gate`, `_budgeted`, `_strip_fence`, `_NO_ARTIFACT`, `_UNEXPLAINED_REJECTION`,
   `DEFAULT_MAX_ATTEMPTS`. `adapter.py` should end up substantially shorter than its current 1205
   lines. Rewrite `tests/test_rust_llm_agent.py` and `tests/test_rustapp_review_budget.py`; update
   `tests/test_rustapp.py`, `test_rustapp_setup_cache.py`, `test_rustapp_validate_target.py`,
   `test_rustapp_verdicts.py`.

4. **Docs and replay.** Rewrite `rust-applications.md` §5 and its §12 judge entries to describe the
   session, and **fold this file into it, then delete this file.** Shipping both would leave the
   repo carrying a document that explains a delta against code no reader outside the branch ever
   saw — §1 and §5 are review correspondence, not a design record. Re-record any tape whose tool
   surface moved; the CVL/Foundry tool *names* are unchanged by step 1, so the exposure is the Rust
   harnesses.

Step 1 before step 3, because the point is that step 3 is small. Steps 2 and 3 are one change in
practice.

## 7. Decisions taken

- **"Spec" is the vocabulary** for the authored buffer and every tool over it, across all three
  backends. `curr_spec` stays; Foundry's `curr_test` renames into it. The pipeline-wide
  `artifact_text` / `ArtifactLayout` names for the delivered *file* are out of scope (§3).
- **Separate entry functions.** `batch_cvl_generation` and `batch_foundry_test_generation` stay as
  thin per-backend assemblies over the shared core, and the Rust adapter is a third. They differ in
  exactly the parameters the core already takes, so collapsing them would buy nothing.
- **No standalone build-check tool** in the component belt for now (§4).
- **The rebuttal evidence vocabulary is descriptor-declared** per wheel (§4). CVL and Foundry
  already differ on it (`typecheck_failure` vs `compilation_failure` / `test_run_output` /
  `execution_trace`) because the evidence a backend can actually produce is a property of that
  backend, not of the judge; a wheel declaring its own is the same call, made once more.
- **The counterexample gate follows CVL/Foundry** (§5.1) — publish is blocked on a `BAD` until it
  is fixed, marked with a reason, or given up on.
