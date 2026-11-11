# CVLR author prompts: less standing text, more recipes, symptoms matched in code

A plan to shrink what the CVLR author and judge carry on every turn. It has three parts that depend
on each other:

* **Prover symptoms are matched in code**: the origin of a violated assertion (`assertOrigin`), the
  `[NNNN]` code of an analysis error, the phase of a timed-out rule. A matched symptom lets the advice
  for it be *pushed* to the author at the moment it applies, instead of sitting in the system prompt
  in case it does. What the Prover already reports is enough for that; no Prover change is needed
  (§3).
* **Duplicated and redundant prompt text is removed.** Several passages appear two or three times in
  the author's context.
* **Situational text moves into recipes**, retrieved by trigger (`get_cvlr_recipe`) or pushed by
  `verify_rules` on a matched symptom.

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
   recognize. The author does not have to recognize anything, and the text costs
   tokens only in runs that hit the symptom. This already exists for two kinds of generated
   assertion: `_ADVISED` in `composer/spec/cvlr/verify.py` and the matching paragraphs in
   `composer/templates/cvlr/verdicts.j2`. `cvlr/external_functions.j2` is the same idea for the
   Prover's external-function warnings.
3. **Pulled** (a recipe the agent retrieves from the index). For text keyed on a situation the
   agent recognizes in the program or the property before any run: "the handler performs a CPI",
   "an acceptance property through a `seeds` constraint". Also for symptoms nothing matches.

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

### 3.2 What the author already reads

The full error text reaches the author. An `ERROR` rule carries `error_messages`, collected from the
tree view's error entries (`flatten_tree_view`), and `verify_rules` prints every one through
`TrivialFanoutCexHandler` and `flat_rule_feedback.j2`. A Solana analysis error
(`FormattedErrorMessage` in the Prover's `sbf/support/Errors.kt`) is:

```text
[3308] illegal dereference of an absolute address
source: src/state.rs:118
  from: src/handlers/deposit.rs:42
note: ...
help: ...
```

So the author already has the code, the inlined source chain at the refused instruction, and the
Prover's own explanation and advice. The `help:` text is written for a person with the whole Prover
configuration in reach ("add the function to an inlining file", "unroll the loop by hand"), and part
of it is outside the author's action space. Correcting that is what a pushed recipe is for.

One case where the text did not arrive: above the cex handler's summarization threshold (10
VIOLATED instances, which one `cvlr_rules!` across a dozen handlers reaches), `_report_to_todo_list`
had an LLM rewrite the report as a list of fixes, dropping the messages and reading every violation
as something to fix. CVLR's handler now runs with no threshold (`CexAnalysis.handler`), so the
report always arrives whole.

The codes the prompts teach:

| code | message | what the prompts say it means |
|---|---|---|
| `[3308]` | illegal dereference of an absolute address | pointer analysis refused an object; most often an Anchor `#[error_code]` enum formatting its `#[msg]` |
| `[3003]` | dereference of a register with unknown provenance | the same family; seen through `Rc<RefCell<..>>` in `AccountInfo::lamports` |
| `[3005]` | memcpy with dynamically sized length | a copy whose length the analysis cannot fix: a variable-length collection or a derived (de)serializer |
| `[3006]` | illegal store of a stack pointer | the same family |

What has no code or kind:

* **Job-level failures.** The job's `alertReport.json` carries them, and a failed job's `ERROR`
  alerts already reach the author: `CloudJobError.alerts`, read by `_failure_advice` in
  `composer/prover/core.py`, which tells a resource limit, a Solana encoding failure and a crash
  apart by the alert's `type`.
* **The cause of a vacuity.** A `SANITY_FAILED` rule does not say *why* no path reaches the end. The
  commonest measured cause, the loop that drops the 16-account array, does show indirectly: in
  multi-assert mode the sibling `ProverInternalChecks` subrule is usually VIOLATED with
  `assertOrigin: loopUnwinding` (§3.5).
* **"Cannot overflow" before `assertOrigin`.** Older Provers report it as an unclassified assertion,
  which `classify_violation` treats as the rule's own.

