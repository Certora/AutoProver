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
> assumes its vocabulary (`FormT`, `Formalizer`, `PreparedSystem`, the phase chain).

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
Python for free — see [§4](#4-the-async-problem-two-tiers).

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

## 4. The async problem, two tiers

Everything above assumes the Rust backend is **self-contained**: given its inputs, it
produces a result without calling back into Python. That is Tier 1, and it is easy.
Tier 2 — Rust that must `await` Python services mid-computation — is where the real
additional work lives.

### 4.1 Tier 1 — self-contained Rust (`asyncio.to_thread`)

If the Rust `formalize` does its own verification (spawns a solver, shells out to a
tool, computes an artifact) and only needs its inputs, the adapter wraps a **synchronous**
Rust call in a thread:

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
infrastructure. **Prefer designing the Rust backend to fit here.**

### 4.2 Tier 2 — Rust that calls back into async Python

The moment the Rust backend wants to *reuse* the Python machinery mid-run — the LLM
authoring loop, `run.runner(...)` for task tracking/telemetry, `ctx.cache_get/put`, the
`verify_spec` prover tool, the `property_feedback_judge` agent — it must `await` Python
coroutines from inside Rust. That is a genuine async FFI bridge, and it requires:

**A. An async runtime bridge — `pyo3-async-runtimes`.**
This crate converts a Python awaitable into a Rust `Future` (`into_future`) and a Rust
`Future` into a Python awaitable (`future_into_py`). The Rust `formalize` becomes an
`async fn` exposed to Python as a coroutine:

```rust
#[pyfunction]
fn formalize<'py>(py: Python<'py>, ctx: PyObject, payload: String) -> PyResult<Bound<'py, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        // ... Rust logic that awaits Python callbacks ...
    })
}
```

**B. A single, agreed event-loop/runtime pairing.** Python drives one asyncio loop; Rust
drives a Tokio runtime. `pyo3-async-runtimes` must be initialized to marry the two
(`tokio::init` + the current running loop). Every `await` of a Python callback re-acquires
the GIL, so callbacks must be *coarse-grained* — one `await` per LLM turn, not per token.

**C. A callback ABI — the "host services" trait.** Rather than let Rust reach into
arbitrary Python objects, expose a **small, explicit Python shim** of just the services
Rust needs, and hand it across as a `PyObject`. For example:

```python
class RustHostServices:                 # constructed by the adapter, passed into Rust
    async def run_task(self, task_json: str) -> str: ...     # wraps run.runner(...)
    async def cache_get(self, key: str) -> str | None: ...   # wraps ctx.cache_get
    async def cache_put(self, key: str, val: str) -> None: ...
    async def llm_author(self, prompt_json: str) -> str: ...  # wraps batch_cvl_generation
    async def run_prover(self, conf_json: str) -> str: ...    # wraps the verify_spec tool
```

Rust holds this `PyObject`, calls a method, gets back a Python awaitable, and
`into_future().await`s it. The ABI is **JSON strings in, JSON strings out** — no pydantic
or deep graphs cross the FFI line even for callbacks.

**D. Cancellation and error semantics.** asyncio task cancellation must translate into
Tokio cancellation (drop the future) and vice versa; a Python exception raised inside a
callback surfaces as a Rust `PyErr` that the backend must either handle or propagate back
out as a `PyErr` on the outer coroutine. This is the fiddliest part and needs explicit
tests.

**E. Structured-concurrency parity.** The driver relies on
`asyncio.gather(..., return_exceptions=True)` treating a per-component failure as an
isolated `ComponentOutcome`. A Tier-2 Rust backend spawning its own concurrent work must
not let a Tokio task panic escape as a process abort; wrap task bodies so failures become
`PyErr`, preserving the driver's isolation guarantee.

> **Recommendation.** Treat Tier 2 as a separate, later milestone. It roughly triples the
> effort and the test surface. Most of the value (native-speed verification, fast artifact
> transforms) is reachable at Tier 1 by keeping the Rust backend self-contained and doing
> the LLM/tooling orchestration in the Python adapter. Adopt Tier 2 only when the Rust
> side genuinely needs to *drive* the async services rather than be driven by them.

---

## 5. Hypothetical: the CVL prover backend in Rust

To make the two tiers concrete, here is how the CVL backend
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

### 5.2 What forces Tier 2

`formalize` for CVL is *not* self-contained. `batch_cvl_generation`
([author.py](../composer/spec/source/author.py)) runs an **LLM agent graph to a fixpoint**,
interleaving:

- LLM authoring turns,
- the `verify_spec` prover tool (an async service call),
- the `property_feedback_judge` agent ([feedback.py](../composer/spec/feedback.py)),
- two hard validation gates (`PROVER_VALIDATION_KEY`, `FEEDBACK_VALIDATION_KEY`) before the
  agent may publish.

A Rust `formalize` that *owned* this loop would need to `await` all of those Python
services mid-computation — squarely Tier 2, needing the `RustHostServices` shim and the
`pyo3-async-runtimes` bridge from [§4.2](#42-tier-2--rust-that-calls-back-into-async-python).
Sketch:

```rust
async fn formalize(host: HostServices, batch: Batch) -> Result<GaveUpOr<Cvl>, PyErr> {
    let mut state = AuthorState::new(&batch);
    loop {
        let draft   = host.llm_author(&state.prompt()).await?;   // Python LLM turn
        let prover  = host.run_prover(&draft.conf).await?;       // verify_spec tool
        let feedback = host.judge_feedback(&draft, &prover).await?;
        state.record(prover, feedback);
        match state.gate() {
            Gate::Publish(cvl) => return Ok(GaveUpOr::Value(cvl)),
            Gate::GiveUp(reason) => return Ok(GaveUpOr::GaveUp(reason)),
            Gate::Continue => {}                                  // loop again
        }
    }
}
```

### 5.3 `prepare_formalization` — mixed

CVL's `prepare_formalization` ([formalization-abstraction.md §4.2](./formalization-abstraction.md))
runs AutoSetup ∥ summaries ∥ structural-invariant formulation concurrently, then generates
`invariants.spec` once (with a cache short-circuit) and folds it into the resource set. The
concurrency and cache calls are async Python service calls → Tier 2. The *invariant
formulation logic* itself could be a Tier-1 Rust helper, but the orchestration is easiest
left in the Python adapter.

### 5.4 The pragmatic split

A realistic first cut of a Rust CVL backend would be **hybrid**:

| Method | Where it lives |
|---|---|
| `prepare_system` (harness lift, prover-tool build) | Python adapter |
| `prepare_formalization` (orchestration) | Python adapter; invariant formulation → Rust helper |
| `formalize` (LLM authoring loop) | Python adapter **until** Tier 2 lands; then Rust owns the loop |
| `fetch_verdicts` | **Rust** (Tier 1) |
| `finalize` | **Rust** (Tier 1) |
| artifact formatting (`.spec`/`.conf`) | **Rust** helper, Python `ArtifactStore` shell |
| `GeneratedCVL` (`FormT`) | Python pydantic |

That captures the native-speed wins (verdict parsing, artifact assembly, invariant
computation) without paying for the async bridge, and leaves the LLM-orchestration loop —
the part that fundamentally *is* a dance with async Python services — for a deliberate
Tier-2 milestone.

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

### Phase 2 — async callback bridge (Tier 2, only if needed)
- Adopt `pyo3-async-runtimes`; pin the asyncio-loop/Tokio-runtime pairing.
- Define and stabilize the `RustHostServices` JSON ABI (task-run, cache, LLM, prover).
- Implement cancellation + error propagation both directions; test cancellation storms.
- Verify structured-concurrency parity (per-component isolation under
  `gather(return_exceptions=True)`).

### Cross-cutting
- Build/CI: cross-platform abi3 wheels, `uv` source wiring, reproducible Rust toolchain.
- Docs: fold the final ABI into [formalization-abstraction.md §9](./formalization-abstraction.md)'s
  "new backend" checklist.

---

## 7. Open questions

1. **Do we actually need Tier 2?** If the target Rust backends are self-contained
   verifiers, we may never pay for the async bridge. This should be decided per concrete
   backend, not up front.
2. **Wheel vs mixed build** — does any consumer need `ai-composer` itself to remain a pure
   sdist-installable package? If so, the separate-wheel path is mandatory.
3. **ABI stability** — the `RustHostServices` / marshalling JSON schemas become a
   versioned contract between the wheel and `ai-composer`. Where does that schema live, and
   how is it version-checked at load time?
4. **Observability** — the driver's `run.runner(TaskInfo(...))` wrapping is what gives
   per-task telemetry/UI rows. A Tier-1 Rust method invoked via `asyncio.to_thread` still
   sits inside one `runner` task (good); a Tier-2 backend spawning its own work needs to
   route sub-tasks back through `run_task` to stay visible in the TUI.

---

## 8. Key files

| Concern | File |
|---|---|
| Driver + abstraction definitions | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| Result protocols (`FormalResult`, `ArtifactIdentifier`) | [composer/spec/types.py](../composer/spec/types.py) |
| `ReportableResult`, `Verdict`, `collect` | [composer/spec/source/report/collect.py](../composer/spec/source/report/collect.py) |
| CVL backend (reference for the sketch) | [composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py) |
| CVL authoring loop (Tier-2 driver) | [composer/spec/source/author.py](../composer/spec/source/author.py) |
| CVL result type (`FormT`) | [composer/spec/cvl_generation.py](../composer/spec/cvl_generation.py) |
| Foundry backend (simplest backend to copy) | [composer/foundry/pipeline.py](../composer/foundry/pipeline.py) |
| `ReportBackend` literal to widen | [composer/spec/source/report/collect.py](../composer/spec/source/report/collect.py) |
| Packaging | [pyproject.toml](../pyproject.toml) |
| The seam this builds on | [formalization-abstraction.md](./formalization-abstraction.md) |
