# Design Doc — What Is an AutoProver "Application"

> How the pieces — argument parsing, service setup, the pipeline, and the UI — are
> wired into a single, runnable *application* such as **autoprove** or **foundry**,
> and the conventions a new application is expected to follow.
>
> Companion to [ARCHITECTURE.md](../ARCHITECTURE.md) and
> [formalization-abstraction.md](./formalization-abstraction.md). Where the
> formalization doc zooms into the *backend* seam (how a property becomes a verified
> artifact), this doc zooms out to the *whole vertical*: everything from `argv` to a
> rendered TUI. The [MultiJobApp design](../composer/ui/MULTI_JOB_DESIGN.md) covers
> the generic UI base this leans on.

---

## 1. What "application" means here

An AutoProver **application** is a complete, runnable vertical slice that takes a
Solidity project + a design document and drives the shared property-extraction /
formalization pipeline to a set of on-disk deliverables, rendered live to the user.

There are two today:

| Application | Deliverable | Backend | Entry points |
|---|---|---|---|
| **autoprove** | CVL `.spec` + `.conf`, verified by the Certora Prover | `ProverBackend` | [tui_autoprove.py](../composer/cli/tui_autoprove.py) · [console_autoprove.py](../composer/cli/console_autoprove.py) |
| **foundry** | `.t.sol` tests, gated by `forge test` | `FoundryBackend` | [tui_foundry.py](../composer/cli/tui_foundry.py) · [console_foundry.py](../composer/cli/console_foundry.py) |

Crucially, "application" is **not** a single class. It is a *convention*: a set of
five collaborating pieces, each an implementation of a shared abstraction, wired
together by a thin `main()`. The value of the convention is that the pieces are
mutually orthogonal — you can swap the frontend (TUI ↔ console) without touching the
pipeline, and swap the backend without touching either frontend.

---

## 2. The five pieces of an application

Every application is assembled from exactly these, each keyed off one shared
type parameter — the application's **phase enum** `P`:

```
        ┌─────────────────────────────────────────────────────────────┐
        │ main()  (composer/cli/*.py)                                   │
        │   async with entry_point(summary) as run:   ← the Executor    │
        │       app = FrontendApp()                   ← the Frontend    │
        │       await run(app.make_handler)           ← the seam        │
        └─────────────────────────────────────────────────────────────┘
                 │                                        │
     ┌───────────▼───────────┐              ┌─────────────▼─────────────┐
     │ 2. Entry point /       │              │ 4. Frontend               │
     │    Executor            │              │    MultiJobApp[P, H]       │
     │  argv → services →     │              │    OR console handler      │
     │  a run(handler) closure│              │  supplies make_handler:    │
     └───────────┬───────────┘              │  HandlerFactory[P, H]      │
                 │ calls                     └───────────────────────────┘
     ┌───────────▼───────────┐
     │ 3. Pipeline            │        1. Phase enum  P: HasName
     │  run_pipeline(backend) │           (the spine that threads all
     │  + a PipelineBackend   │            five together)
     └───────────┬───────────┘
                 │ contributes
     ┌───────────▼───────────┐
     │ 5. Artifact store      │
     │  on-disk layout        │
     └───────────────────────┘
```

1. **Phase enum** `P` — the task-grouping vocabulary.
2. **Entry point / Executor** — argv → configured services → a `run(handler)` closure.
3. **Pipeline + backend** — the work, expressed as a `PipelineBackend` fed to the
   shared `run_pipeline` driver.
4. **Frontend** — a `MultiJobApp[P, H]` subclass (TUI) or console handler that
   supplies a `HandlerFactory[P, H]`.
5. **Artifact store** — the on-disk deliverable layout.

The rest of this doc walks each piece with the autoprove and foundry
implementations side by side.

---

## 3. The seam that makes it compose: `HandlerFactory`

Before the pieces, understand the seam between them. The pipeline and the frontend
never reference each other. They meet at one protocol,
[`HandlerFactory[P, H]`](../composer/io/multi_job.py):

