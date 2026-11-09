# CVLR author prompts: less standing text, more recipes, typed Prover symptoms

A plan to shrink what the CVLR author and judge carry on every turn. It has three parts that depend
on each other:

* **The Prover reports its analysis failures as typed values**, the way it already reports the
  origin of a violated assertion (`assertOrigin`). A typed symptom can be matched in code, so the
  advice for it can be *pushed* to the author at the moment it applies, instead of sitting in the
  system prompt in case it does.
* **Duplicated and redundant prompt text is removed.** Several passages appear two or three times in
  the author's context.
* **Situational text moves into recipes**, retrieved by trigger (`get_cvlr_recipe`) or pushed by
  `verify_rules` on a typed symptom.

The CVL side is out of scope. Its author already runs on recipes: a 3 KB system prompt and 21
recipes in `cvl_recipes_index.yaml`.

---

## 1. Where the text is now

What the CVLR author carries on every turn:

| text | where | size |
|---|---|---|
| system prompt | `composer/templates/cvlr_property_generation_system_prompt.j2` | 49 KB |
| baseline facts, inlined as a context document | `composer/kb/resources/cvlr_baseline_facts.md` | 26 KB |
| initial prompt | `composer/templates/cvlr_property_generation_prompt.j2` | 21 KB |
| recipe index (K1–K3) | `composer/kb/resources/cvlr_recipes_index.yaml` | 2.5 KB |

That is about 98 KB, or roughly 25k tokens, before the program's own context. The judge
(`cvlr_property_judge_system_prompt.j2`, 25 KB) loads the same baseline facts and also holds
`get_cvlr_recipe` (`CVLR_BUNDLE` in `composer/kb/kb_context.py`), so a recipe can shorten both
prompts at once.

The largest sections of the system prompt:

| section | size |
|---|---|
| "Reaching a handler on an Anchor program", through "Writing the stand-in" and the `try_accounts` shape | 19 KB |
| "When the program itself is in the way, ask the editor" | 5.8 KB |
| "When a computation will not finish: a bounded stand-in" | 4.5 KB |
| "State invariants", with "When a counterexample starts from a state the program cannot be in" | 5.0 KB |
| tools list | 3.6 KB |
| "The prover configuration your rules run under" | 3.5 KB |

Step 3 of the initial prompt, which says how to read a verification outcome, is another 9 KB. Most
of it is a list of measured cases.

---

## 2. Three ways to deliver a piece of advice

Each passage gets one of three channels. The question is when the agent first needs it, and whether
anything can tell it so.

1. **Standing** (system prompt, baseline facts). For text that has to shape the *first* draft:
   habits whose absence costs a prover round before any symptom appears. Examples: call the handler,
   not the dispatcher; `.unwrap()`, not `.is_ok()`; the contrapositive form for a rejection;
   `Box` and `core::mem::forget(accounts)`; read post-state through the context's handle. Also the
   tools list, the conf, and what a finding is.
2. **Pushed** (attached to a `verify_rules` result). For text keyed on a symptom the code can
   recognize as a typed value. The author does not have to recognize anything, and the text costs
   tokens only in runs that hit the symptom. This already exists for two kinds of generated
   assertion: `_ADVISED` in `composer/spec/cvlr/verify.py` and the matching paragraphs in
   `composer/templates/cvlr/verdicts.j2`. `cvlr/external_functions.j2` is the same idea for the
   Prover's external-function warnings.
3. **Pulled** (a recipe the agent retrieves from the index). For text keyed on a situation the
   agent recognizes in the program or the property before any run: "the handler performs a CPI",
   "an acceptance property through a `seeds` constraint". Also for symptoms that are not typed yet.

A recipe file is the single home for the text in channels 2 and 3. A pushed recipe is still listed
in the index, so an author who meets the situation before a run says so can fetch it, and the judge
can fetch it too. Moving text into a recipe file also puts its code blocks under
`tests/test_cvlr_recipe_examples.py`, which compiles every `rust` block. Text in a template gets no
such check.

---

## 3. Prover communication