### 3.3 Typing only where a decision depends on it

`assertOrigin` had to be typed because it decides something: whether a violation is a finding or a
Prover limit, which drives stamping and `expect_rule_failure`. Getting that from message text would
make the gate's correctness depend on wording.

No such decision depends on an analysis error's kind. An `ERROR` rule is unaccounted whatever its
kind. The kind only chooses which advice to push, and a wrong choice costs a paragraph of
irrelevant text that the author can see is irrelevant. For that, the `[NNNN]` code is enough. It is a
closed vocabulary the Prover documents to its users, printed in a fixed position, and every error
the recipes are about has one.

A typed `errorKind` from the Prover was drafted and set aside (§3.5). Beyond renaming the codes it
added kinds for the errors without a code, which are setup failures in what AutoProver itself
supplied (stripped symbols, a missing or unparsable summary or inlining file) rather than anything a
recipe fixes, and function names on each source frame, which the author can read off the
`source:` chain with its source tools.

Revisit this if a benchmark run shows the code choosing the wrong advice, or a recipe that needs to
know *which* function was refused to be useful.

### 3.4 AutoProver side

* **An analysis error's kind, from its code**, in one function next to `assert_origin` in
  `composer/prover/results.py`:

  ```python
  class AnalysisErrorKind(IntEnum):
      UNKNOWN_PROVENANCE_DEREF = 3003
      DYNAMIC_MEMCPY = 3005
      STACK_POINTER_STORE = 3006
      ABSOLUTE_ADDRESS_DEREF = 3308
      # ... the codes a recipe is pushed on

  @dataclass(frozen=True)
  class AnalysisError:
      kind: AnalysisErrorKind | UnknownCode | Uncoded
      message: str
  ```

  `UnknownCode(code: int)` is a code this build does not list, `Uncoded` a message with no `[NNNN]`
  prefix. `RuleResult.error_messages` becomes `errors: list[AnalysisError]`, and the report keeps
  printing each `message` whole. Since EVMVerifier#8815 an encoding failure's message starts with
  the code; a Prover from before it prefixes `In <rule>: `, so the parser accepts that prefix too.
  One test per listed code, against a message in the Prover's format, and one with the old prefix.
* **Timeouts.** `RuleStatistics.phase` becomes a type (`StaticAnalysis | Solving(...) |
  OtherPhase(str)`), so "the rule never reached the solver" can be matched rather than compared to a
  string.
* **Failed jobs.** `ErrorAlert.type` already distinguishes the cases `_failure_advice` needs. It
  becomes a type the same way when a recipe is pushed on it.
* **Vacuity.** K12 is pushed on `SANITY_FAILED`. It names the drop loop as the commonest cause
  without evidence from the run: the `ProverInternalChecks` subrule of §3.5 exists only in
  multi-assert mode, which CVLR's conf does not turn on. The direct signal is EVMVerifier#8803
  (draft), a warning on a vacuity check that failed without reaching the solver, listing the loop
  unwinding conditions in the checked program. Read it into the K12 note when that merges.

### 3.5 Prover asks, if they become needed (November 2026)

None of these is needed for this plan (§3.3). They are recorded, from reading the Prover's code
(EVMVerifier), so the investigation is not repeated if one is. Per-rule data belongs on the rule's
tree view node, which is where AutoProver already reads per-rule results; a job-level failure belongs
in `alertReport.json`, which exists when the tree view may not and which AutoProver already reads for
a failed job.

**Ask 1: a typed error kind and source frames. Drafted, set aside.** EVMVerifier#8815 first added
them, then was trimmed to two fixes to the existing text, and merged in that form on 2026-11-10
(7c1068d8). `Symbol.toRange` no longer discards a source location whose column is 0, so more errors
get a `source:` line. An encoding failure is now reported as the original exception, not through a
wrapper that logged it as "Unknown error ... Error code N". As a result the message loses its
`In <rule>: ` prefix; the `[NNNN]` prefix is unchanged. The draft, for reference: each entry in a
rule node's `errors` gained two fields beside `severity` and `message`, and job-level alerts carried
the same fields:

```json
{ "severity": "error", "message": "[3308] illegal dereference ...",
  "errorKind": "absoluteAddressDeref",
  "frames": [ {"function": "...", "file": "src/lib.rs", "line": 42} ] }
```

* `frames` is the inlined source call chain at the refused instruction, innermost first, from
  `llvm-symbolizer`, with the function names the message's `source:` chain leaves out. The innermost
  frame is often in `core`/`alloc`, so a consumer would pick the first frame in the program's own
  sources. `function` is `null` when DWARF has no name, and `frames` is empty for errors with no
  instruction (no assertion, heap exhausted).
* The vocabulary is `SolanaErrorKind` in `sbf/support/Errors.kt`. One kind per code:
  3001 `indeterminateStackOffset`, 3002 `unknownStackContent`, 3003 `unknownProvenanceDeref`,
  3004 `unknownProvenanceStore`, 3005 `dynamicMemcpyLength`, 3006 `stackPointerEscape`,
  3307 `indeterminateGlobalDeref`, 3308 `absoluteAddressDeref`, 3309 `conflictingHeapUsage`,
  3310 `stackNotScalarizable`, 3311 `undecidedAllocFree`, 4000 `noAssertion`,
  4001 `noAssertionAfterSlicing`, 4002 `noSatisfyAfterSlicing`, 5000 `summaryFileSyntax`,
  5001 `inliningFileSyntax`, 6000 `stackFrameExceeded`, 6001 `heapExhausted`,
  7000 `unsupportedCall`, 7001 `unsupportedIndirectCall`. Errors with no code also get a kind:
  `missingRuleFunction`, `strippedSymbols`, `missingConfigurationFile`, `summaryShapeMismatch`,
  `invalidStackOffset`.
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
* Shape to propose: a `kind` on the job-level alert in `alertReport.json`, set from the catch blocks
  in `EntryPoint.kt` and the global-timeout handler. The tree view is not the place: a job that fails
  before the rule loop may not write one, and AutoProver reads only the alert report for a failed
  job. Kinds fall out of those blocks: `globalTimeout`, `userError`, `internalError`,
  `unexpectedException`, `badArguments`.

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
* Available now, with no Prover change, but only in multi-assert mode, which CVLR's conf does not
  turn on: the sibling `ProverInternalChecks` subrule is usually VIOLATED with
  `assertOrigin: loopUnwinding` when a drop loop after the rule's assertion always exceeds the bound.
* In progress: EVMVerifier#8803 (draft) warns on a vacuity check the presolver decided, with the
  loop unwinding conditions of the checked program, and covers the drop-loop case without a new
  field.

**Ask 5: per-function cost on Solana rules.** Measured, not yet investigated in the Prover. The
hotspot lists in `rule_live_statistics_*.json` (which function holds the branching, and which the
nonlinear operations) came back for 6 of the 37 timed-out instances in the November benchmark (§8).
The other 31 say `failed to compute per-call stats`, have an empty `path count hotspots` and no
`nonlinearity hotspots`. Both lists come from the rule's call structure
(`statistics/data/SingleDifficultyStats.kt`, `DifficultyStats.kt`): the per-call section fails when
the root call has no callees, and a procedure's share is how much the path count drops when its call
sites are jumped. So for most Solana rules the Prover is missing the program's procedures, not
finding that no function dominates. The same rule can get the list on one node and not on its
sibling (the `Assertions` node versus the vacuity check). Until that is found, AutoProver renders the
totals that are always there (§8).

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

## 4. Pushing recipes on matched symptoms

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
  `generated: GeneratedKind`, `error: AnalysisErrorKind` (written as the code, `error: 3308`),
  `timeout: <phase kind>`, `job_failure: <alert type>`, `status: SANITY_FAILED`. CVL's index never sets it. The kind names in the
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
| The CPI model, for the judge | judge §1, twice: under "Judge authorship, not depth" and in the paragraph beginning "For a CPI — the common case"; both restate the system prompt's | the judge keeps what to check (rules asserting over a value a CPI moves, with nothing modelling the move); the mechanism moves to K6. Done with step 3's follow-up: K6 also names the token models (`spl_token_transfer`, `spl_mint_to`, `spl_burn`) the judge requires, which the author was never told of |