```python
# composer/io/multi_job.py
class HandlerFactory[P: HasName, H](Protocol):
    def __call__(self, /, info: TaskInfo[P]) -> Awaitable[TaskHandle[H]]: ...
```

- The **pipeline** is a producer of *work*. Every task it launches is described by a
  `TaskInfo(task_id, label, phase)` and run through `run_task`, which calls the factory
  to obtain a `TaskHandle` (an `IOHandler` + `EventHandler` + lifecycle callbacks).
- The **frontend** is a producer of *handlers*. It implements the factory: given a
  `TaskInfo`, it mounts a panel, builds a per-task renderer, and returns the
  `TaskHandle`.

So the entire application boils down to:

```python
async with entry_point(summary) as run:   # run: Executor  (the pipeline, service-loaded)
    app = FrontendApp()                    # the frontend
    result = await run(app.make_handler)   # hand the factory to the pipeline
```

`run` is typed as an **Executor**, and its whole signature *is* the seam:

```python
# composer/spec/source/autoprove_common.py
type Executor = Callable[[HandlerFactory[AutoProvePhase, None]], Awaitable[CorePipelineResult[GeneratedCVL]]]
# composer/foundry/pipeline.py
type FoundryPipelineExecutor = Callable[[HandlerFactory[FoundryPhase, None]], Awaitable[FoundryPipelineResult]]
```

Because the pipeline only ever *calls* the factory and the frontend only ever
*implements* it, the two are swappable independently. That is why autoprove has both
a TUI ([`AutoProveApp`](../composer/ui/autoprove_app.py)) and a console
([`AutoProveConsoleHandler`](../composer/ui/autoprove_console.py)) frontend against
the same pipeline, selected purely by which `make_handler` `main()` passes in.

`H` is the human-interaction schema. Both current applications are non-interactive at
the per-task level (`H = None`; their handlers raise from `format_hitl_prompt`), but
the seam carries the type so an interactive application (e.g. the NatSpec pipeline)
plugs in without changing the contract.

---

## 4. Piece 1 — the phase enum `P`

Every application defines a single enum whose members are its task-grouping phases.
This enum is the type parameter that threads through the frontend
(`MultiJobApp[P, ...]`), the seam (`HandlerFactory[P, H]`, `TaskInfo[P]`), and the
backend (`CorePhases[P]`). It only needs to satisfy `HasName` (an enum trivially does).

```python
# composer/ui/autoprove_app.py
class AutoProvePhase(enum.Enum):
    HARNESS = "harness"
    AUTOSETUP = "autosetup"
    INVARIANTS = "invariants"
    SUMMARIES = "summaries"
    COMPONENT_ANALYSIS = "component_analysis"
    BUG_ANALYSIS = "bug_analysis"
    CVL_GEN = "cvl_gen"
    REPORT = "report"
```

```python
# composer/foundry/pipeline.py
class FoundryPhase(enum.Enum):
    SYSTEM_ANALYSIS = "system_analysis"
    PROPERTY_EXTRACTION = "property_extraction"
    TEST_GENERATION = "test_generation"
    REPORT = "report"
```

The phase serves two roles:

- **Grouping in the UI.** The frontend maps each phase to a human label and an
  ordering, and every task lands in the section for its phase:

  ```python
  # composer/ui/foundry_app.py
  FOUNDRY_PHASE_LABELS = {
      FoundryPhase.SYSTEM_ANALYSIS: "System Analysis",
      FoundryPhase.PROPERTY_EXTRACTION: "Property Extraction",
      FoundryPhase.TEST_GENERATION: "Test Generation",
  }
  FOUNDRY_SECTION_ORDER = ["System Analysis", "Property Extraction", "Test Generation"]
  ```

- **The driver ↔ backend contract.** The shared driver tags four *core* phases; the
  backend maps its own enum onto them via `CorePhases[P]` (see §6). Note the two enums
  above differ in granularity: foundry has three phases, autoprove has eight — the
  prover contributes several extra prep phases (harness, autosetup, summaries,
  invariants) that the driver never knows about. The enum is the application's own
  vocabulary; only the four core slots are shared.

---

## 5. Piece 2 — the entry point / Executor

