# The Rust IoC loop: what it does, why it exists, and how to remove it

Design note. Describes the inversion-of-control (IoC) effect loop that connects the
Python host (`composer/rustapp`) to a Rust backend wheel (`autoprover-sdk` +
`crucible-app`/`example-app`), why it is shaped the way it is, and a concrete option for
**eliminating it** in favour of a small set of *pure* Rust callouts driven directly by the
Python core pipeline. No code change yet — this is to decide the shape.

## 1. What the loop is (mechanics)

A Rust backend does not run its own control flow. It is a **pure synchronous decider**:
a state machine that, given the result of the last effect, returns the next *command* to
perform. Python owns the actual work (LLM turns, subprocesses, caching, events) and the
async event loop.

The FFI surface a wheel exports (`export_app!`, [autoprover-sdk/src/lib.rs](../rust/autoprover-sdk/src/lib.rs)):

| Function | Kind | Role |
|---|---|---|
| `descriptor() -> json` | **pure** | phases, event kinds, args, artifact layout, ecosystem, backend tag |
| `validate_preconditions(args) -> str?` | **pure** | e.g. "is there a `Cargo.toml`?" |
| `new_session(input) -> RustSession` | **stateful** | build a formalization state machine |
| `new_setup_session(input) -> RustSession?` | **stateful** | build the program-wide setup state machine (Crucible fixture) |
| `RustSession.resume(obs_json) -> cmd_json` | **stateful** | **the loop step** — one decision per call |
| `fetch_verdicts(input) -> json` | **pure** | per-unit report verdicts |
| `finalize(outcomes) -> json?` | **pure** | run-level artifact files |

Everything except the **session** (`new_session`/`new_setup_session` + `resume`) is
already a pure, stateless callout. The loop is the one stateful exception.

### The step protocol

The Rust session speaks two closed enums ([lib.rs](../rust/autoprover-sdk/src/lib.rs)):

- **`Command`** (Rust → Python): `CallLlm`, `RunProver`, `RunCommand`, `RunFeedback`,
  `CacheGet`, `CachePut`, `Emit`, and the terminals `Publish` / `GiveUp`.
- **`Observation`** (Python → Rust): `Start`, `LlmReply`, `ProverResult`,
  `FeedbackResult`, `Cached`, `CommandResult`, `Ack`.

The Python driver `drive_session` ([composer/rustapp/loop.py](../composer/rustapp/loop.py))
is the whole loop:

```python
observation = {"kind": "start"}
for _ in range(max_steps):
    command = json.loads(session.resume(json.dumps(observation)))   # sync FFI hop
    match command["kind"]:
        case "call_llm":    text = await effects.call_llm(command["messages"]); observation = {"kind": "llm_reply", "text": text}
        case "run_command": r = await effects.run_command(...);               observation = {"kind": "command_result", ...}
        case "emit":        await effects.emit(...);                          observation = {"kind": "ack"}
        case "publish":     return RustFormalized(command["result"])          # terminal
        case "give_up":     return GaveUp(command["reason"])                  # terminal
        ...
```

`Effects` is a protocol (`call_llm`, `run_command`, `run_prover`, `run_feedback`,
`cache_get/put`, `emit`), so a fake drives the decider in tests and `RealEffects`
([adapter.py](../composer/rustapp/adapter.py)) drives it in production.

### A session in practice

Crucible's `BatchSession` ([crucible-app/src/lib.rs](../rust/crucible-app/src/lib.rs)) is a
`{Start, AwaitDraft, AwaitFuzz, Done}` state machine holding `test_src`, `attempts`, `cur`,
`verdicts`. One conceptual procedure —

> author all tests → for each feature: build+fuzz → interpret → (retry the *whole* harness
> on a build error) → publish the verdicts

— is spread across `resume` arms, one per effect boundary, with the state threaded by hand
between calls. What is logically a `for` loop with a retry is expressed as a resumable
coroutine hand-compiled into an enum.

## 2. Why it exists (the rationale — all still real)

1. **Decide/do split across the FFI, with no async bridge.** All async I/O (LLM, subprocess,
   Postgres, event streaming) lives in Python; Rust stays pure and synchronous. Every
   `resume` is a fast blocking call, so there is **no `pyo3-async`/tokio bridge** and no Rust
   async runtime. This mirrors the `PureFunctionGenerator` decide/do split the CVL author
   already uses in Python — relocated across the language boundary.
2. **Testability.** Because effects are a protocol, the Rust decider's logic runs against a
   fake with canned command results — no LLM, no toolchain
   ([test_crucible_events.py](../tests/test_crucible_events.py)).
