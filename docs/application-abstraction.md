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

Two ship as hand-written verticals:

| Application | Deliverable | Backend | Entry points |
|---|---|---|---|
| **autoprove** | CVL `.spec` + `.conf`, verified by the Certora Prover | `ProverBackend` | [tui_autoprove.py](../composer/cli/tui_autoprove.py) · [console_autoprove.py](../composer/cli/console_autoprove.py) |
| **foundry** | `.t.sol` tests, gated by `forge test` | `FoundryBackend` | [tui_foundry.py](../composer/cli/tui_foundry.py) · [console_foundry.py](../composer/cli/console_foundry.py) |

Crucially, "application" is **not** a single class. It is a *convention*: a set of
five collaborating pieces, each an implementation of a shared abstraction, wired
together by a thin `main()`. The value of the convention is that the pieces are
mutually orthogonal — you can swap the frontend (TUI ↔ console) without touching the
pipeline, and swap the backend without touching either frontend.

There is also a third path that writes **none** of the five by hand: an application whose backend
is a Rust wheel *declares* them in an `AppDescriptor`, and the generic host in
[composer/rustapp/](../composer/rustapp/) synthesizes the phase enum, entry point, frontend, store
and `main()` from that declaration. The convention below is what that host implements, so this doc
is still the model — see [rust-applications.md](./rust-applications.md) for the declarative form.

---

## 2. The five pieces of an application

Every application is assembled from exactly these, each keyed off one shared
type parameter — the application's **phase enum** `P`:

```
        ┌─────────────────────────────────────────────────────────────┐
        │ main()  (composer/cli/*.py)                                 │
        │   async with entry_point(summary) as run:   ← the Executor  │
        │       app = FrontendApp()                   ← the Frontend  │
        │       await run(app.make_handler)           ← the seam      │
        └─────────────────────────────────────────────────────────────┘
                 │                                        │
     ┌───────────▼────────────┐              ┌─────────────▼─────────────┐
     │ 2. Entry point /       │              │ 4. Frontend               │
     │    Executor            │              │    MultiJobApp[P, H]      │
     │  argv → services →     │              │    OR console handler     │
     │  a run(handler) closure│              │  supplies make_handler:   │
     └───────────┬────────────┘              │  HandlerFactory[P, H]     │
                 │ calls                     └───────────────────────────┘
     ┌───────────▼───────────┐
     │ 3. Pipeline           │        1. Phase enum  P: HasName
     │  run_pipeline(backend)│           (the spine that threads all
     │  + a PipelineBackend  │            five together)
     └───────────┬───────────┘
                 │ contributes
     ┌───────────▼───────────┐
     │ 5. Artifact store     │
     │  on-disk layout       │
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
    DISCOVER_DESIGN_DOC = "discover_design_doc"
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
    DISCOVER_DESIGN_DOC = "discover_design_doc"
    SYSTEM_ANALYSIS = "system_analysis"
    PROPERTY_EXTRACTION = "property_extraction"
    TEST_GENERATION = "test_generation"
    REPORT = "report"
```

Both carry `DISCOVER_DESIGN_DOC` because design-doc discovery is a *pre-pipeline* task the shared
entry point runs (only when `system_doc` was omitted), and it still needs a phase to be grouped
under — the application passes the member as `cli_pipeline`'s `design_doc_phase` (§5).

The phase serves two roles:

- **Grouping in the UI.** The frontend maps each phase to a human label and an
  ordering, and every task lands in the section for its phase:

  ```python
  # composer/ui/foundry_app.py
  FOUNDRY_PHASE_LABELS = {
      FoundryPhase.DISCOVER_DESIGN_DOC: "Design Doc Discovery",
      FoundryPhase.SYSTEM_ANALYSIS: "System Analysis",
      FoundryPhase.PROPERTY_EXTRACTION: "Property Extraction",
      FoundryPhase.TEST_GENERATION: "Test Generation",
  }
  FOUNDRY_SECTION_ORDER = [
      "Design Doc Discovery", "System Analysis", "Property Extraction", "Test Generation",
  ]
  ```

  A phase may be absent from the labels (foundry's `REPORT` is): the label map drives *sections*,
  so an unlabelled phase simply gets no section of its own.

- **The driver ↔ backend contract.** The shared driver tags four *core* phases; the
  backend maps its own enum onto them via `CorePhases[P]` (see §6). Note the two enums
  above differ in granularity: foundry has five phases, autoprove has nine — the
  prover contributes several extra prep phases (harness, autosetup, summaries,
  invariants) that the driver never knows about. The enum is the application's own
  vocabulary; only the four core slots are shared.

---

## 5. Piece 2 — the entry point / Executor

Each application has an `_entry_point` async context manager that yields the Executor closure. It
owns only what is *its own* — the argument parser and the choice of env/backend/ecosystem — and
delegates all the imperative service setup to one shared context manager,
[`cli_pipeline`](../composer/pipeline/cli.py):

> parse args → `cli_pipeline` (services, design-doc resolution, cache root) → build this
> application's env + backend → hand them to the continuation.

```python
# composer/foundry/entry.py  (shape shared by composer/spec/source/autoprove_common.py)
@asynccontextmanager
async def _entry_point(summary: RunSummary) -> AsyncIterator[FoundryRunner]:
    args = cast(FoundryArgs, _build_parser().parse_args())
    thread_id = f"foundry_{uuid.uuid4().hex[:12]}"

    async def runner(fact: HandlerFactory[FoundryPhase, None]) -> FoundryPipelineResult:
        async with (
            cli_pipeline(
                args=args, thread_id=thread_id, summary=summary, task_handler=fact,
                design_doc_phase=FoundryPhase.DISCOVER_DESIGN_DOC,
                at_exit=_usage_exit_logger(summary), workflow="foundry",
            ) as (staged, cont),
            PostgreSQLRAGDatabase.rag_context(staged.embed_model, args.rag_db) as rag,
        ):
            env = build_foundry_env(model_provider=staged.llm_models, rag_db=rag, ...)
            return await cont(env, backend(source_input=staged.source, ...), EVM)

    yield runner
```

`cli_pipeline` is the shared half — one implementation, not a per-application convention:

1. Resolves `project_root` + `main_contract` (`path:ContractName`).
2. Opens the shared connection stack — `standard_connections`, the async tool context, the
   thread logger — under a single `async with`.
3. **Resolves the design doc**: the supplied `system_doc`, or, when it was omitted, discovers one
   as a visible task tagged with the caller's `design_doc_phase`. This is why discovery needs a
   phase member (§4) and why it happens inside the handler scope.
4. Computes the **root cache key** (project root + doc bytes + relative path + contract name),
   records the run's cache tags, and reads an optional threat model.
5. Yields `(staged, cont)`: a `StagedPipeline` of everything the application needs to build its
   env and backend (connections, models, embedder, `SourceCode`, logger, `root_key`), and a
   `Continuation` that takes `(env, backend, ecosystem)`, builds the `PipelineRun`, and runs the
   driver.

What stays per-application is exactly the part that differs: the parser, the tool/RAG env, the
backend, and which ecosystem it targets.

The args are declared as a **Protocol** (`AutoProveArgs`, `FoundryArgs`), not a class,
so the parser and the typed access agree without a dataclass in between:

The shared half declares what it needs of them as `PipelineArgs`
([pipeline/cli.py](../composer/pipeline/cli.py)) — `project_root`, `main_contract`, `system_doc`,
the cache/memory namespaces, concurrency, recursion limit, `interactive`, `threat_model`,
`max_bug_rounds` — and each application's protocol extends that with its own:

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

Convention points worth naming:

- **Foundry validates its precondition in the entry point** (`foundry.toml` must
  exist) — application-specific input validation belongs here, before any service is
  opened.
- **Each application owns its RAG DB choice.** Foundry overrides `--rag-db`'s default
  to the cheatcodes DB via a Protocol (`FoundryRAGDBOptions`) rather than a new flag.
- **Run-close artifacts land in an `at_exit` hook** passed to `cli_pipeline` (both apps dump
  `token_usage.json` there). `cli_pipeline` calls it from its own `finally` and swallows what it
  raises, so a diagnostics failure can't mask the run's own outcome.
- **The entry point never imports a frontend.** It yields the Executor; `main()`
  chooses the frontend. That is what lets one entry point back both a TUI and a
  console `main()`.

---

## 6. Piece 3 — the pipeline and its backend

The application builds a `PipelineBackend` and hands it, with its env and its ecosystem, to
`cli_pipeline`'s continuation. The continuation is where the `PipelineRun` is assembled and the
shared driver [`run_pipeline`](../composer/pipeline/core.py) is called — so no application writes
that wiring:

```python
# composer/pipeline/cli.py — the Continuation yielded by cli_pipeline
async def cont(env, backend, ecosystem) -> CorePipelineResult[FormT]:
    run = PipelineRun(
        ctx=full_ctx, source=full_source, env=env,
        _semaphore=semaphore, _handler_factory=task_handler,
    )
    return await run_pipeline(
        backend=backend, run=run, ecosystem=ecosystem,
        interactive=args.interactive, max_bug_rounds=args.max_bug_rounds,
        threat_model=threat_model,
    )
```

```python
# each application supplies only the backend (composer/foundry/pipeline.py)
def backend(*, forge_binary, forge_timeout_s, source_input, forge_concurrency) -> FoundryBackend:
    return FoundryBackend(FoundryArtifactStore(source_input.project_root),
                          _ForgeRunConfig(forge_binary, forge_timeout_s, ...))
```

Two things to notice. The `handler_factory` (the frontend seam) and the run's two concurrency
semaphores are bundled into the `PipelineRun` — `run.runner(task_info, job)` is how every phase of
the driver spins up a task through whatever frontend was supplied, and `run.cpu_runner` is its peer
for a task that is *not* an agent (a toolchain build): same task machinery, charged to the CPU
budget (`--max-cpu-tasks`) rather than to the `--max-concurrent` agent slots. And the **ecosystem** is an explicit argument to the driver, not something
the backend carries: it supplies the analyzed-model type, the analysis prompts, `locate_main` and
the unit enumeration, so one backend shape can target more than one chain (see
[ecosystem-abstraction.md](./ecosystem-abstraction.md)).

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

Everything below this — `preflight`, `prepare_system`, `prepare_formalization`, `formalize`,
`fetch_verdicts` — is the **formalization abstraction**, documented in full in
[formalization-abstraction.md](./formalization-abstraction.md). The one-line summary
of the contrast:

| | autoprove (`ProverBackend`) | foundry (`FoundryBackend`) |
|---|---|---|
| `FormT` | `GeneratedCVL` | `GeneratedFoundryTest` |
| `preflight` | none | none (a building backend — Crucible — puts its build here, overlapping analysis) |
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
            app.mark_pipeline_done()
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

1. **Phase enum** `P(enum.Enum)` — your task-grouping vocabulary, with the four core phases
   (analysis / extraction / formalization / report) representable, plus a member to group
   design-doc discovery under.
2. **Backend** — implement `PipelineBackend`, naming its eight type arguments, and its phase
   objects (`preflight` → `prepare_system` → `PreparedSystem.prepare_formalization` →
   `Formalizer`, or a `StagedFormalizer` when every unit shares one artifact), plus
   `backend_guidance`, `core_phases`, `analysis_spec`, `artifact_store`, `to_artifact_id`. (Full
   checklist in [formalization-abstraction.md §9](./formalization-abstraction.md).)
3. **Artifact store** — subclass `ArtifactStore`; define an `ArtifactIdentifier`.
4. **Result type** `FormT` satisfying `FormalResult` + `ReportableResult`.
5. **Entry point** — an `_entry_point` context manager that parses args (declared as a `Protocol`
   extending `PipelineArgs`) and yields a `runner(handler)` closure which opens `cli_pipeline`,
   builds the env + backend from the `StagedPipeline`, and calls the continuation with its
   ecosystem. There is no per-application pipeline wrapper to write.
6. **Frontend(s)** — a `MultiJobApp[P, T]` subclass (phase labels, section order,
   `create_task_handler`, per-task event streaming) and/or a console handler.
7. **`main()`** — glue an entry point to a frontend via `run(app.make_handler)`.

The dependency direction is the guardrail: `main` → (entry point, frontend);
entry point → `cli_pipeline` → driver; driver → backend + `PipelineRun(handler_factory)`. Frontend
and backend never reference each other, and neither references `main`. Keep those
edges and the pieces stay swappable.

For a Rust-wheel backend, steps 1–7 are all *declared* instead of written; see
[rust-applications.md](./rust-applications.md).

---

## 11. Key files

| Piece | autoprove | foundry | shared abstraction |
|---|---|---|---|
| Phase enum | [autoprove_app.py](../composer/ui/autoprove_app.py) | [foundry/pipeline.py](../composer/foundry/pipeline.py) | `HasName` ([multi_job.py](../composer/io/multi_job.py)) |
| Entry point / Executor | [autoprove_common.py](../composer/spec/source/autoprove_common.py) | [foundry/entry.py](../composer/foundry/entry.py) | `cli_pipeline` ([pipeline/cli.py](../composer/pipeline/cli.py)) |
| Backend | [spec/source/pipeline.py](../composer/spec/source/pipeline.py) | [foundry/pipeline.py](../composer/foundry/pipeline.py) | [pipeline/core.py](../composer/pipeline/core.py) · [pipeline/ptypes.py](../composer/pipeline/ptypes.py) |
| Frontend (TUI) | [autoprove_app.py](../composer/ui/autoprove_app.py) | [foundry_app.py](../composer/ui/foundry_app.py) | [multi_job_app.py](../composer/ui/multi_job_app.py) |
| Frontend (console) | [autoprove_console.py](../composer/ui/autoprove_console.py) | [foundry_console.py](../composer/ui/foundry_console.py) | [multi_console_handler.py](../composer/ui/multi_console_handler.py) |
| Artifact store | [spec/source/artifacts.py](../composer/spec/source/artifacts.py) | [foundry/artifacts.py](../composer/foundry/artifacts.py) | [spec/artifacts.py](../composer/spec/artifacts.py) |
| The seam | — | — | `HandlerFactory` / `TaskInfo` / `TaskHandle` ([multi_job.py](../composer/io/multi_job.py)) |
| `main()` | [tui_autoprove.py](../composer/cli/tui_autoprove.py) · [console_autoprove.py](../composer/cli/console_autoprove.py) | [tui_foundry.py](../composer/cli/tui_foundry.py) · [console_foundry.py](../composer/cli/console_foundry.py) | — |
| All five, declared | — | — | [composer/rustapp/](../composer/rustapp/) ([doc](./rust-applications.md)) |
