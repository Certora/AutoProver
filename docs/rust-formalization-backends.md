# Design Doc — Rust Formalization Backends via PyO3

> How to implement an AutoProver formalization backend in Rust and plug it into the
> generic Python pipeline through PyO3, what the boundary looks like, the additional
> work required to let Rust call *back* into the async Python services, and a
> hypothetical sketch of the CVL prover backend rewritten in Rust.
>
> Companion to [formalization-abstraction.md](./formalization-abstraction.md), which
> defines the backend seam this leans on, and
> [application-abstraction.md](./application-abstraction.md), which covers how a backend
> is wired into a runnable application. Read the formalization doc first — this document
> assumes its vocabulary (`FormT`, `Formalizer`, `PreparedSystem`, the phase chain). For
> the *rest* of the vertical around a Rust backend — phase enum, entry point, frontend,
> `main()` — see [rust-applications.md](./rust-applications.md).

---

## 1. Problem & motivation

The formalization seam ([formalization-abstraction.md §3](./formalization-abstraction.md))
is deliberately narrow: a backend is any object that structurally satisfies the
`PipelineBackend` protocol and hands the generic driver three immutable phase objects
(`PipelineBackend → PreparedSystem → Formalizer`). The driver in
[composer/pipeline/core.py](../composer/pipeline/core.py) never imports a concrete
backend — it moves opaque `FormT` values around and never reads a field.

We want to author backends (or performance-critical parts of them) in **Rust**: a native
verification engine, a fast artifact transformer, a solver driver, or a
whole-backend reimplementation that only borrows the shared analysis/extraction/report
machinery. **PyO3** is the bridge — it lets a Rust crate expose functions and classes
that Python can call as if they were native.

The seam being structural (a `Protocol`, not a base class you must subclass) is what makes
this tractable: nothing in the driver needs to know a backend is "really" Rust. The
question is entirely about the **boundary** — what crosses it, in which direction, and
synchronously or not.

### Design goals

1. **Confine the PyO3 surface.** The FFI boundary should be as small, synchronous, and
   serde-friendly as the backend allows. Every awaitable, pydantic model, or deep object
   graph that crosses the boundary is a cost.
2. **Reuse the driver unchanged.** Caching, the artifact store, the report, and the
   concurrency structure are driver-owned; a Rust backend inherits them for free
   ([formalization-abstraction.md §7](./formalization-abstraction.md)).
3. **Keep the main tree pure-Python.** The project builds with `setuptools` today
   ([pyproject.toml](../pyproject.toml)); adding Rust should not force a build-system
   rewrite of `ai-composer` itself.

---

## 2. The boundary, and what crosses it

The whole interface a backend must implement is five async methods plus a handful of
properties and one sync mapper ([formalization-abstraction.md §3](./formalization-abstraction.md)):

| Member | Direction | Kind |
|---|---|---|
| `prepare_system` | driver → backend | `async` |
| `PreparedSystem.prepare_formalization` | driver → backend | `async` |
| `Formalizer.formalize` | driver → backend | `async` |
| `Formalizer.fetch_verdicts` | driver → backend | `async` |
| `Formalizer.finalize` | driver → backend | `async` (optional hook) |
| `to_artifact_id`, `extra_report_inputs`, the four properties | driver → backend | sync |

Four properties of this boundary drive the entire design.

### 2.1 It is thoroughly `async`

