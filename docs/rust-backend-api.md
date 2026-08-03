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
still built the way they are today — e.g. Crucible's external `crucible_kb` corpus, imported
from a committed manifest via `composer.scripts.rag_import` — not supplied through this API.)

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
struct AuthorInput { kind: String, program: String, program_crate: ProgramCrate,
                     component: Value, props: Vec<Property>, context: Value }

/// Where the analyzed code lives as a compilation unit, for a wheel that must *depend* on it.
/// `program` above is only the analysis identifier (the `Name` in `path:Name`) — it is NOT a Cargo
/// name, and a crate's directory / package / lib names are independent of it and of each other
/// (lend: `programs/lend`, package `example_lending`). The host resolves this from the main
/// source file's manifest; read it via `resolved(program)`, which fills any part the host left
/// empty from the legacy `programs/<program>` convention. `anchor` is the crate's declared
/// `anchor-lang` requirement — a wheel can only link the crate when it matches its own Anchor
/// major, so this is what routes Crucible to its IDL path (crucible-application.md §6.2).
struct ProgramCrate { dir: String, package: String, lib: String, anchor: String }
struct Prompt      { system: Option<String>, instruction: String }
struct Failure     { errors: String }                 // compile stderr or judge feedback, fed back to the model
enum   CompileResult { Ok, Failed { errors: String } }
struct Unit        { property: String, unit: String } // report row + fuzz target (e.g. c_<slug>)

/// The confinement wrapper, authored by Python (never the LLM). Python owns the confinement
/// *intent* and lowers it to `argv_prefix` — an opaque argv the backend prepends to its command,
/// naming no sandbox mechanism. Empty prefix = the trusted/`none` path (exec directly). See
/// composer/sandbox/config.py::SandboxConfig.backend_spec.
struct Sandbox {
    argv_prefix: Vec<String>,  // confinement wrapper up to `--`; empty → run directly (trusted)
    timeout_s: u64,
}
```

`kind` lets one backend author more than one thing with the same primitives — Crucible's
program-wide **fixture** (`kind="setup"`) and each unit's **tests** (`kind="component"`) are
both "author a spec, then `compile` it"; only the component kind has a `validate` step, and
the fixture's compiled output feeds the components' `context`. A third kind, `kind="preflight"`,
reuses `compile` alone with an empty `spec` to gate the *prepared workspace* before anything has
been authored — see §3.1.

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
`[*sandbox.argv_prefix, program, *args]` — which collapses to `[program, *args]` when
`argv_prefix` is empty (the trusted/`none` path); (3) spawns it (std `Command`),
waits with `timeout`, and captures the streams. The prefix is authored wholesale by the Python
`launcher` provider (`argv_prefix`), so the launch is exactly —

```text
run-confined --rw <workdir> --rw <.sandbox_cargo> --ro <toolchain> --ro <crucible>
             --allow-env PATH --allow-env CARGO_HOME=… [--allow-network]
             --rlimit-as … -- crucible run <program> <feature> --release --mode explore --timeout N
```

— where everything up to and including `--` is Python's `argv_prefix` (opaque to the SDK, which
just prepends it), and the backend appends only the command. So `compile` builds the
`{program, args, files}` for a dry-run and calls `run_confined`; `validate` does the same for
one unit's fuzz run; neither re-implements sandbox plumbing. The **command after `--` is
backend-authored**; the **prefix before it is Python-authored** (`Sandbox.argv_prefix`); Landlock
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

- The **author system prompt is backend-definable**: the host parses this payload as
  `composer.rustapp.wire.Prompt`, and a `system` of `None` means "use the host's neutral default".
- **RAG is unchanged**: the host builds the author's knowledge-base search tools as it does
  today (Crucible's external `crucible_kb`, imported from the committed
  `rust/crucible-app/crucible_kb.rag.json` via `composer.scripts.rag_import`) and passes them in
  `env.rag_tools`. Moving the corpus into the wheel is a possible later step, not part of this change.
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

### 3.1 The preflight — gating the workspace before the model runs

`compile` is called with three `kind`s, not two. The third, `kind="preflight"`, is a **gate on the
prepared workspace** that runs *concurrently with system analysis* — before a single property
exists — and it is the only one where the host sends an **empty `spec`**: nothing has been authored
yet, so the wheel renders its own minimal skeleton (Crucible: `skeleton_fixture.j2` + the existing
`c_probe` test).

The driver overlaps it, so a backend that builds gets its build for free:

```text
                    ┌── backend.preflight ──────────────────┐   run_component_analysis
                    │  workspace_prep: files, cargo fetch,  │   (one agent)
                    │    build the program, place the IDL   │         │
                    │  compile(kind="preflight", spec="")   │   _extract_all
                    └───────────────┬───────────────────────┘   (N agents)
                                    └─── failure ⇒ both cancelled ┘