Also fix the cross-reference in the system prompt's editor section: "`swap_derive` ... has its own
section below" points at text that is in the baseline facts, not below. It goes away when that text
moves to K11.

---

## 6. Moving text into recipes

New recipes, continuing the K series. "Source" is where the text is now. "Push" is the matched
symptom from §4 that attaches it, where one exists; otherwise it is pulled only.

| id | recipe | source | trigger in the index | channel | push |
|---|---|---|---|---|---|
| K4 | A loop the bound does not finish | system prompt "The loop ladder", conf section, baseline facts "Loops are bounded" | a rule stops on `Unwinding condition in a loop` | CONF / RULE | `generated: loopUnwinding` |
| K5 | `Cannot overflow` on an inferred pointer | system prompt conf section; `verdicts.j2` | a rule stops on `Cannot overflow: inferred pointer` or `computing TAC map index` | RULE / SKIP | `generated: cannotOverflow` |
| K6 | Taking a CPI out from under a handler | system prompt "Reaching a handler" (the second move), "Writing the stand-in: two things that do not analyze" | a handler performs a CPI and the property is its own bookkeeping; `[3003]` with `alloc/src/rc.rs` in the trace; a VIOLATED rule over a value decoded from an instruction's bytes | EDIT | — (`[3003]` is pushed to K10's family only if a run shows K6 is not fetched) |
| K7 | Descending to the program's state-transition core | system prompt "Reaching a handler" (the first move, and its three conditions) | a handler-level rule blocked by a CPI, by validation or by error formatting, where the program has an accounting function below the handler | RULE | — |
| K8 | A bounded stand-in for a computation that will not finish | system prompt "When a computation will not finish"; initial prompt Step 3, the timeout bullets about a rule alone still halting | a TIMEOUT that survives a small job; a statistics line with phase `static analysis`, or one function holding most of the branching or nonlinear share | MOCK / EDIT | `timeout: static analysis`, and a hotspot share over a threshold |
| K9 | A halted job, and which rule halted it | initial prompt Step 3, the timeout bullets (submit without the last rule; the measured cases: a `HashSet` insert, a bincode walk of a `Pubkey`, a `u64 × u64` product, per-length splits, per-epoch walks) | a job that halts with no verdicts; a rule that times out alone | RULE / EDIT / SKIP | `job_failure: Out of Resources` |
| K10 | A rule the pointer analysis refuses | initial prompt Step 3, the "could not analyze" bullet (`summarize_for_prover`, the three-directive limit, what a summary drops, weaken or skip) | `[3308]`, `[3006]` on a rule | MOCK / SKIP | `error: 3308`, `error: 3006` |
| K11 | State the analysis cannot size: variable-length collections and `swap_derive` | baseline facts "When the Prover cannot follow the program's own data" | `[3005]`; an internal error out of `ScalarDomain`; a job that fails with no rule results | EDIT / SKIP | `error: 3005` |
| K12 | Diagnosing a vacuous rule | initial prompt Step 3, the vacuity bullet (`explain_vacuity` first, then a copy ending in `cvlr_assert!(false)`, not `cvlr_satisfy!`) | a rule reported `SANITY_FAILED` | RULE | `status: SANITY_FAILED` |
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
  needs them too, for the judge and for a report from an older Prover without `assertOrigin`.

What stays standing (§2, channel 1): output shape, the tools list, the conf, the handler idioms
named there, "When a rule should fail", and the judge's checklist.

---

## 7. Order of work