Every real method is a coroutine, driven by `asyncio.create_task` /
`asyncio.gather(..., return_exceptions=True)` in the driver
([core.py](../composer/pipeline/core.py)). PyO3 does not make Rust `async fn` visible to
Python for free — see [§4](#4-the-async-problem-three-tiers).

### 2.2 The result type must stay cacheable

The driver keys the cache on `formalizer.formalized_type` and calls
`cache_put(result)` / `cache_get(type)` ([core.py](../composer/pipeline/core.py)). Both
existing results — `GeneratedCVL` ([cvl_generation.py](../composer/spec/cvl_generation.py))
and `GeneratedFoundryTest` ([foundry/author.py](../composer/foundry/author.py)) — are
pydantic v2 `BaseModel`s that serialize cleanly. A raw `#[pyclass]` result would have to
satisfy *both* structural protocols (`FormalResult` + `ReportableResult`) **and**
round-trip through the cache's (de)serialization.

> **Decision:** keep `FormT` a Python pydantic model. Rust does the work and returns
> plain data; the pydantic result is constructed on the Python side (or PyO3 instantiates
> the pydantic class). Caching, the artifact store, and the report all keep working
> unchanged.

### 2.3 The inputs are a deep object graph

`formalize` receives a `ContractComponentInstance` (a dataclass wrapping a pydantic
`SourceApplication` / `HarnessedApplication` graph — [system_model.py](../composer/spec/system_model.py)),
a `list[PropertyFormulation]`, a `WorkflowContext`, and a `PipelineRun`. Reading these
from Rust via GIL-bound attribute access is verbose and brittle.

> **Decision:** marshal at the boundary. The thin Python adapter serializes the *slice*
> the Rust backend needs (`model_dump()` / JSON) and passes that in; Rust deserializes
> into its own `serde` structs and never touches Python objects directly.
> `PropertyFormulation` and the result models are trivially JSON-able.

### 2.4 It is a `Protocol`, not a base class

Neither `ProverBackend` nor `FoundryBackend` inherits from `PipelineBackend` — they match
by shape. So the "backend" the driver sees can be a **thin Python adapter** that
implements the protocol and delegates to the Rust extension. That adapter is where all the
async-wrapping, marshalling, and pydantic-construction live.

---

## 3. Recommended architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ composer/pipeline/core.py  (generic driver — UNCHANGED)          │
└───────────────┬─────────────────────────────────────────────────┘
                │ holds an opaque PipelineBackend[...]  (structural)
                ▼
┌─────────────────────────────────────────────────────────────────┐
│ composer/rustbackend/adapter.py   (thin PYTHON adapter)          │
│  • implements the async protocol methods                         │
│  • async def formalize(...):                                      │
│       payload = _marshal(feat, props)          # pydantic → JSON  │
│       raw = await asyncio.to_thread(_rs.formalize, payload)       │
│       return GeneratedRustResult.model_validate(raw)  # → pydantic│
│  • keeps FormT a pydantic BaseModel                               │
└───────────────┬─────────────────────────────────────────────────┘
                │ sync, serde-friendly FFI calls
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  _rustbackend  (PyO3 / maturin extension wheel)                  │
│  #[pyfunction] fn formalize(payload: &str) -> PyResult<String>   │
│  • serde_json::from_str → own structs                            │
│  • py.allow_threads(|| heavy_rust_work())                        │
│  • returns serde_json::to_string(&result)                        │
└─────────────────────────────────────────────────────────────────┘
```

This confines the entire PyO3 surface to **synchronous, `&str`-in / `String`-out
functions**, sidestepping three problems at once: no Tokio↔asyncio bridge, no
pydantic-in-Rust, no cache-serialization of a `#[pyclass]`.

### 3.1 Packaging

Two options; the first is strongly preferred.

- **(a) Separate wheel — recommended.** The Rust crate is its own maturin project
  producing a `_rustbackend` extension wheel. `ai-composer` gains one dependency; its
  `[build-system]` stays on setuptools. Least invasive, independently versioned/CI'd.
- **(b) Mixed maturin build for `ai-composer` itself.** Rewrites `[build-system]` and
  `[tool.setuptools.*]`. Only worth it if the Rust and Python are co-developed in
  lockstep.

`requires-python = ">=3.12"` is a hard floor (the seam uses PEP 695 generics), so build
abi3 wheels for `cp312+`. The project is managed with `uv`; add the crate as a `uv`
source (path dep during development, published wheel in CI).

### 3.2 Two small wiring changes (not PyO3-specific)

These are the same steps any new backend takes ([application-abstraction.md](./application-abstraction.md)):

1. Add a wrapper `run_<rust>_pipeline` that constructs the adapter backend and calls the
   generic `run_pipeline` — mirror [foundry/pipeline.py](../composer/foundry/pipeline.py),
   the simplest reference backend, plus a `[project.scripts]` CLI entry.
2. Widen the `ReportBackend` literal — currently a closed `Literal["prover","foundry"]`
   at [report/schema.py](../composer/spec/source/report/collect.py) — to include the new
   backend tag.

### 3.3 GIL and errors

- Release the GIL (`py.allow_threads(|| ...)`) around heavy Rust work, so the
  `asyncio.to_thread` offload in the adapter yields real concurrency across the
  semaphore-bounded per-component fan-out.
- Map Rust `Err`/panics to Python exceptions (`PyResult`, `catch_unwind` at the FFI edge).
- For the *declined* outcome, return the existing `GaveUp(BaseModel)` (`{reason: str}`)
  from the adapter — the driver treats it as a normal, reportable result, **not** a crash
  ([formalization-abstraction.md §8](./formalization-abstraction.md)). Reserve raised
  exceptions for genuine failures the driver should capture via `return_exceptions=True`.

---

## 4. The async problem, three tiers

Extensions **will** need LLM authoring loops — a backend that authors an artifact, runs a
verifier, reads the feedback, and revises, turn after turn, is the whole point. That work
is inherently a dance with async Python services (the LLM, the prover tool, the feedback
judge, the cache). The naive way to give Rust that capability is to let Rust `await` Python
coroutines directly — the full async FFI bridge. **We can avoid that**, because of one fact
about how this codebase is already built.

### 4.0 The enabling fact: the loop already separates *decide* from *do*

The authoring loop is a langgraph `StateGraph` (`initial → tools ⇄ tool_result → __end__`),
but underneath, every node is a **pure generator** ([graphcore/graph.py](../graphcore/graphcore/graph.py)):

```python
type PureFunctionGenerator[ResT] = Generator[list[AnyMessage], BaseMessage, ResT]
```

A node *yields* the messages it wants sent to the LLM and *receives* the reply via
`.send()`. A tiny adapter, `_stitch_async_impl`, performs the one awaited effect
(`res = await llm_impl(d)`) between the yield and the send. **The "decide next action" logic
is already pure and synchronous; only the effect in the middle is async.** The routing
predicates that end the loop — `should_end`, `ai_message_router`, `check_completion` —
are ordinary side-effect-free functions of the state dict
([cvl_generation.py](../composer/spec/cvl_generation.py)).

That split is exactly what we relocate across the FFI boundary. Rust owns the pure decider;
Python keeps owning the async effects. No bridge required.

### 4.1 Tier 1 — self-contained Rust (`asyncio.to_thread`)

If the Rust work does its own thing (spawns a solver, shells out, computes an artifact) and
needs no Python callbacks, the adapter wraps a **synchronous** Rust call in a thread:

```python
async def formalize(self, label, feat, props, ctx, run) -> FormT | GaveUp:
    payload = _marshal(feat, props, self._config)
    raw = await asyncio.to_thread(_rs.formalize, payload)   # sync Rust, off the loop
    obj = json.loads(raw)
    if obj["kind"] == "gave_up":
        return GaveUp(reason=obj["reason"])
    return GeneratedRustResult.model_validate(obj["result"])
```

This is exactly the pattern the CVL backend already uses for its off-thread prover query
([formalization-abstraction.md §4.5](./formalization-abstraction.md)). No new
infrastructure. Good for pure computation, useless for an LLM loop.

### 4.2 Tier 2 — inversion of control: Rust decides, Python does the I/O ⭐

**This is the recommended way to give an extension an LLM authoring loop.** It requires
**no async bridge at all** — the FFI stays 100% synchronous.

Python owns the async event loop and *every* effect (LLM call, prover run, feedback judge,
cache). Rust is a **pure, synchronous state machine**. Python calls one sync FFI function,
`resume(handle, observation) -> Command`; Rust decides the next action and returns a
`Command`; Python performs that async effect and calls `resume` again with the outcome.
This is the classic **sans-I/O** pattern, and it mirrors the `PureFunctionGenerator` split
above one-to-one — the generator's `yield` becomes a returned `Command`, its `.send()`
becomes the next `resume` argument.

**The command vocabulary** (a closed enum, JSON across the wire) is read straight off the
real loop's effects:

```rust
enum Command {                         // Rust → Python: "please perform this effect"
    CallLlm      { messages: Json },   // an LLM turn (initial / tool_result node)
    RunProver    { spec: String, config: Json, rules: Option<Vec<String>> },
    RunFeedback  { spec: String, skipped: Json, rebuttals: Json },  // nested judge agent
    CacheGet     { key: String },
    CachePut     { key: String, value: Json },
    Summarize    { messages: Json },   // history-compaction LLM turn
    Publish      { result: Json },     // ⇒ FormT ; loop ends
    GiveUp       { reason: String },   // ⇒ GaveUp ; loop ends
}

enum Observation {                     // Python → Rust: "here is the result"
    LlmReply(Json), ProverResult(Json), FeedbackResult(Json),
    Cached(Option<Json>), Ack, Start,
}
```

The Python driver is a plain `while` loop with **no bridge, no Tokio, no `pyo3-async-runtimes`**:

```python
async def formalize(self, label, feat, props, ctx, run) -> FormT | GaveUp:
    handle = _rs.new_session(_marshal(feat, props, self._config))   # sync: build Rust state
    obs = _rs.START
    while True:
        cmd = json.loads(_rs.resume(handle, obs))       # sync FFI: Rust decides
        match cmd["kind"]:
            case "call_llm":     obs = await self._llm(cmd["messages"])        # async, in PYTHON
            case "run_prover":   obs = await self._verify_spec(cmd)            # async, in PYTHON
            case "run_feedback": obs = await self._feedback_judge(cmd)         # async, in PYTHON
            case "cache_get":    obs = await ctx.cache_get_raw(cmd["key"])     # async, in PYTHON
            case "cache_put":    await ctx.cache_put_raw(cmd["key"], cmd["value"]); obs = _rs.ACK
            case "publish":      return GeneratedRustResult.model_validate(cmd["result"])
            case "give_up":      return GaveUp(reason=cmd["reason"])
```

Because Python owns the loop, the extension inherits everything for free: `run.runner(...)`
task/telemetry wrapping, the existing `verify_spec` prover tool, the
`property_feedback_judge` sub-agent, the hierarchical cache, cancellation
(`asyncio.CancelledError` just unwinds the Python loop), and structured-concurrency
isolation under `gather(return_exceptions=True)`. Rust contributes only the *policy*: prompt
construction, response interpretation, the validation-gate check, and the publish/give-up
decision.

**Ergonomics for the Rust author — two flavors:**

- **Explicit state machine.** Author writes `fn resume(&mut self, obs) -> Command` over an
  explicit state enum. Simple, zero dependencies, but verbose for a rich loop.
- **Self-driven coroutine (recommended).** Author writes *linear, idiomatic* `async fn`
  Rust, but `await`s custom `HostCall` futures whose leaf `poll` suspends a **Rust-owned,
  single-threaded** executor and hands a `Command` out through `resume`. Python resumes by
  feeding the `Observation` back in. The executor never leaves Rust and never touches
  asyncio, so there is still **no cross-language async bridge** — it's a coroutine whose
  "syscalls" happen to be Python async operations. This gives the write-it-linearly feel of
  the full bridge at the cost of a ~200-line effect runtime, entirely in Rust.

**What Tier 2 must reproduce that langgraph gives for free:**

- **State threading.** The loop state is a flat, reducer-merged dict — `curr_spec`,
  `skipped`, `property_rules`, `validations: dict[str,str]`, `required_validations`,
  `rule_skips`, `config`, `prover_link`, `messages` ([author.py](../composer/spec/source/author.py)).
  Rust holds this as its session struct; each `Command`'s observation merges in exactly as
  the langgraph reducers do today.
- **The validation gates.** Publication is gated by `check_completion`: a content digest of
  `(spec, skipped)` must match a stored digest for each of `required_validations`
  (`["feedback","prover"]`), and **any spec edit invalidates both** by changing the digest.
  Rust owns this predicate — it is pure — and only emits `Publish` when it passes; otherwise
  it emits another `CallLlm` with the rejection reason, exactly as `PublishResultTool` does
  today.
- **A turn budget.** langgraph's `recursion_limit` bounds the loop; Rust owns the counter and
  emits `GiveUp` when it's exhausted.
- **Injection.** Today tools reach state/identity via langgraph `InjectedState` /
  `InjectedToolCallId` and a `contextvars` runtime. Across FFI there is no ambient context —
  the adapter passes what the effect needs explicitly in each `Command`.

The one genuine constraint: **effects must be coarse-grained** — one `resume` per LLM turn
or per tool call, not per token. That is already the loop's natural granularity, so it costs
nothing here. Treat `RunFeedback` as a single opaque effect (Python runs the whole nested
judge agent) rather than recursing the state machine.

### 4.3 Tier 3 — the full async bridge (only if Rust must *drive*)

If Rust must genuinely `await` Python coroutines from inside deeply nested Rust `async`
code — e.g. it spawns its own concurrent Tokio tasks that each need to call Python
mid-flight — then and only then do you need `pyo3-async-runtimes` (Python awaitable ↔ Rust
`Future` via `into_future` / `future_into_py`), a pinned asyncio-loop ↔ Tokio-runtime
pairing, two-way cancellation translation, and panic-isolation so a Tokio task abort
doesn't kill the process. This roughly triples the effort and the test surface.

> **Recommendation.** Deliver LLM authoring loops at **Tier 2** — inversion of control.
> It gives extensions the full author→verify→revise loop with none of the bridge's cost,
> and it fits the codebase's existing decide/do seam exactly. Reach for Tier 3 only if a
> concrete backend must drive concurrent async Python from within Rust — a need none of the
> current backends have.

---

## 5. Hypothetical: the CVL prover backend in Rust

To make the tiers concrete, here is how the CVL backend
([formalization-abstraction.md §4](./formalization-abstraction.md)) *might* be structured
in Rust. This is illustrative, not a proposal to rewrite it.

### 5.1 What maps cleanly (Tier 1 candidates)

The CVL backend's heavy, self-contained steps are natural Rust:

- **`fetch_verdicts`** — resolves each spec's prover run and rolls per-rule outcomes into
  `Verdict`s ([formalization-abstraction.md §4.5](./formalization-abstraction.md)). Pure
  data-in/data-out over the prover output; already runs off-thread today. In Rust:

  ```rust
  #[derive(Deserialize)]
  struct ReportInput { name: String, final_link: Option<String>, /* ... */ }
  #[derive(Serialize)]
  struct RuleVerdict { rule: String, outcome: String, line: Option<u32>,
                       duration_seconds: Option<f64>, unit_file: Option<String> }

  #[pyfunction]
  fn fetch_verdicts(payload: String) -> PyResult<String> {
      let inp: ReportInput = serde_json::from_str(&payload)?;
      let verdicts: Vec<RuleVerdict> = query_prover_output(&inp)?;  // native HTTP/parse
      Ok(serde_json::to_string(&verdicts)?)
  }
  ```

  The adapter turns the returned list back into `dict[RuleName, Verdict]`.

- **`finalize`** — builds the `{spec → prover-run link}` map and writes
  `components_to_prover_runs.json` ([formalization-abstraction.md §4.6](./formalization-abstraction.md)).
  Trivial serde + file write.

- **The artifact bundle** — emitting the `.spec` + rendering the `.conf` (base config +
  fixed run overlay) is string/JSON assembly, a good fit for a Rust `ArtifactStore`
  helper, though the `ArtifactStore` object itself can stay Python and call a Rust
  formatter.

- **`GeneratedCVL` as `FormT`** stays a Python pydantic model
  ([cvl_generation.py](../composer/spec/cvl_generation.py)); Rust returns its fields as
  JSON and the adapter does `GeneratedCVL.model_validate(...)`. Its protocol methods
  (`property_units()`, `artifact_text`, `output_link`) remain Python one-liners so the
  cache/report keep working.

### 5.2 The authoring loop — Tier 2, inversion of control

`formalize` for CVL is the interesting case: it is *not* self-contained. `batch_cvl_generation`
([author.py](../composer/spec/source/author.py)) runs an **LLM agent graph to a fixpoint**,
interleaving:

- LLM authoring turns,
- the `verify_spec` prover tool (an async service call),
- the `property_feedback_judge` agent ([feedback.py](../composer/spec/feedback.py)),
- two hard validation gates (`PROVER_VALIDATION_KEY`, `FEEDBACK_VALIDATION_KEY`) before the
  agent may publish.

Under [§4.2](#42-tier-2--inversion-of-control-rust-decides-python-does-the-io-), Rust owns
this loop as a **synchronous decider** while Python performs each async effect. The Rust
side is a plain state machine over the same state the langgraph loop threads today:

```rust
// Rust: a pure step function. No async, no PyO3 awaitables — just decide the next effect.
fn resume(session: &mut Author, obs: Observation) -> Command {
    match obs {
        Observation::Start          => Command::CallLlm { messages: session.opening_prompt() },
        Observation::LlmReply(msg)  => session.interpret(msg),   // draft edit? verify? publish?
        Observation::ProverResult(r) => { session.record_prover(r); session.next() }
        Observation::FeedbackResult(f) => { session.record_feedback(f); session.next() }
        Observation::Cached(hit)    => session.after_cache(hit),
        Observation::Ack            => session.next(),
    }
}

// session.next() enforces the SAME publish gate check_completion does today:
fn next(&mut self) -> Command {
    if self.turns_left == 0 { return Command::GiveUp { reason: "turn budget exhausted".into() }; }
    match self.gate() {                                  // digest(spec, skipped) vs required keys
        Gate::NeedProver   => Command::RunProver { spec: self.spec(), config: self.conf(), rules: None },
        Gate::NeedFeedback => Command::RunFeedback { spec: self.spec(), skipped: self.skipped(),
                                                     rebuttals: self.rebuttals() },
        Gate::Ready(res)   => Command::Publish { result: res },   // both digests fresh ⇒ publish
        Gate::KeepAuthoring(reason) => Command::CallLlm { messages: self.reprompt(reason) },
    }
}
```

The Python adapter's driver loop ([§4.2](#42-tier-2--inversion-of-control-rust-decides-python-does-the-io-))
maps each `Command` onto the *existing* async services — `self._llm`, the real `verify_spec`
tool, `property_feedback_judge`, `ctx.cache_*` — so Rust reuses all of them without ever
awaiting. The validation gates stay in Rust because `check_completion` is already pure: a
content digest of `(spec, skipped)` that must match a stored digest per `required_validation`,
with any spec edit invalidating both. **No `pyo3-async-runtimes`, no Tokio, no bridge.**

### 5.3 `prepare_formalization` — orchestration stays in Python

CVL's `prepare_formalization` ([formalization-abstraction.md §4.2](./formalization-abstraction.md))
runs AutoSetup ∥ summaries ∥ structural-invariant formulation concurrently, then generates
`invariants.spec` once (with a cache short-circuit) and folds it into the resource set. This
is async orchestration of Python services — easiest left in the Python adapter. The
invariant *formulation logic* (once it has its inputs) and the invariant CVL authoring can
each be a Tier-1 helper or reuse the Tier-2 loop, but the `gather`/cache dance stays Python.

### 5.4 The pragmatic split

A realistic Rust CVL backend would be **hybrid**:

| Method | Tier | Where it lives |
| --- | --- | --- |
| `prepare_system` (harness lift, prover-tool build) | — | Python adapter |
| `prepare_formalization` (concurrency + cache orchestration) | — | Python adapter |
| `formalize` **decider** (prompt policy, gate check, publish/give-up) | 2 | **Rust** state machine |
| `formalize` **effects** (LLM, `verify_spec`, feedback judge, cache) | 2 | Python adapter loop |
| `fetch_verdicts` (prover-output parse → verdicts) | 1 | **Rust** |
| `finalize` (run-link map) | 1 | **Rust** |
| artifact formatting (`.spec`/`.conf`) | 1 | **Rust** helper, Python `ArtifactStore` shell |
| `GeneratedCVL` (`FormT`) | — | Python pydantic |

Every native-speed win (verdict parsing, artifact assembly, and now the authoring *policy*
itself) lands in Rust, and the LLM authoring loop works end-to-end — all without the Tier-3
async bridge.

---

## 6. Work breakdown

### Phase 0 — spike (Tier 1, throwaway)
- Stand up a maturin crate producing a `cp312` abi3 wheel; import it from `ai-composer`.
- Prove the round-trip: JSON payload → Rust → `serde` structs → JSON result →
  `pydantic.model_validate`.
- Confirm `py.allow_threads` + `asyncio.to_thread` gives real concurrency under the driver.

### Phase 1 — a self-contained Rust backend (Tier 1)
- Thin Python adapter implementing the async protocol; all callbacks stay in Python.
- Rust owns whichever methods are self-contained (e.g. a native `fetch_verdicts`/`finalize`,
  or a whole self-contained verifier).
- Marshalling helpers (`_marshal` / result validation); keep `FormT` pydantic.
- Wiring: `run_<rust>_pipeline`, CLI entry, widen `ReportBackend`.
- Tests: cache hit/miss round-trips, `GaveUp` path, exception → `ComponentOutcome`.

### Phase 2 — the LLM authoring loop via inversion of control (Tier 2)

This is the milestone that unlocks the capability extensions actually need. **No async
bridge.**

- Define the `Command` / `Observation` JSON enums (the effect vocabulary of one turn).
- Rust: the `new_session` / `resume` sync FFI pair — the pure decider, holding the
  loop state (`curr_spec`, `skipped`, `validations`, `rule_skips`, `config`, turn counter),
  with the `check_completion` digest gate reproduced in Rust.
- Python: the adapter driver loop mapping each `Command` onto the *existing* async services
  (`self._llm`, `verify_spec`, `property_feedback_judge`, `ctx.cache_*`).
- Decide the Rust-author ergonomics: ship the explicit-state-machine API first; add the
  self-driven-coroutine effect runtime if authors want linear code.
- Tests: a full author→verify→feedback→publish trace; gate staleness on spec edit;
  turn-budget give-up; `CancelledError` unwinds cleanly.

### Phase 3 — full async bridge (Tier 3, only if a backend must *drive*)
- Adopt `pyo3-async-runtimes`; pin the asyncio-loop/Tokio-runtime pairing.
- Two-way cancellation translation; panic isolation so a Tokio abort ≠ process abort.
- Verify structured-concurrency parity (per-component isolation under
  `gather(return_exceptions=True)`).

### Cross-cutting

- Build/CI: cross-platform abi3 wheels, `uv` source wiring, reproducible Rust toolchain.
- Docs: fold the final `Command`/`Observation` ABI and the marshalling schemas into
  [formalization-abstraction.md §9](./formalization-abstraction.md)'s "new backend" checklist.

---

## 7. Open questions

1. **Do we ever need Tier 3?** Tier 2 (inversion of control) delivers the LLM authoring
   loop with no bridge. Tier 3 only pays off if a concrete backend must drive concurrent
   async Python from within Rust — decide per backend, not up front.
2. **Rust-author ergonomics for Tier 2** — ship the explicit `resume` state machine, or
   invest in the self-driven-coroutine effect runtime so authors write linear `async` Rust?
   Start with the former; graduate to the latter only if the loops get complex enough to
   warrant it.
3. **Wheel vs mixed build** — does any consumer need `ai-composer` itself to remain a pure
   sdist-installable package? If so, the separate-wheel path is mandatory.
4. **ABI stability** — the `Command`/`Observation` enums and the marshalling JSON schemas
   become a versioned contract between the wheel and `ai-composer`. Where does that schema
   live, and how is it version-checked at load time?
5. **Effect granularity** — the IoC loop assumes one `resume` per turn/tool-call. Is any
   effect an extension might want finer-grained than that (e.g. streaming LLM tokens)? If so
   it must still be batched to a turn boundary, or it forces Tier 3.
6. **Observability** — the driver's `run.runner(TaskInfo(...))` wrapping gives per-task
   telemetry/UI rows. A Tier-1 Rust method invoked via `asyncio.to_thread` still sits inside
   one `runner` task (good); a Tier-2 loop's per-turn effects should each be wrapped by the
   Python adapter in `run.runner(...)` so they stay visible in the TUI.

---

## 8. Key files

| Concern | File |
|---|---|
| Driver + abstraction definitions | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| Result protocols (`FormalResult`, `ArtifactIdentifier`) | [composer/spec/types.py](../composer/spec/types.py) |
| `ReportableResult`, `Verdict`, `collect` | [composer/spec/source/report/collect.py](../composer/spec/source/report/collect.py) |
| CVL backend (reference for the sketch) | [composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py) |
| CVL authoring loop + completion tools (`batch_cvl_generation`, `PublishResultTool`) | [composer/spec/source/author.py](../composer/spec/source/author.py) |
| `PureFunctionGenerator` decide/do seam + graph topology (the IoC precedent) | [graphcore/graphcore/graph.py](../graphcore/graphcore/graph.py) |
| `check_completion` validation-gate predicate + loop state (`FormT`, gate digest) | [composer/spec/cvl_generation.py](../composer/spec/cvl_generation.py) |
| Foundry backend (simplest backend to copy) | [composer/foundry/pipeline.py](../composer/foundry/pipeline.py) |
| `ReportBackend` literal to widen | [composer/spec/source/report/collect.py](../composer/spec/source/report/collect.py) |
| Packaging | [pyproject.toml](../pyproject.toml) |
| The seam this builds on | [formalization-abstraction.md](./formalization-abstraction.md) |