### 3.1 What is typed today

| signal | type | used for |
|---|---|---|
| a violated assertion's origin | `AssertOrigin` = `UserAssertion \| GeneratedAssertion(GeneratedKind \| UnknownKind) \| UnclassifiedAssertion` (`composer/prover/ptypes.py`) | `classify_violation` sorts a violation into a finding or a Prover limit; `_ADVISED` pushes advice for `loopUnwinding` and `cannotOverflow` |
| where an unfinished rule stood | `RuleStatistics` (phase, path count, branching and nonlinear hotspots), from `rule_live_statistics_*.json` (`composer/prover/results.py`) | rendered into the timeout report |
| rule status | `VERIFIED`, `VIOLATED`, `TIMEOUT`, `ERROR`, `SANITY_FAILED`, `SKIPPED` | the gate and the verdict note |

### 3.2 What is still free text

* **Analysis errors.** An `ERROR` rule carries `error_messages: list[str]`, collected from the tree
  view's error nodes (`flatten_tree_view` in `composer/prover/results.py`). The prompts teach the
  author to recognize these by the bracketed code in the message:

  | code | message | what the prompts say it means |
  |---|---|---|
  | `[3308]` | illegal dereference of an absolute address | pointer analysis refused an object; most often an Anchor `#[error_code]` enum formatting its `#[msg]` |
  | `[3003]` | dereference of a register with unknown provenance | the same family; seen through `Rc<RefCell<..>>` in `AccountInfo::lamports` |
  | `[3005]` | memcpy with dynamically sized length | a copy whose length the analysis cannot fix: a variable-length collection or a derived (de)serializer |
  | `[3006]` | illegal store of a stack pointer | the same family |

  `results.py` itself tells a timeout from an error by matching substrings in the message
  (`"timed-out"`, `"BlockingCoroutine was cancelled"`).
* **Job-level failures.** An internal error out of `sbf.domains.ScalarDomain`, and a job that fails
  with no per-rule verdicts at all. The reason is in the job's `alertReport.json` and does not reach
  the author (open since the cloud work; see the FAILED-job note in `cvlr-todo.md`).
* **The cause of a vacuity.** A `SANITY_FAILED` rule does not say *why* no path reaches the end.
  The commonest measured cause is a generated assertion on every path: the unwinding assertion of
  the loop that drops the 16-account array, after the rule's own assertion. `explain_vacuity` gets
  at some of this from the unsat core, but cannot see past the rule's assertion.
* **"Cannot overflow" before `assertOrigin`.** Older Provers report it as an unclassified assertion,
  which `classify_violation` treats as the rule's own.

### 3.3 Asks of the Prover

In order of value:

1. **A kind on every analysis error**, in the tree view, beside the message: `errorKind` on each
   error node, from a closed vocabulary the Prover documents, as `assertOrigin` does. The codes
   above are the minimum. Each should carry, where the Prover has it, the **function** whose code it
   refused and the **source location**, so the advice can name the method to stand in for rather than
   asking the author to read a trace. That decides the `redirect_module` versus `mock_fn` question
   ("diagnose the method, not its caller") in code.
2. **A kind for a timeout.** `rule_live_statistics_*.json` already gives the phase. A timeout that
   is the global job timeout, as opposed to the rule's own budget, should say so. Today that is
   decided from message text.
3. **A job-level failure reason**, in the job's result, not only in `alertReport.json`: a kind
   (`internalAnalysisError`, ...) and the component that raised it. A job that fails with no rule
   results then says which of its causes it was.
4. **A vacuity cause.** For a `SANITY_FAILED` rule, the generated assertions that sit on every path
   to the vacuity check's `satisfy`, with their kinds. That is the same data `assertOrigin` gives for
   a violation. It would make the drop-loop case report itself as `loopUnwinding` after the rule's
   last statement. (Separate from the fix that removes the drop loop's effect on vacuity: see
   [upstream-defects.md](./upstream-defects.md).)

Each ask is independent. The AutoProver side below works with whichever of them have landed.

### 3.4 AutoProver side

