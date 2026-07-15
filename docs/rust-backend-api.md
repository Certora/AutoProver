# A service-shaped backend API (no IoC loop, no async runtime in the wheel)

Design note. A concrete alternative to the IoC decider loop
([rust-ioc-loop.md](./rust-ioc-loop.md)): recast the Rust backend as a **passive service**
that the **Python core pipeline** drives through the whole author→compile→judge→validate
loop. The backend answers pure questions (prompts, RAG) and owns the two "run the local
toolchain" steps (`compile`, `validate`) — which invoke `cargo`/`crucible` **directly**, each
through the `run-confined` sandbox launcher. No `RustSession`/`resume`, no
`Command`/`Observation` protocol, and no async runtime in the wheel.

## 1. The reframing

The backend's real job in formalization is small:

- **know what to say to the model** — the authoring prompt and the optional judging prompt;
- **compile a candidate spec** — build it via the toolchain and report failures verbatim so
  the model can fix them;
- **validate the spec** — run the checker (Crucible) and turn its output into verdicts.

Everything else — running the LLM agent, retrying, streaming events, caching, and building the
RAG search tools — is generic orchestration the pipeline already does for the CVL/Foundry
backends. So the backend stops being a *driver* (the `resume` state machine) and becomes a
*service* the driver consults. (RAG is unchanged: the author's knowledge-base search tools are
still built the way they are today — e.g. Crucible's external `crucible_kb` corpus, populated
by `scripts/populate_crucible_rag.sh` — not supplied through this API.)

The two slow steps are different from the pure ones: `compile` and `validate` actually run
untrusted native tools, so they **execute inside the sandbox**. The backend does that itself
by spawning [`run-confined`](../rust/run-confined/src/main.rs) — the standalone launcher that
applies Landlock + seccomp + rlimits + an env allowlist to itself and then `execve`s the tool
(fail-closed). The backend authors the command line; **Python authors the confinement policy**
(which paths are writable/readable, the private `CARGO_HOME`, caps) and hands it in, because
that is environment/recipe knowledge that lives in `composer/sandbox`
([command-sandbox.md](./command-sandbox.md)).

## 2. The backend API

Two tiers:

- **Pure** callouts (`json → json`, no I/O): metadata, `units`, prompts. Same shape as today's
  `descriptor`/`fetch_verdicts`.
- **Blocking** callouts: `compile`/`validate` spawn `run-confined` and wait. They are ordinary
  synchronous functions that **release the GIL** while the child runs, so Python calls them
  with `await asyncio.to_thread(...)` — non-blocking to the event loop, **no tokio, no
  `pyo3-async`** (see §5). They are kept **clearly separate** (compile the whole spec once;
  validate one unit at a time) — no fusing the dry-run into the first fuzz.

```rust
pub trait Backend: Send + Sync + 'static {
    // ---- metadata (unchanged, pure) --------------------------------------
    fn descriptor(&self) -> AppDescriptor;
    fn validate_preconditions(&self, args: &Value) -> Result<(), String> { Ok(()) }

    // ---- authoring (pure) ------------------------------------------------
    /// The units this input formalizes — one per property — each a property title and its
    /// backend-specific unit name (Crucible: `c_<slug>`, the test-fn / feature selector).
    /// Pure and *pre-authoring*: the author prompt requires exactly these fn names, the
    /// host validates each unit, and it is the report's property→unit map. `kind="setup"`
    /// (the fixture) has no units.
    fn units(&self, input: &AuthorInput) -> Vec<Unit>;          // {property, unit}
    /// Instruction (+ optional system prompt) to author `input.kind`'s spec — covering all
    /// its units. `failure = Some(..)` on a re-author after a compile failure or a judge
    /// rejection, so one function covers the initial draft and every revision.
    fn author_prompt(&self, input: &AuthorInput, failure: Option<&Failure>) -> Prompt;
    /// Optional LLM review of a compiled spec, before validation. `None` (the default —
    /// what every backend returns today) skips judging; `Some(prompt)` runs a judge turn
    /// whose structured verdict (accept / reject + feedback) the host feeds back as a
    /// `Failure` on reject. Present in the API so a backend can opt in without a reshape.
    fn judge_prompt(&self, input: &AuthorInput, spec: &str) -> Option<Prompt> { None }

    // ---- gating: run the toolchain directly, inside run-confined ---------
    /// Compile/typecheck the whole spec ONCE (every unit shares one build): materialize it
    /// into `workdir`, build under `sandbox`, and report success or the errors to hand back
    /// to the model. BLOCKING — releases the GIL while `run-confined` runs.
    fn compile(&self, input: &AuthorInput, spec: &str, workdir: &Path, sandbox: &Sandbox)
        -> CompileResult;                                       // Ok | Failed { errors }

    /// Validate ONE unit against the (already-compiled) spec and bake its verdict — run the
    /// checker for that unit only. Per-unit so the host owns enumeration and scheduling
    /// (and can fan out); the backend never discovers units here. BLOCKING (GIL released).
    fn validate(&self, input: &AuthorInput, spec: &str, unit: &str,
                workdir: &Path, sandbox: &Sandbox) -> Verdict;  // GOOD/BAD/…

    // ---- assembly (pure) -------------------------------------------------
    fn finalize(&self, outcomes: &Value) -> BTreeMap<String, String> { BTreeMap::new() }
}
```