```

Why the gate and not just the prep: `cargo fetch` resolves a dependency graph but **compiles
nothing**, and its failure is deliberately non-fatal (`warm_cargo_cache` logs and returns, on the
theory that the offline build will surface it). So before this existed, the first thing that actually
built the harness crate was the compile of the first LLM-authored draft — at the far end of the
extraction phase. Everything that can go wrong there is invisible to an authoring agent's revise
loop, because the agent does not own the manifest:

| Failure | Where it used to appear |
|---|---|
| Dependency graph won't co-resolve (e.g. `ahash` under Solana 1.17 vs `libafl 0.15`) | compiler errors in draft #1 |
| Harness crate won't link (program crate on another Anchor major) | compiler errors in draft #1 |
| `declare_fuzz_program!` rejects the IDL (unsupported type, missing `metadata.address`) | compiler errors in draft #1 |
| The `.so` isn't where the fixture is told it is, or won't load into LiteSVM | a mystery panic in `setup()` |

So a preflight failure is **terminal** — the host raises `PreflightFailed` with the wheel's
extracted diagnostics; there is no re-author — and the driver's `_all_or_none` cancels the analysis
racing it. Two side benefits: the gate's `--dry-run` proves the built `.so` loads and `setup()` runs
one iteration, and it leaves `fuzz/<program>/target` warm, so the first *authored* compile builds one
crate instead of the whole graph.

The task is created with `run.unmetered_runner`, not `run.runner`: the run's semaphore budgets
concurrent *agents* (`--max-concurrent`), and a multi-minute cargo build charged to it would silently
take a quarter of the default concurrency away from the analysis it overlaps.

On the backend seam this costs one descriptor field (`preflight: Option<PreflightSpec>` — presence
opts in; the workspace prep runs either way) and **no new callout**: `compile` already had the right
shape, and `PipelineBackend.preflight` returns a backend-defined value that the driver carries,
opaquely, into `prepare_system`.

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
  `{accept, feedback}` JSON the host's `_parse_judge` reads. Whenever a wheel supplies a judge for an
  input, the host runs it as a `request_review` **tool inside the author session** — the author
  self-revises and can only finalize an accepted draft (see `docs/crucible-judge-in-loop.md`) — so
  there is no separate post-authoring judge turn. A wheel with no judge (`judge_prompt → None`) gets
  the plain single-shot author.
- `compile(input, spec, workdir, sandbox)` → `run_confined(sandbox, "crucible", ["run", program,
  probe, "--release", "--dry-run"], files={"fuzz/<program>/src/main.rs": fixture+spec}, workdir)`;
  `Failed{errors: tail}` if `is_build_error(out)` or nonzero exit, else `Ok`.
- `validate(input, spec, target, workdir, sandbox)` → `run_confined(sandbox, "crucible", ["run",
  program, target, "--release", "--mode", "explore", "--timeout", n], files={main.rs: fixture+spec},
  workdir)`. `target` is the fuzz feature (Crucible's per-component `c_<slug>`, shared by every
  invariant — see `docs/crucible-unit-granularity.md`). The run is classified once (`[FUZZ_FINDING]`
  → BAD, exit 0 → GOOD, build markers → `BuildFailed`) and **attributed by the backend** to a
  `Verdict` per report unit the target covers (`ValidateOutcome::Verdicts`): a counterexample is
  pinned to the property whose title the finding names (assertions are tagged `[<title>]`), the rest
  held GOOD. The host records these verbatim — it owns no verdict logic and never parses a finding.
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
mechanism (`crucible_kb`, imported via `composer.scripts.rag_import`, surfaced as `env.rag_tools`), `_llm_agent`
(now called directly by the pipeline), and `run_prover`/`run_feedback` only for the run-service
exception.
**Add**: the `Backend` callouts above (pure `descriptor`/`validate_preconditions`/`units`/
`author_prompt`/`judge_prompt`/`finalize` + the two GIL-releasing blocking `compile`/`validate`), the shared
`autoprover_sdk::run_confined` helper (which just prepends Python's opaque `argv_prefix` — the
`SandboxPolicy` → `run-confined` argv assembly stays in the Python `launcher` provider), and one
generic `formalize` in the core pipeline.

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
the pure command-building in the backend, and `SandboxConfig.build_policy` /
`LauncherProvider.argv_prefix` in Python. A test asserts the launched argv is
`[*argv_prefix, "crucible"/"cargo", …]` (the prefix being `[run-confined, …policy…, "--"]`) and
never contains LLM text.

## 8. Decisions and deferrals

Resolved:

- **Shared launcher helper — yes.** `autoprover_sdk::run_confined` prepends the Python-authored
  `Sandbox.argv_prefix` to the backend's command; every backend calls it (§2).
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