* **A typed analysis error**, parallel to `GeneratedKind`:

  ```python
  class AnalysisErrorKind(StrEnum):
      ILLEGAL_ABSOLUTE_DEREF = ...   # [3308]
      UNKNOWN_PROVENANCE = ...       # [3003]
      DYNAMIC_MEMCPY = ...           # [3005]
      STACK_POINTER_STORE = ...      # [3006]

  @dataclass(frozen=True)
  class AnalysisError:
      kind: AnalysisErrorKind | UnknownKind
      message: str
      function: str | None
      source: SourceSpan | None
  ```

  `RuleResult` gets `errors: list[AnalysisError]` beside `error_messages`; the latter stays until
  nothing reads it. A kind this build does not know is `UnknownKind`, logged, as `assert_origin` does.
* **Until ask 1 lands**, read the kind from the message's `[NNNN]` prefix, in one function next to
  `assert_origin`, with a test per code. This parses the Prover's text, not anything of ours, and it
  is removed when `errorKind` ships. A message with no code is `UnknownKind`. `function` and `source`
  stay `None` until the Prover provides them.
* **A typed timeout and job failure**, as ask 2 and 3 land. `RuleStatistics.phase` becomes a type
  (`StaticAnalysis | Solving(...) | OtherPhase(str)`), so "the rule never reached the solver" can be
  matched rather than compared to a string.

### 3.5 Prover-side findings (November 2026)

From reading the Prover's code (EVMVerifier) for each ask. Asks 2–4 are deferred; these notes are so
the investigation is not repeated.

**Ask 1: in progress** (EVMVerifier branch `eric/autoprover-typed-errors`, not merged). Each entry in
a rule node's `errors` gains two fields beside `severity` and `message`:

```json
{ "severity": "error", "message": "[3308] illegal dereference ...",
  "errorKind": "absoluteAddressDeref",
  "frames": [ {"function": "...", "file": "src/lib.rs", "line": 42} ] }
```

* `frames` is the inlined source call chain at the refused instruction, innermost first, from
  `llvm-symbolizer`. It replaces `function` and `source` in §3.4: the innermost frame is often in
  `core`/`alloc`, so `AnalysisError` should keep the list and pick the first frame in the program's
  own sources for the `redirect_module` / `mock_fn` decision. `function` is `null` when DWARF has no
  name, and `frames` is empty for errors with no instruction (no assertion, heap exhausted).
* The vocabulary is `SolanaErrorKind` in `sbf/support/Errors.kt`. One kind per code:
  3001 `indeterminateStackOffset`, 3002 `unknownStackContent`, 3003 `unknownProvenanceDeref`,
  3004 `unknownProvenanceStore`, 3005 `dynamicMemcpyLength`, 3006 `stackPointerEscape`,
  3307 `indeterminateGlobalDeref`, 3308 `absoluteAddressDeref`, 3309 `conflictingHeapUsage`,
  3310 `stackNotScalarizable`, 3311 `undecidedAllocFree`, 4000 `noAssertion`,
  4001 `noAssertionAfterSlicing`, 4002 `noSatisfyAfterSlicing`, 5000 `summaryFileSyntax`,
  5001 `inliningFileSyntax`, 6000 `stackFrameExceeded`, 6001 `heapExhausted`,
  7000 `unsupportedCall`, 7001 `unsupportedIndirectCall`. Errors with no code also get a kind:
  `missingRuleFunction`, `strippedSymbols`, `missingConfigurationFile`, `summaryShapeMismatch`,
  `invalidStackOffset`. The §3.4 enum names (`ILLEGAL_ABSOLUTE_DEREF`, ...) should map to these.
* Side effect: the message loses its `In <rule>: ` prefix. Encoding failures were reported through a
  wrapper exception, which also logged each one as "Unknown error ... Error code N". The `[NNNN]`
  prefix is unchanged, so the interim parser keeps working.
* An internal error while encoding a rule (`SolanaInternalError`, including `ScalarDomainError`) has
  no kind yet. It has been caught per rule since EVMVerifier #8771, so a job that lost every rule to
  a `ScalarDomain` error was probably on an older Prover.