Supporting types:

```rust
struct AuthorInput { kind: String, program: String, component: Value, props: Vec<Property>, context: Value }
struct Prompt      { system: Option<String>, instruction: String }
struct Failure     { errors: String }                 // compile stderr or judge feedback, fed back to the model
enum   CompileResult { Ok, Failed { errors: String } }
struct Unit        { property: String, unit: String } // report row + fuzz target (e.g. c_<slug>)

/// The confinement policy, authored by Python (never the LLM). The backend maps it to a
/// `run-confined` argv and prepends the launcher; `None` = the trusted/`none` path (exec
/// directly, no launcher). See composer/sandbox/policy.py::SandboxPolicy.
struct Sandbox {
    run_confined: Option<PathBuf>,       // launcher path; None → run unconfined (trusted)
    rw: Vec<PathBuf>, ro: Vec<PathBuf>,  // Landlock grants (workdir, toolchains, checkout)
    allow_env: Vec<String>,              // env allowlist ("NAME" or "NAME=VAL")
    network: bool,
    rlimits: Rlimits,                    // as / cpu / nproc / fsize
    timeout_s: u64,
}
```

`kind` lets one backend author more than one thing with the same primitives — Crucible's
program-wide **fixture** (`kind="setup"`) and each unit's **tests** (`kind="component"`) are
both "author a spec, then `compile` it"; only the component kind has a `validate` step, and
the fixture's compiled output feeds the components' `context`.

### How `compile`/`validate` reach the sandbox — a shared SDK helper

Both `compile` and `validate` run their tool the same way, so `autoprover-sdk` provides **one
helper** every backend calls — the launcher contract lives in exactly one place:

```rust
// in autoprover-sdk
pub fn run_confined(
    sandbox: &Sandbox, program: &str, args: &[String],
    files: &BTreeMap<String, String>, workdir: &Path, timeout: Duration,
) -> io::Result<CommandOutput>;   // { exit_code, stdout, stderr }
```

It (1) materializes `files` into `workdir`, path-confining each write (reject absolute / `..`,
as `composer.sandbox.command._confined_target` does); (2) builds the argv
`[run_confined, <policy flags…>, "--", program, *args]` — or `[program, *args]` when
`sandbox.run_confined` is `None` (the trusted/`none` path); (3) spawns it (std `Command`),
waits with `timeout`, and captures the streams. The resulting launch is exactly what the
Python `launcher` provider builds today —

```text
run-confined --rw <workdir> --rw <.sandbox_cargo> --ro <toolchain> --ro <crucible>
             --allow-env PATH --allow-env CARGO_HOME=… [--allow-network]
             --rlimit-as … -- crucible run <program> <feature> --release --mode explore --timeout N
```

— just assembled in the SDK next to the backend that owns the command. So `compile` builds the
`{program, args, files}` for a dry-run and calls `run_confined`; `validate` does the same for
one unit's fuzz run; neither re-implements sandbox plumbing. The **command after `--` is
backend-authored**; the **policy flags before it are Python-authored** (`Sandbox`); Landlock
grants only the `--rw` workdir, so even a bad file path can't escape.

## 3. The Python side

The formalizer is one generic loop in the core pipeline:

```python
async def formalize(mod, input, env, sandbox, *, workdir, max_attempts, emit) -> Formalized | GaveUp:
    units = mod.units(input)                                                # ← backend (pure): the fuzz targets
    spec, failure = None, None
    for _ in range(max_attempts):                                           # author → compile → judge (retry)
        prompt = mod.author_prompt(input, failure)                          # ← backend (pure)
        spec = await run_llm_agent(env, prompt, tools=env.rag_tools + env.source_tools)  # ← Python: LLM
        r = await asyncio.to_thread(mod.compile, input, spec, workdir, sandbox)  # ← backend runs run-confined
        if r.failed:
            failure = Failure(r.errors); emit("build", r.errors); continue
        if (jp := mod.judge_prompt(input, spec)) is not None:               # ← backend (pure); default None → skip
            review = await run_llm_agent(env, jp, structured=JUDGE)         # ← Python: LLM
            if not review.accept:
                failure = Failure(review.feedback); continue
        break
    else:
        return GaveUp(f"did not pass compile/judge in {max_attempts} attempts")

    # validate each unit — host owns enumeration + scheduling (serial today; see §5).
    async def check(u):
        v = await asyncio.to_thread(mod.validate, input, spec, u.unit, workdir, sandbox)
        emit("verdict", {"unit": u.unit, **v})                              # live per-unit notice
        return u, v
    results = [await check(u) for u in units]                               # or asyncio.gather to fan out
    return Formalized(artifact_text=spec,
                      property_units=[(u.property, [u.unit]) for u, _ in results],
                      verdicts={u.unit: v for u, v in results})
```