3. **The command-line security invariant.** `RunCommand` carries a **decider-authored**
   `program` + `args`; only file *contents* may be LLM-derived. The IoC boundary makes this
   explicit and enforceable: the LLM never chooses what runs (see the `RunCommand` doc
   comment and [command-sandbox.md](./command-sandbox.md)). **Any replacement must preserve
   this.**
4. **A backend-agnostic host.** The generic Python host drives *any* wheel through the same
   loop + descriptor, so "a new backend is just a wheel." The `Command`/`Observation` protocol
   is the uniform contract.

## 3. What it costs

- **State-machine bookkeeping.** Each backend re-expresses a straight-line author-gate
  procedure as a resumable enum: explicit stages, hand-threaded fields, an `emit`-queue
  shim (`Emitter`) to fire events *between* real commands. `BatchSession` is ~150 lines of
  plumbing for a loop.
- **Two orchestration layers.** The Python core pipeline (`composer/pipeline/core.py`)
  already orchestrates phases (analysis → extraction → formalize → report) with real async
  and a result cache. The IoC loop is a *second*, nested orchestration inside `formalize`,
  with its own mini-cache and event channel. Two models to understand.
- **The control flow is invisible where you look for it.** The interesting logic — author,
  gate with a CLI, retry on failure, publish — is scattered across `resume` arms in Rust and
  a dispatch `match` in Python, joined only by JSON round-trips. Neither side reads as the
  procedure it is.
- **Ceremony per step.** Every decision is a JSON serialize → FFI hop → deserialize, plus a
  `Command`/`Observation` variant to define and thread on both sides.

## 4. The key observation

**The decider carries no state that Python couldn't hold, and needs no control flow Python
can't express.** All real work is already in Python; the Rust "decisions" are pure functions
of the accumulated state (the draft, the attempt count, the command outputs). Resumability
is not intrinsic — it is an artifact of expressing an *author→gate→retry→publish* loop as a
coroutine that happens to suspend at each effect. Move the loop to Python and Rust needs only
a handful of **pure functions** at the decision points.

And the shape is uniform across today's backends:

| Backend / session | author | gate command(s) | interpret | publish |
|---|---|---|---|---|
| Crucible setup | fixture | `crucible run … --dry-run` | build error? → revise | fixture source |
| Crucible batch | all invariant tests | `crucible run <program> <feat>` per feature | `[FUZZ_FINDING]`→BAD else GOOD; build error→revise all | verdicts + property map |
| echoprover (demo) | spec (or cache hit) | `RunProver` | verified? | rules |

All three are the same template: **author (with bounded revise-on-failure) → run
decider-authored gating command(s) → interpret results → publish or give up** — exactly the
author-gate loop the CVL/foundry Python backends already run.

## 5. Proposal: replace the state machine with pure callouts + a Python driver

Keep the four already-pure callouts (`descriptor`, `validate_preconditions`,
`fetch_verdicts`, `finalize`). **Delete** `new_session`/`new_setup_session`/`resume`, the
`RustSession` pyclass, the `Command`/`Observation` enums, and `drive_session`. **Add** a
small pure-callout contract per session kind, and drive it from one generic Python loop that
lives in the core pipeline.

### The callouts (pure `json → json`, no session object, no state)

1. `prompt(input, attempt, draft?, error?) -> {system?, instruction}`
   The author prompt for attempt *N* — initial when `attempt == 0`, else the revise prompt
   built from the prior `draft` + `error`. (Absorbs `author_prompt` + `revise_suffix` +
   cheat sheets.)
2. `gate(input, draft) -> [Effect]`
   The **decider-authored** gating commands for a draft, where
   `Effect = Shell{program, args, files} | Prover{spec, config, rules} | Feedback{…}`.
   *This is where the command-line security invariant lives* — `program`/`args` come from
   compiled Rust, never the LLM. (Absorbs `fuzz_command` / `RunProver`.)