**Ask 2: timeout kind.** Nothing machine-readable says which timeout ended a rule.

* A rule cut off by the **global timeout** usually ends as `TIMEOUT` with no message: still running
  at shutdown, it is set to `TIMEOUT` by `TreeViewReporter.terminateRunningLeaves`. Less often it is
  `ERROR` with `Rule X timed-out` (an interrupt) or `BlockingCoroutine was cancelled`.
* `BlockingCoroutine was cancelled` always means the global timeout: it is kotlinx's message for the
  main coroutine scope that the global-timeout handler cancels (`EntryPoint.kt`). It also produces a
  second, misleading "internal Prover error" alert in `alertReport.json`.
* A **solver `TIMEOUT`** can also be the global budget: `Config.getSolverTimeout()` caps the solver
  limit at the remaining global time. Out-of-memory is reported as `TIMEOUT` too.
* After a global timeout the exit code is overwritten from ERROR to FAIL (100) by the tree view's
  close-time check.
* Shape to propose: `timeoutKind` on the rule node (`solver`, `solverCappedByGlobal`, `global`,
  `outOfMemory`), using a flag set by the global-timeout handler. `rule_live_statistics_*.json` says
  nothing about why a rule stopped; the node is the better place.

**Ask 3: job-level failure reason.** There is no job-status field anywhere: the tree view, `output.json`
and `statsdata.json` carry none, and `alertReport.json` drops the exception's type.

* Still job-level on Solana: anything before the per-rule loop (ELF disassembly, reading the
  summary and inlining files, CFG construction), and a non-Solana exception (`check`/`error`) inside a
  rule's encoding.
* `VerificationFlow` silently drops (warn log only) any encoding failure it cannot tie to a rule, and
  likewise for continuation rules.
* Shape to propose: a top-level `jobFailure: {kind, component, message}` in the tree view and
  `output.json`, set from the catch blocks in `EntryPoint.kt` and the global-timeout handler. Kinds
  fall out of those blocks: `globalTimeout`, `userError`, `internalError`, `unexpectedException`,
  `badArguments`.

**Ask 4: vacuity cause.** Feasible, with two traps.

* In the CVLR vacuity rule, generated assertions (loop unwinding, cannot-overflow, panic, unresolved
  CPI) become assumptions with their kind kept (`assumeAssertsOfSatisfyRule`). But the optimizer
  later cuts `assume false` branches and the replacement commands carry no kind. So the analysis has
  to run before optimization, not on the solver's TAC.
* "A generated assertion on every path to the satisfy" misses the drop-loop case: the unwinding check
  sits only on the path that runs every unrolled iteration, so it never dominates the satisfy. The
  sound test is that the **loop head dominates the satisfy, before unrolling**.
* With `optimistic_loop` the unwinding check is a plain assumption with no kind at all.
* Shape to propose: `blockingAssertOrigins: [{assertOrigin, jumpToDefinition}]` on the
  `SANITY_FAILED` node, reusing the `assertOrigin` names. It says what lies on every path, not proven
  cause; cause would need a re-solve without those assumptions.
* Available now, with no Prover change: in multi-assert mode, the sibling `ProverInternalChecks`
  subrule is usually VIOLATED with `assertOrigin: loopUnwinding` when a drop loop after the rule's
  assertion always exceeds the bound. That can stand in for ask 4 in the drop-loop case.

**Soroban/WASM.** No asks yet; what the Prover side would need first:

* No WASM front-end exception is a `CertoraException`, and part of each rule's encoding (inlining,
  the satisfy check) runs outside the per-rule `try` (`WasmEntryPoint.kt`). So any failure, including
  "no assertions" and "mixed assert and satisfy", fails the whole job with a generic "internal Prover
  error" alert, and every rule's result is lost. Solana reports the same cases per rule.
* Unsupported opcodes (floating point, `CALL_REF`, table ops, ...) throw Kotlin `TODO()`, which is not
  caught at all: the JVM exits with a stack trace and no alert.