1. **Duplicates (§5).** Prompt-only. Removes about 6 KB from the author's context. Until K4 exists,
   the loop ladder's one copy is the baseline facts' section, which the author and judge both load
   and the judge already cites; the system prompt keeps the heading "The loop ladder", which the
   Solana skill sends its agent to, with a pointer to that section. So step 1 removes about 2 KB,
   and the rest comes off when step 2 moves the ladder into K4.
2. **Push mechanism (§4)** with K4 and K5, replacing `_ADVISED` and the paragraphs in `verdicts.j2`.
   Behaviour-preserving for those two kinds, so a good check of the mechanism. Done: `pushed_on` is
   a field of `CvlrRecipe` only, so the CVL index cannot set it, and it has one symptom family so
   far (`generated:`); the others join it as their steps land. The baseline facts keep a short
   "Loops are bounded" section with the facts and a pointer to K4, and the judge's §5 cites K4.
3. **Pulled recipes that need nothing new:** K6, K7, K13, K14, and the move into
   `invariant_guidance()`. Done. Each moved passage leaves a pointer that keeps what shapes a first
   draft: the two moves past a CPI and that the descent must reach the program's own function, what
   neither move can show, that `seeds` acceptance is a skip, and the bumps-type rule (with this
   program's names when the analysis supplies them). The system prompt went from 47 KB to 36 KB.
4. **Analysis errors by code (§3.4)**, then K10 and K11 pushed on them. Done, with different
   names from §3.4's sketch: a message is a `ProverMessage(code, text)` in `composer/prover/ptypes.py`,
   since SKIPPED rules' warnings ride in the same list, and `RuleResult.error_messages` became
   `messages`. `ProverReport.errors` carries each ERROR instance's messages to `verify_rules`. K11
   took the baseline facts' "When the Prover cannot follow the program's own data"; what that section
   said about munges in general stays there, as "Asking for a munge".
5. **K8, K9, K12**, pushed on the statistics phase, the failed job's alert type and
   `SANITY_FAILED`. Done. K8 is pushed on `timeout: static analysis` (`RuleStatistics.phase` is
   now `StaticAnalysis | Solving | OtherPhase`) and on `timeout: dominant function`, when one
   function holds at least `DOMINANT_SHARE` (40 %) of a timed-out rule's branching or nonlinear
   operations. The 40 is provisional: it separates a stand-in's 256-bit division (44–65 %) from
   handler logic (at most 28 %) in a handful of klend jobs (`composer/spec/cvlr/verify.py`), and
   `verify_rules` logs every timed-out rule's top share so a benchmark can move it; the first one could not (§8.1). K12 is pushed on `status: SANITY_FAILED`. K9 is pushed on `job: halted`: from the
   report's `halted` when the job still returned verdicts, and from the failure when it returned
   none. For the second, `run_prover` returns a `ProverFailure` (`JobFailed | ResultsUnavailable |
   RunFailed`) rather than a string; a `JobFailed` that is `HALTED`, or carries an "Out of
   Resources" alert, pushes K9. The CVL callers render the failure where it leaves `run_prover`, so
   their contract, including the plugin protocol's, still reports a failure as a string.

   Follow-up: the measured timeout cases were in three places (Step 1 of the initial prompt, its
   Step 3, and K9), and K9 held single-rule timeouts it was never pushed for. K9 now holds only the
   halted job: isolating the rule, and the handlers that halt alone (a loop whose trip count comes
   from data). The single-rule shapes (split by case, bound a wide product, bound an unbounded walk)
   open K8, which is also pushed on `status: TIMEOUT` in a job that did not halt; before, a rule
   that timed out while solving, with no dominant function, got no recipe. The signer walk that
   needs `loop_iter` at the account-list length moved to K4. Step 1 states the three shapes as
   principles and Step 3 says which recipe answers which timeout.

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

### 8.1 The November benchmark (2026-11-11)

One pair of runs on the klend liquidation unit (33 properties), from the same pinned analysis and
properties, on fresh copies of the input, $150 each, run side by side. "Before" is the code before
step 1 of §7; "after" is the code with steps 1–5 and their follow-ups.

| | Before | After |
|---|---|---|
| Rules delivered | 25 (24 good, 1 marked) | 42 (41 good, 1 marked) |
| Properties covered / skipped | 21 / 12 | 24 / 9 |
| Wall clock, spend (log estimate) | 3h 47m, about $23 | 5h 05m, about $27 |
| Prover jobs | 8 | 10 |
| Author's system prompt | 55.1K characters | 39.2K characters |
| Author's first-call input | 63.1K tokens | 55.4K tokens |
| Recipes pushed | none (no mechanism) | K4 once, K8 five times, K9 once |
| Recipes fetched: author / judges | none / none | K14, K7, K1, K3 / K8 twice, K14, K13, K9 |

Three earlier runs on the same unit delivered 34 to 42 rules with 9 to 12 skips. So "before" is the
weakest of the five and "after" ties the best; one pair cannot separate the change from run to run
variation.

Against the criteria above:

* **Recipes left unfetched where their trigger occurred.** None found. The author fetched the four
  recipes that hold text the standing prompt used to carry (descent, `try_accounts`, satisfy,
  sysvars). `[3003]` occurred in both runs; in "after" it came from a `try_accounts` rule over 20
  accounts, not the CPI case K6 covers. No `[3308]`, `[3006]` or `[3005]` occurred. `[3001]` in
  the `uint` crate occurred in both runs, and no recipe covers it.
* **New reimplementation.** None. Each run made one source edit, both extracting the same decision
  statement in the liquidation handler.
* **New skips.** "After" skipped two properties "before" covered. One is individual deleveraging's
  eligibility, and it is the case K8 should have prevented. "Before" wrote one rule per liquidation
  reason. All six timed out, with the 256-bit division at 92 to 96 % of their branching. It then
  fixed the obligation's deposited value at one, and all six verified. "After" wrote one rule
  matching on every reason, which never reached the solver. It got no function share to act on,
  and skipped the family; K8 was pushed but described neither move in terms that fit. K8 now has
  both, and Step 1 of the initial prompt names them. The other, that a liquidation cannot be
  blocked by the withdraw queue or caps, was skipped on `[3001]` and a `[3308]` error path.

What it says about the provisional parts:

* **K8 pushed after the split.** After a pushed K8 the author split a combined rule into one rule
  per reason, which is K8's first move. It did not do the same for the eligibility rule above.
* **`DOMINANT_SHARE`.** Not movable from this run. Only 6 of 37 timed-out instances carried a
  function share (76 to 96 %, all above 40); the rest had none (§3.5, Ask 5). Their statistics
  still carry totals: the eligibility and order rules 600 to 720 nonlinear operations of degree 336,
  the socialize-loss and permission rules no nonlinear operations but 202 to 1,296 memory updates in
  one sequence. The stuck-rule line now shows the totals the Prover rates above low
  (`RuleStatistics.nonlinearity`, `.memory`), and K8 says how to read them.
* **K7** was fetched while planning, before the first draft, so there is no evidence yet that it
  belongs back in the standing prompt (§9).

Both authors' threads were summarized during the run, so a thread's latest checkpoint holds only the
history since its last summary. The counts above merge every checkpoint.

## 9. Open questions

* **Repeated pushes.** If a unit hits `loopUnwinding` on every submission, the body is repeated
  every time. Once per thread is cheaper, but a summarization can drop the first copy. Measured in
  §8.1: K8 (6.7 KB) was pushed on five submissions, about 8K tokens of repeated text in one thread
  of 27M input tokens. Small next to the thread, so not yet worth the summarization risk.
* **Recipes for the editor.** The munge editor (`cvlr_munge_editor_system.j2`) has its own copy of
  some of this (what each kind is for). It does not hold `get_cvlr_recipe`. Whether it should is a
  separate question.
* **Whether K7 is a recipe at all.** Descending below the handler is the commonest successful move
  in shipped verification projects. If the benchmark shows authors not fetching it until after a
  blocked handler-level rule, it goes back into the standing prompt. In §8.1 the author fetched it
  while planning, with K14, K1 and K3, before its first draft; one run, so not settled.