Each application has an `_entry_point` async context manager that owns **all** the
imperative setup and yields the Executor closure. Its shape is a strict convention
(the foundry file's docstring literally says "Mirrors autoprove_common's shape"):

> parse args → set up DB / RAG / store / checkpointer / logging / thread logger →
> yield a closure the caller drives with a handler factory.

```python
# composer/spec/source/autoprove_common.py  (shape shared by composer/foundry/entry.py)
@asynccontextmanager
async def _entry_point(summary: RunSummary) -> AsyncIterator[Executor]:
    parser = argparse.ArgumentParser(...)
    add_protocol_args(parser, RAGDBOptions)
    add_protocol_args(parser, ExtendedModelOptions)
    parser.add_argument("project_root", ...)
    parser.add_argument("main_contract", help="Main contract as path:ContractName")
    parser.add_argument("system_doc", ...)
    # ... application-specific flags ...
    args = cast(AutoProveArgs, parser.parse_args())
    async with autoprove_executor(args, summary) as runner:
        yield runner
```

The heavy lifting is in the inner context manager, which:

1. Resolves `project_root` + `main_contract` (`path:ContractName`) + `system_doc`.
2. Computes a **root cache key** from the inputs (`_root_cache_key` hashes project
   root + doc content + relative path + contract name) — identical helper in both.
3. Opens the shared connection stack — `standard_connections`, a RAG DB, the async
   tool context, the thread logger — under a single `async with`.
4. Reads the design doc into a `SourceCode`, builds the environment (`ServiceHost`),
   and creates the `WorkflowContext`.
5. Yields a `runner(handler)` closure that calls the application's pipeline function.

The args are declared as a **Protocol** (`AutoProveArgs`, `FoundryArgs`), not a class,
so the parser and the typed access agree without a dataclass in between:

```python
# composer/spec/source/autoprove_common.py
class AutoProveArgs(ExtendedModelOptions, RAGDBOptions, Protocol):
    project_root: str
    main_contract: str
    system_doc: str
    max_concurrent: int
    cloud: bool          # ← prover-only: run jobs in the cloud
    ...
```

```python
# composer/foundry/entry.py
class FoundryArgs(TieredModelOptions, FoundryRAGDBOptions, Protocol):
    project_root: str
    main_contract: str
    system_doc: str
    forge_binary: str        # ← foundry-only
    forge_timeout_s: int     # ← foundry-only
    max_forge_runners: int   # ← foundry-only
    ...
```

The `runner` closure each yields is the Executor:

```python
# autoprove
async def runner(handler: HandlerFactory[AutoProvePhase, None]) -> CorePipelineResult[GeneratedCVL]:
    return await run_autoprove_pipeline(
        ctx=ctx, source_input=system_doc, env=source_env, handler_factory=handler,
        prover_opts=prover_opts, interactive=args.interactive, ...)

# foundry
async def runner(handler: HandlerFactory[FoundryPhase, None]) -> FoundryPipelineResult:
    return await run_foundry_pipeline(
        source_input=source_input, ctx=ctx, handler_factory=handler, env=env,
        forge_binary=args.forge_binary, forge_timeout_s=args.forge_timeout_s, ...)
```

Convention points worth naming:

- **Foundry validates its precondition in the entry point** (`foundry.toml` must
  exist) — application-specific input validation belongs here, before any service is
  opened.
- **Each application owns its RAG DB choice.** Foundry overrides `--rag-db`'s default
  to the cheatcodes DB via a Protocol (`FoundryRAGDBOptions`) rather than a new flag.
- **The `finally` block is where run-close artifacts land** (autoprove dumps
  `token_usage.json` there) — guarded so a diagnostics failure can't mask the run's
  own outcome.
- **The entry point never imports a frontend.** It yields the Executor; `main()`
  chooses the frontend. That is what lets one entry point back both a TUI and a
  console `main()`.

---

## 6. Piece 3 — the pipeline and its backend

The Executor's closure calls the application's `run_*_pipeline` function. For both
current applications, that function is a thin wrapper that constructs a
`PipelineBackend` + a `PipelineRun` and hands them to the shared driver
[`run_pipeline`](../composer/pipeline/core.py):

```python
# composer/spec/source/pipeline.py
async def run_autoprove_pipeline(source_input, ctx, handler_factory, env, *, prover_opts, ...):
    backend = ProverBackend(ProverArtifactStore(source_input.project_root, source_input.contract_name), prover_opts)
    run = PipelineRun(ctx, env, source_input, handler_factory, asyncio.Semaphore(max_concurrent))
    return await run_pipeline(backend, run, interactive=interactive, ...)
```

```python
# composer/foundry/pipeline.py
async def run_foundry_pipeline(source_input, ctx, handler_factory, env, *, forge_binary, ...):
    artifacts = FoundryArtifactStore(source_input.project_root)
    backend = FoundryBackend(artifacts, _ForgeRunConfig(forge_binary, forge_timeout_s, foundry_sem))
    run = PipelineRun(ctx, env, source_input, handler_factory, asyncio.Semaphore(max_concurrent))
    return await run_pipeline(backend, run, ...)
```

Notice the `handler_factory` (the frontend seam) is bundled into the `PipelineRun` —
`run.runner(task_info, job)` is how every phase of the driver spins up a task through
whatever frontend was supplied.

The backend itself is the four-slot contract the driver reads. The application maps
its phase enum onto the four **core phases** the driver tags:

```python
# composer/spec/source/pipeline.py                # composer/foundry/pipeline.py
core_phases = CorePhases({                          core_phases = CorePhases({
    "analysis":      AutoProvePhase.COMPONENT_ANALYSIS,   "analysis":      FoundryPhase.SYSTEM_ANALYSIS,
    "extraction":    AutoProvePhase.BUG_ANALYSIS,         "extraction":    FoundryPhase.PROPERTY_EXTRACTION,
    "formalization": AutoProvePhase.CVL_GEN,              "formalization": FoundryPhase.TEST_GENERATION,
    "report":        AutoProvePhase.REPORT,               "report":        FoundryPhase.REPORT,
})                                                  })
```

Everything below this — `prepare_system`, `prepare_formalization`, `formalize`,
`fetch_verdicts` — is the **formalization abstraction**, documented in full in
[formalization-abstraction.md](./formalization-abstraction.md). The one-line summary
of the contrast:

| | autoprove (`ProverBackend`) | foundry (`FoundryBackend`) |
|---|---|---|
| `FormT` | `GeneratedCVL` | `GeneratedFoundryTest` |
| `prepare_system` | harness lift + build prover tool | identity (`main_instance` only) |
| `prepare_formalization` | AutoSetup ∥ summaries ∥ invariants fan-out | trivial (formalizer already built) |
| `formalize` | author CVL, run prover, revise on CEX | author `.t.sol`, run `forge test` |
| `backend_guidance` | `CERTORA_BACKEND_GUIDANCE` | `FOUNDRY_BACKEND_GUIDANCE` |

`backend_guidance` deserves a note as an application-shaping convention: it is a prose
string injected into the property-extraction prompt telling the agent what the
verification surface can and can't express. Foundry's, for instance, explains that a
fuzzer can't *prove* universals but *refutations are valuable* — so the same shared
extraction step produces backend-appropriate properties without the driver knowing
anything about it.

---

## 7. Piece 4 — the frontend

The frontend implements the `HandlerFactory` seam. The TUI frontends are thin
subclasses of the generic [`MultiJobApp[P, T]`](../composer/ui/multi_job_app.py)
(see [its design doc](../composer/ui/MULTI_JOB_DESIGN.md)). A frontend supplies four
things and inherits everything else:

1. **Phase labels + section order** (constructor args), covered in §4.
2. **A per-task handler** — `create_task_handler`, returning a
   `MultiJobTaskHandler` subclass.
3. **A per-task event handler** — `create_event_handler`, for domain-specific
   streaming events beyond LLM messages.
4. **`make_handler`** — inherited from `MultiJobApp`; this *is* the `HandlerFactory`.
   It mounts the panel/summary-row and calls the two `create_*` hooks.

The autoprove and foundry TUIs are nearly identical in shape; they differ only in
what streams into a task's log. Both make their task handler double as its own
`EventHandler` via the `NullEventHandler` mixin:

```python
# composer/ui/foundry_app.py
class FoundryTaskHandler(MultiJobTaskHandler[None], NullEventHandler):
    async def handle_event(self, payload, path, checkpoint_id) -> None:
        evt = cast(ForgeTestRunEvent, payload)
        if evt["type"] == "forge_test_run":            # stream forge run summaries
            log = await self._ensure_forge_log()
            log.write(evt["summary"])

class FoundryApp(MultiJobApp[FoundryPhase, FoundryTaskHandler]):
    def __init__(self):
        super().__init__(phase_labels=FOUNDRY_PHASE_LABELS,
                         section_order=FOUNDRY_SECTION_ORDER,
                         header_text="Foundry Test Author | ...")
    def create_task_handler(self, panel, info) -> FoundryTaskHandler:
        return FoundryTaskHandler(info.task_id, info.label, panel, self, ToolDisplayConfig())
    def create_event_handler(self, handler, info) -> EventHandler:
        return handler   # handler is its own event handler
```

```python
# composer/ui/autoprove_app.py  — same structure; the domain events differ
class AutoProveTaskHandler(MultiJobTaskHandler[None], NullEventHandler):
    async def handle_event(self, payload, path, checkpoint_id) -> None:
        evt = cast(AutoProveEvent, payload)
        match evt["type"]:
            case "prover_output":  ...   # stream Certora Prover output lines
            case "cloud_polling":  ...   # stream cloud job status
```

The autoprove handler additionally implements `handle_progress_event` to stream the
AutoSetup agent's output — an example of an application surfacing a backend-specific
sub-agent in its own panel. Neither handler supports HITL, so both raise from
`format_hitl_prompt` — a deliberate, explicit opt-out of a base-class hook.

**The console frontend is the proof the seam works.** `AutoProveConsoleHandler` is a
*different* implementation of the same `HandlerFactory[AutoProvePhase, None]` that
renders to stdout instead of a Textual app:

```python
# composer/ui/autoprove_console.py
class AutoProveConsoleHandler(MultiJobConsoleHandler[AutoProvePhase]):
    """IOHandler[Never] + HandlerFactory for the auto-prove pipeline."""
```

The pipeline can't tell the difference — it only ever calls `make_handler`.

---

## 8. Piece 5 — the artifact store

Deliverables are written through an [`ArtifactStore[I, FormT]`](../composer/spec/artifacts.py)
subclass — one per application. The base owns everything identical across
applications (`properties.json`, `commentary.md`, the property→units map,
`token_usage.json`); the subclass fixes the on-disk layout and adds the
format-specific bundle. This is covered in detail in
[formalization-abstraction.md §6](./formalization-abstraction.md); the application-level
point is the *convention that both applications share a project root without
colliding*:

```
autoprove →  certora/specs/…        certora/confs/…       certora/ap_report/…
foundry   →  <test dir>/*.t.sol     certora/foundry/…     certora/foundry/reports/…
```

Foundry deliberately materializes its `.t.sol` into the foundry project's own `test/`
dir (so `forge` finds them) but keeps all metadata under `certora/foundry/`, so a
co-located autoprove run and foundry run share one project without clobbering each
other's outputs.

---

## 9. Piece 0 — the wiring: `main()`

The `main()` in each `composer/cli/*.py` is the whole application in ~20 lines. It is
the *only* place that names both an entry point and a frontend, and its job is to
glue them via the seam and translate the result into user-facing output.

```python
# composer/cli/tui_foundry.py   (tui_autoprove.py is identical in shape)
async def _main() -> int:
    summary = RunSummary()
    async with _entry_point(summary) as pipeline:      # piece 2: Executor
        app = FoundryApp()                             # piece 4: frontend
        async def work():
            result = await pipeline(app.make_handler)  # ← the seam
            app.notify(f"Foundry tests complete: {result.n_components} components, ...")
            app._pipeline_done = True
        app.set_work(work)
        await app.run_async()                          # TUI owns the event loop
        print(summary.format())
        return 0
```

Two `main()` shapes exist, differing only in who owns the event loop:

- **TUI** — the pipeline runs as a background *worker* inside the Textual app
  (`app.set_work(work); await app.run_async()`), so the UI stays responsive while
  the pipeline streams into it.
- **Console** — the pipeline runs directly and results print on completion:

  ```python
  # composer/cli/console_autoprove.py
  async with _entry_point(summary) as run:
      result = await run(AutoProveConsoleHandler().make_handler)
      print(summary.format())
      print(f"  Components: {result.n_components}   Properties: {result.n_properties}")
  ```

Both call `import composer.bind as _` first — the side-effecting binding module that
must load before anything touches the DI container.

---

## 10. Extending: defining a new application

Because each piece is an implementation of a shared abstraction, adding an
application is a fill-in-the-blanks exercise; nothing in the driver, the UI base, or
the seam changes.

1. **Phase enum** `P(enum.Enum)` — your task-grouping vocabulary, with at least the
   four core phases (analysis / extraction / formalization / report) representable.
2. **Backend** — implement `PipelineBackend[P, FormT, H, A]` and its three phase
   objects (`prepare_system` → `PreparedSystem.prepare_formalization` → `Formalizer`),
   plus `backend_guidance`, `core_phases`, `analysis_spec`, `artifact_store`,
   `to_artifact_id`. (Full checklist in
   [formalization-abstraction.md §9](./formalization-abstraction.md).)
3. **Artifact store** — subclass `ArtifactStore`; define an `ArtifactIdentifier`.
4. **Result type** `FormT` satisfying `FormalResult` + `ReportableResult`.
5. **Pipeline function** `run_<app>_pipeline(...)` — build backend + `PipelineRun`,
   call `run_pipeline`.
6. **Entry point** — an `_entry_point` context manager following the
   parse → services → `yield runner` convention; declare args as a `Protocol`.
7. **Frontend(s)** — a `MultiJobApp[P, T]` subclass (phase labels, section order,
   `create_task_handler`, per-task event streaming) and/or a console handler.
8. **`main()`** — glue an entry point to a frontend via `run(app.make_handler)`.

The dependency direction is the guardrail: `main` → (entry point, frontend);
entry point → pipeline; pipeline → backend + `PipelineRun(handler_factory)`. Frontend
and backend never reference each other, and neither references `main`. Keep those
edges and the pieces stay swappable.

---

## 11. Key files

| Piece | autoprove | foundry | shared abstraction |
|---|---|---|---|
| Phase enum | [autoprove_app.py](../composer/ui/autoprove_app.py) | [foundry/pipeline.py](../composer/foundry/pipeline.py) | `HasName` ([multi_job.py](../composer/io/multi_job.py)) |
| Entry point / Executor | [autoprove_common.py](../composer/spec/source/autoprove_common.py) | [foundry/entry.py](../composer/foundry/entry.py) | — |
| Pipeline + backend | [spec/source/pipeline.py](../composer/spec/source/pipeline.py) | [foundry/pipeline.py](../composer/foundry/pipeline.py) | [pipeline/core.py](../composer/pipeline/core.py) |
| Frontend (TUI) | [autoprove_app.py](../composer/ui/autoprove_app.py) | [foundry_app.py](../composer/ui/foundry_app.py) | [multi_job_app.py](../composer/ui/multi_job_app.py) |
| Frontend (console) | [autoprove_console.py](../composer/ui/autoprove_console.py) | — | — |
| Artifact store | [spec/source/artifacts.py](../composer/spec/source/artifacts.py) | [foundry/artifacts.py](../composer/foundry/artifacts.py) | [spec/artifacts.py](../composer/spec/artifacts.py) |
| The seam | — | — | `HandlerFactory` / `TaskInfo` / `TaskHandle` ([multi_job.py](../composer/io/multi_job.py)) |
| `main()` | [tui_autoprove.py](../composer/cli/tui_autoprove.py) · [console_autoprove.py](../composer/cli/console_autoprove.py) | [tui_foundry.py](../composer/cli/tui_foundry.py) · [console_foundry.py](../composer/cli/console_foundry.py) | — |