- The **author system prompt is already backend-definable** (`_llm_agent._split_prompt`), so
  `Prompt.system` drops in.
- **RAG is unchanged**: the host builds the author's knowledge-base search tools as it does
  today (Crucible's external `crucible_kb`, populated by `scripts/populate_crucible_rag.sh`)
  and passes them in `env.rag_tools`. Moving the corpus into the wheel is a possible later
  step, not part of this change.
- **Sandbox policy** is built once by Python (`SandboxConfig.build_policy(workdir)`, the
  existing recipe) and passed straight through as `Sandbox` — Python keeps ownership of the
  *intent*; the backend only assembles it into a `run-confined` argv.
- **Events + caching are Python's** (it knows the phase and each result), so the `Emit`
  command and the `Emitter` shim disappear and the pipeline's result cache subsumes the loop's
  scratch cache.
- `fetch_verdicts` disappears for self-contained backends — verdicts come from `validate`.

`CrucibleFormalizer.formalize` becomes: prepare the crate → run the `setup` artifact (author +
`compile`, no validate) to get the fixture → run the `component` artifact through the loop
above. Two readable `await`s over backend callouts; no state machine.

## 4. How Crucible implements it

- `units(input)` → one `Unit{ property: title, unit: "c_<slug>" }` per invariant (the current
  `_unique_slugs` mapping, moved into the backend). `kind="setup"` ⇒ `[]`.
- `author_prompt(input, failure)` → `kind="setup"` ⇒ the fixture prompt; `kind="component"` ⇒
  the all-invariants prompt (listing the `units`' fn names); `failure` ⇒ append revise context,
  dispatched on `failure.kind`: a `Compile` failure appends the prior draft + compiler errors
  (`revise_suffix`), a `Judge` rejection appends the prior draft + review feedback framed as
  *not* a build error (`judge_revise_suffix`).
- `judge_prompt` → overridden for `kind="component"` (skipped for the fixture): a reviewer turn
  modeled on Foundry's feedback judge, retargeted to fuzzing — the load-bearing question is
  reachability (can the fuzzer drive a state where the invariant could fail?). Emits the
  `{accept, feedback}` JSON the host's `_parse_judge` reads; a rejection re-authors the suite
  with the feedback (as a `Judge`-kind `Failure`).
- `compile(input, spec, workdir, sandbox)` → `run_confined(sandbox, "crucible", ["run", program,
  probe, "--release", "--dry-run"], files={"fuzz/<program>/src/main.rs": fixture+spec}, workdir)`;
  `Failed{errors: tail}` if `is_build_error(out)` or nonzero exit, else `Ok`.
- `validate(input, spec, unit, workdir, sandbox)` → `run_confined(sandbox, "crucible", ["run",
  program, unit, "--release", "--mode", "explore", "--timeout", n], files={main.rs: fixture+spec}, workdir)`;
  `BAD` if stdout contains `[FUZZ_FINDING]`, else `GOOD`. One unit per call.
- `finalize` → unchanged.

Every piece is the body of a current `resume` arm turned into a pure/blocking function —
directly unit-testable in Rust (feed a spec + a fake `crucible` on `PATH`, assert the command
or the verdict).

## 5. Why there is still no async runtime in the wheel

`compile`/`validate` block on a child process. To keep them off the event loop **without**
tokio or a Python-await bridge:

- the `#[pyfunction]` wraps its subprocess work in `Python::allow_threads(|| …)`, releasing the
  GIL for the (minutes-long) build/fuzz;
- Python calls it with `await asyncio.to_thread(mod.compile, …)`.

So the wheel stays **synchronous** — no tokio, no `future_into_py`/`into_future`, no
GIL-across-await marshaling, no contextvar-through-a-bridge risk (the earlier
async-into-Rust concern in [rust-ioc-loop.md]). The backend just spawns and waits; Python
just moves the wait to a thread. This is the whole reason to spawn `run-confined` *directly*
rather than await a Python runner: the sandbox is already a standalone binary, so the backend
needs nothing from Python at run time except the policy data.

Because `validate` is **per-unit**, the host *can* fan the units out (`asyncio.gather` of
`to_thread` calls) for free at the Python layer — but actually running Crucible fuzz builds
concurrently against the **shared crate** hits the binary-name collision from
[crucible-unit-granularity.md §7](./crucible-unit-granularity.md) (every feature builds the
same `invariant_test`), so real parallelism still needs the `--binary-in` build/fuzz split.
The per-unit signature is the right shape regardless (the host owns enumeration/scheduling);
it runs **serial today** and becomes parallel when §7 is done — no API change either way.

### Scope: self-contained vs run-service backends

This shape fits a backend whose checker is a **local tool** (Crucible: `cargo`/`crucible`;
a future soroban twin). A backend whose "validate" is a **remote/Python service** (the Certora
prover) can't spawn it under `run-confined`; that path would keep a Python-side effect for the
service call (or the real prover stays the Python `ProverBackend` it already is, not a Rust
wheel). The echoprover demo's prover step is the one place this matters — it either keeps a
thin Python `run_prover` hook or drops the prover step; Crucible, the actual target, is fully
self-contained.

## 6. What changes, concretely

**Delete** (SDK + host): `Command`/`Observation`, `FormalizeSession`/`resume`, the
`RustSession` pyclass, `drive_session`, the `Emitter` shim, the loop's scratch cache, and the
`RealEffects` `run_command` routing (the backend now spawns `run-confined` itself).
**Keep**: `descriptor`/`validate_preconditions`/`finalize`, the sandbox *policy* layer
(`SandboxPolicy`/`SandboxConfig.build_policy`), `run-confined` itself, the existing RAG
mechanism (`crucible_kb` + `populate_crucible_rag.sh`, surfaced as `env.rag_tools`), `_llm_agent`
(now called directly by the pipeline), and `run_prover`/`run_feedback` only for the run-service
exception.
**Add**: the `Backend` callouts above (pure `descriptor`/`validate_preconditions`/`units`/
`author_prompt`/`judge_prompt`/`finalize` + the two GIL-releasing blocking `compile`/`validate`), the shared
`autoprover_sdk::run_confined` helper (the `SandboxPolicy` → `run-confined` argv assembly moves
here, out of the Python `launcher` provider), and one generic `formalize` in the core pipeline.

Net: the FFI goes from "a coroutine hand-compiled into a state machine + an 8-variant effect
protocol" to "pure prompt/unit callouts + `compile`/`validate` that run the toolchain via one
shared launcher helper." The control flow lives in one Python function that reads like the
procedure it is.

## 7. Security invariant (unchanged, and clearer)

The rule ([command-sandbox.md](./command-sandbox.md) §2/§7): the **command line** (`program` +
`args`) is authored by trusted compiled code; only file **contents** may derive from the LLM.
Here `compile`/`validate` construct `program`/`args` in Rust and place them after `--`; the
`Sandbox` policy (the `run-confined` flags before `--`) is authored by Python. The LLM's spec
is written into the `--rw` workdir and confined by Landlock. Two audit points, both trivial:
the pure command-building in the backend, and `SandboxConfig.build_policy` in Python. A test
asserts the launched argv is `[run-confined, …policy…, "--", "crucible"/"cargo", …]` and never
contains LLM text.

## 8. Decisions and deferrals

Resolved:

- **Shared launcher helper — yes.** `autoprover_sdk::run_confined` owns the `Sandbox` →
  `run-confined` argv assembly; every backend calls it (§2).
- **`compile` and `validate` stay separate.** No fusing the dry-run into the first fuzz — one
  whole-spec compile, then per-unit validation (§2).
- **`validate` is per-unit.** The host enumerates units (`units(input)`) and calls `validate`
  once per unit, so the backend never discovers units and the host owns scheduling; serial
  today, parallel once the §7 build-collision split lands (§5).
- **Judge — API present, default no-op.** `judge_prompt` stays in the trait but defaults to
  `None` (skip), so the loop has a judge step wired in and a backend can opt in later without
  a reshape. No backend overrides it today.
- **Wheel-owned RAG — deferred.** RAG stays the external `crucible_kb` mechanism; a
  `knowledge_base()` callout the host indexes is an additive later step, out of scope here.

Still open:

- Timeout mechanics in the SDK helper (std `Command` has no built-in timeout — a wait-thread
  vs a small `wait-timeout`-style dependency).
- Where `units`' slug-uniqueness lives if two backends want to share it (SDK helper vs
  per-backend), and whether `author_prompt` should take the `units` list explicitly rather
  than re-deriving it internally.