* Silent havocs, with no signal in the report: calls to imports with no model, and known Soroban host
  functions with no model, including all cross-contract `call` functions. These are the WASM
  equivalent of Solana's 7000/7001. Also havocked with no log: integer `popcnt`/`rotl`/`rotr` and
  some `f64` operations. `core::panicking::panic*` is always a revert, even under `trapAsAssert`.
* There is no DWARF reading on WASM, so `frames` there could carry function names (from the inliner's
  annotations) but no file or line.
* Already typed as `assertOrigin`: `memcpyOverlap`, `recursionLimit`, `unsupportedHostFunction` (wrong
  arity only), and `panic` / `loopUnwinding` under `trapAsAssert`.
* Order of work when it matters: a `WasmError` type and per-rule catching first, then kinds
  (`noAssertion`, `mixedAssertSatisfy`, `invalidEntryPoint`, `unsupportedInstruction`,
  `internalError`), then a kind for the unmodeled calls.

---

## 4. Pushing recipes on typed symptoms

Today the pushed advice lives in `verdicts.j2`, keyed by a tuple in Python. Move it into recipe files
and key it from the index.

* **The index says what pushes a recipe.** A new optional field on `KBRecipe`, validated by
  pydantic with the rest of the index:

  ```yaml
  - id: K4
    name: A loop the bound does not finish
    pushed_on:
    - generated: loopUnwinding
  ```

  The field's type is a discriminated union over the symptom families:
  `generated: GeneratedKind`, `error: AnalysisErrorKind`, `timeout: <phase kind>`,
  `job_failure: <kind>`, `vacuity: GeneratedKind`. CVL's index never sets it. The kind names in the
  YAML are validated against the enums, so a renamed kind fails at load, not at the first run that
  would have pushed it.
* **`verify_rules` attaches the bodies.** `_verdicts_note` collects the symptom kinds in the run, as
  it collects `advised` now, looks up the recipes pushed on each, and renders each body once per
  note. `_ADVISED` and the advice paragraphs in `verdicts.j2` are deleted. The note keeps its own
  text about what the verdict *means* for the gate (not a finding, `expect_rule_failure` does not
  account for it).
* **Inline the body, do not send the id.** The bodies are a few hundred words, and a pushed id costs
  a tool call the author may skip. If a run hits the same kind on every submission, the repetition
  is the cost to watch; send the body the first time per thread and the id after that only if a
  benchmark run shows it matters.
* **The judge** gets the same recipes by id. Its prompt names the recipe where it now restates the
  text (§5).

---

## 5. Removing duplicates and redundancy

These need no recipe and no Prover change. Do them first.

| passage | copies | keep |
|---|---|---|
| The loop ladder (bound the trip count, take the loop out of the way, raise `loop_iter`, `optimistic_loop` last) | system prompt "The loop ladder"; system prompt conf section, the paragraphs on `optimistic_loop`; baseline facts "Loops are bounded"; judge §5; `verdicts.j2` `loopUnwinding` | one recipe, pushed on `loopUnwinding` (K4 in §6). The system prompt keeps one sentence: the two settings you can move, and that `optimistic_loop` is the last rung and changes what a verdict means. The judge's §5 keeps what to *check* and points at the recipe for the ladder |
| "Cannot overflow" and `-solanaTACSoundSignedMath` | system prompt conf section; `verdicts.j2` `cannotOverflow` | one recipe, pushed on `cannotOverflow` (K5). The conf section keeps one sentence on why the flag is there |
| "What a CPI does is not modelled" | system prompt, twice: in "Reaching a handler" and again after the Anchor names table | the first; the CPI recipe (K6) carries the detail |
| `core::mem::forget(accounts)` and the drop loop | system prompt "Reaching a handler"; initial prompt Step 3 vacuity bullet | the system prompt's, which is preventive. Step 3 keeps one line pointing at it |
| The CPI model, for the judge | judge §1, twice: under "Judge authorship, not depth" and in the paragraph beginning "For a CPI — the common case"; both restate the system prompt's | the judge keeps what to check (rules asserting over a value a CPI moves, with nothing modelling the move); the mechanism moves to K6 |