3. `interpret(input, draft, results) -> {status: ok | retry | give_up, error?, reason?}`
   Classify the gate results: pass, retry (with the error text to feed the next `prompt`),
   or give up. (Absorbs `is_build_error`, `[FUZZ_FINDING]` detection, the attempt policy is
   Python's.)
4. `publish(input, draft, results) -> Formalized`
   Assemble `artifact_text` + `property_units` + `verdicts` from the winning draft and its
   results. (Absorbs `publish`.)

### The generic Python driver (in the core pipeline)

```python
async def author_gate(mod, session_input, effects, *, max_attempts, emit) -> Formalized | GaveUp:
    draft, error = None, None
    for attempt in range(max_attempts):
        draft = await effects.call_llm(mod.prompt(session_input, attempt, draft, error))  # async: Python
        results = [await run_effect(effects, e) for e in mod.gate(session_input, draft)]   # subprocess+sandbox: Python
        verdict = mod.interpret(session_input, draft, results)                             # pure: Rust
        if verdict.status == "ok":       return mod.publish(session_input, draft, results) # pure: Rust
        if verdict.status == "give_up":  return GaveUp(verdict.reason)
        error = verdict.error                                                              # retry
    return GaveUp(f"did not converge in {max_attempts} attempts")
```

`run_effect` dispatches a `Shell`/`Prover`/`Feedback` effect to the existing
`RealEffects.run_command`/`run_prover`/`run_feedback` — unchanged, still exec-not-shell,
still sandboxed. Progress **events and caching move to Python**: the driver already knows the
phase and each command's result, so it emits the `verdict`/`build`/`fuzz` notices itself
(the wheel no longer needs an `Emit` command or the `Emitter` shim), and the pipeline's
existing result cache subsumes the loop's scratch `CacheGet`/`CachePut`.

`CrucibleFormalizer.formalize` then becomes: prepare the crate, then `author_gate(...)` — the
control flow is one readable Python function instead of a Rust enum plus a JSON dispatcher.

### What each side ends up owning

- **Rust:** prompts, the command line (security), result interpretation, result assembly —
  all pure functions, unit-testable in Rust with no Python.
- **Python:** the author-gate loop, retries/budget, all async effects, sandboxing, caching,
  events — one orchestration model, shared with the CVL/foundry backends.

## 6. Trade-offs and risks

- **Loss of arbitrary per-backend control flow.** The IoC loop is Turing-complete; the
  callout model fixes one template (author → gate → interpret → publish, with bounded
  revise). All *current* sessions fit it, but a future backend wanting a genuinely different
  flow (multi-phase, branch-and-minimize a counterexample, adaptive strategies) would need a
  new template callout, not a free-form state machine. **This is the real decision:** is the
  author-gate template enough for every backend we foresee? If yes, the loop is
  over-general; if we expect exotic flows, the loop earns its keep.
- **Incremental emission is slightly coarser.** Today Crucible emits a live verdict as *each*
  feature finishes fuzzing. With `gate` returning all commands and `interpret` classifying
  them together, Python would emit verdicts after the batch — unless we keep a tiny
  `classify(one_result) -> outcome` callout so the driver can emit per-command as it goes
  (a fifth pure function; cheap).
- **Migration touches both languages.** Delete the enums/loop/pyclass, add the callouts,
  rewrite `RealEffects` into a small `run_effect` dispatcher, and re-express both Crucible
  sessions and the echoprover demo as callouts. Bounded, but not a one-liner.
- **`example-app` cache short-circuit.** echoprover's `CacheGet → maybe skip LLM` becomes a
  Python-side "check the pipeline cache before authoring" — arguably clearer, and removes the
  redundant second cache.
- **The security invariant must be re-audited** at its new home (`gate`), with a test that
  the LLM draft cannot influence `program`/`args`.

## 7. Recommendation

The four pure callouts already outnumber the one stateful surface, and every current session
is an author-gate loop wearing a state-machine costume. Moving the loop into the Python core
(where the sibling CVL/foundry backends already orchestrate the identical shape) removes a
whole protocol, both `Emitter`/scratch-cache shims, and the hand-compiled enums — at the cost
of committing to the author-gate template. Recommended **if** we accept that template as the
backend contract.

Suggested staging (each shippable):

1. **Add the callouts alongside the loop.** Implement `prompt`/`gate`/`interpret`/`publish`
   for Crucible's `BatchSession` and `SetupSession` as pure functions *next to* the existing
   `resume` (they can share code). No behaviour change yet.
2. **Add `author_gate` to the core pipeline** and switch `CrucibleFormalizer` to it behind a
   flag; verify against the e2e gate (identical verdicts).
3. **Port echoprover**, then **delete** `resume`/`RustSession`/`Command`/`Observation`/
   `drive_session` and trim `Effects` to `call_llm` + `run_effect`.

## 8. Open questions

- Is the author-gate template sufficient for the backends we actually plan (Crucible,
  soroban, a CVL-in-Rust prover)? Any that need branching multi-phase flow?
- Keep the per-result `classify` callout for live per-unit emission, or accept batch-end
  emission?
- Should `gate` return **all** commands up front (enables Python to parallelize them —
  ties into the `--binary-in` fuzz-parallelism lever in
  [crucible-unit-granularity.md §7](./crucible-unit-granularity.md)) or one at a time?
- Does any backend need Rust to hold state *between* gate commands that isn't recoverable
  from `(input, draft, results)`? (None does today.)