Also fix the cross-reference in the system prompt's editor section: "`swap_derive` ... has its own
section below" points at text that is in the baseline facts, not below. It goes away when that text
moves to K11.

---

## 6. Moving text into recipes

New recipes, continuing the K series. "Source" is where the text is now. "Push" is the typed symptom
from §4 that attaches it, where one exists; otherwise it is pulled only.

| id | recipe | source | trigger in the index | channel | push |
|---|---|---|---|---|---|
| K4 | A loop the bound does not finish | system prompt "The loop ladder", conf section, baseline facts "Loops are bounded" | a rule stops on `Unwinding condition in a loop` | CONF / RULE | `generated: loopUnwinding` |
| K5 | `Cannot overflow` on an inferred pointer | system prompt conf section; `verdicts.j2` | a rule stops on `Cannot overflow: inferred pointer` or `computing TAC map index` | RULE / SKIP | `generated: cannotOverflow` |
| K6 | Taking a CPI out from under a handler | system prompt "Reaching a handler" (the second move), "Writing the stand-in: two things that do not analyze" | a handler performs a CPI and the property is its own bookkeeping; `[3003]` with `alloc/src/rc.rs` in the trace; a VIOLATED rule over a value decoded from an instruction's bytes | EDIT | `error: UNKNOWN_PROVENANCE` with a function in `alloc::rc`, once ask 1 gives the function |
| K7 | Descending to the program's state-transition core | system prompt "Reaching a handler" (the first move, and its three conditions) | a handler-level rule blocked by a CPI, by validation or by error formatting, where the program has an accounting function below the handler | RULE | — |
| K8 | A bounded stand-in for a computation that will not finish | system prompt "When a computation will not finish"; initial prompt Step 3, the timeout bullets about a rule alone still halting | a TIMEOUT that survives a small job; a statistics line with phase `static analysis`, or one function holding most of the branching or nonlinear share | MOCK / EDIT | `timeout: static analysis`, and a hotspot share over a threshold |
| K9 | A halted job, and which rule halted it | initial prompt Step 3, the timeout bullets (submit without the last rule; the measured cases: a `HashSet` insert, a bincode walk of a `Pubkey`, a `u64 × u64` product, per-length splits, per-epoch walks) | a job that halts with no verdicts; a rule that times out alone | RULE / EDIT / SKIP | `job_failure: global timeout`, once ask 2 lands |
| K10 | A rule the pointer analysis refuses | initial prompt Step 3, the "could not analyze" bullet (`summarize_for_prover`, the three-directive limit, what a summary drops, weaken or skip) | `[3308]`, `[3006]` on a rule | MOCK / SKIP | `error: ILLEGAL_ABSOLUTE_DEREF`, `error: STACK_POINTER_STORE` |
| K11 | State the analysis cannot size: variable-length collections and `swap_derive` | baseline facts "When the Prover cannot follow the program's own data" | `[3005]`; an internal error out of `ScalarDomain`; a job that fails with no rule results | EDIT / SKIP | `error: DYNAMIC_MEMCPY`; `job_failure: internal analysis error`, once ask 3 lands |
| K12 | Diagnosing a vacuous rule | initial prompt Step 3, the vacuity bullet (`explain_vacuity` first, then a copy ending in `cvlr_assert!(false)`, not `cvlr_satisfy!`) | a rule reported `SANITY_FAILED` | RULE | `SANITY_FAILED` status; `vacuity: <kind>` once ask 4 lands |
| K13 | Acceptance through a `seeds` constraint | system prompt "Reaching a handler", the paragraph on `seeds` | an acceptance property whose validation runs through `seeds`, including `init` on a PDA; "a non-canonical X is refused" | SKIP | — (the point is to skip *without* a run) |
| K14 | Validating an accounts struct with `try_accounts` | system prompt, the `try_accounts` example and "The names Anchor generates" | a property enforced by the struct's `#[account(...)]` constraints | RULE | — |

Notes on individual moves:

* **The editor's nine kinds** (5.8 KB) stay as a menu, one line each, because the author has to know
  what it can ask for before it asks. The depth moves to the recipe the kind serves:
  `redirect_module` and `mock_fn` to K8 and K10, `swap_import` to K6, `swap_derive` to K11,
  extraction to K7. The rule against shrinking a collection stays in the menu.
* **K14 is templated today.** The example and the names table substitute this program's struct,
  bumps type and discriminant path (`example.*`). A recipe file is static. Keep a two-line,
  templated "for this program: struct `X`, bumps `XBumps`, ..." in the system prompt, and move the
  rules and the generic example into K14.
* **State invariants** do not need a recipe: `invariant_guidance()` already serves that topic on
  demand (`composer/templates/cvlr_invariant_guidance.j2`). Move the `macro_rules!` example and the
  "starts from a state the program cannot be in" ladder there. The system prompt keeps the three-part
  outline and the rule that an assumed invariant's steps are never marked expected-to-fail.
* **Step 3 of the initial prompt** keeps the skeleton: each outcome, what it means for the gate, and
  which recipe answers it. With K9, K10 and K12 pushed, its measured cases leave.
* **Index triggers quote the literal symptom strings** (`Unwinding condition in a loop`,
  `[3308]`, `alloc/src/rc.rs`), because the agent matches on what the report says. A pushed recipe
  needs them too, for the judge and for an untyped report from an older Prover.

What stays standing (§2, channel 1): output shape, the tools list, the conf, the handler idioms
named there, "When a rule should fail", and the judge's checklist.

---

## 7. Order of work

1. **Duplicates (§5).** Prompt-only. Removes about 6 KB from the author's context.
2. **Push mechanism (§4)** with K4 and K5, replacing `_ADVISED` and the paragraphs in `verdicts.j2`.
   Behaviour-preserving for those two kinds, so a good check of the mechanism.
3. **Pulled recipes that need nothing new:** K6, K7, K13, K14, and the move into
   `invariant_guidance()`.
4. **Typed analysis errors (§3.4)**, from the message code, then K10 and K11 pushed on them.
5. **K8, K9, K12**, pushed on the statistics phase and on `SANITY_FAILED`.
6. **Prover asks (§3.3)** as they land: switch the parser from message codes to `errorKind`, add the
   function to K6's push key, add job-failure and vacuity kinds.

Steps 1–3 take roughly 25 KB off the author's standing text, and steps 4–5 another 10 KB.

## 8. Checking it

* `tests/test_cvlr_recipe_examples.py` compiles every block of the new recipes. Code that calls the
  program is fenced `rust,ignore`, as the existing recipes do.
* The template fuzzer and the `TypedTemplate` params cover the edited templates.
* A unit test per pushed kind: a `RuleResult` carrying it produces a verdict note containing that
  recipe's body, once.
* The CVLR tape replays a recorded run. If its recorded tool calls depend on text that moved, it
  needs re-recording rather than patching.
* **A benchmark run before and after**, on a unit whose earlier run hit loop, CPI and pointer-analysis
  stops. Compare the author's prompt tokens, how often each recipe was fetched or pushed, and whether
  the outcomes held: the same rules verified, no new reimplementation, no new skip that a pushed
  recipe would have prevented. A recipe that is never fetched in a run where its trigger occurred
  is the failure this change risks. It means that text belonged in the standing prompt.

## 9. Open questions

* **Repeated pushes.** If a unit hits `loopUnwinding` on every submission, the body is repeated
  every time. Once per thread is cheaper, but a summarization can drop the first copy. Decide after
  measuring.
* **Recipes for the editor.** The munge editor (`cvlr_munge_editor_system.j2`) has its own copy of
  some of this (what each kind is for). It does not hold `get_cvlr_recipe`. Whether it should is a
  separate question.
* **Whether K7 is a recipe at all.** Descending below the handler is the commonest successful move
  in shipped verification projects. If the benchmark shows authors not fetching it until after a
  blocked handler-level rule, it goes back into the standing prompt.
