# Design Doc — Rust Applications via PyO3

> How to stand up a whole new AutoProver *application* — phase enum, entry point, pipeline,
> frontend, artifact store, and `main()` — when the formalization backend is written in Rust.
> Where does the Python/Rust line fall across the five pieces, and how little bespoke Python
> can an extension author get away with writing?
>
> Companion to [application-abstraction.md](./application-abstraction.md) (the five pieces of
> an application) and [rust-formalization-backends.md](./rust-formalization-backends.md) (the
> backend seam in Rust, incl. the Tier-2 inversion-of-control authoring loop). This doc
> assumes both: it reuses the backend design wholesale and only addresses the *rest of the
> vertical* an application needs.

---

## 1. The dividing line

An application is [five pieces plus a `main()`](./application-abstraction.md): a phase enum,
an entry point/Executor, a pipeline+backend, a frontend, and an artifact store. The instinct
"write the application in Rust" is the wrong framing, because these pieces are not
symmetric — they fall cleanly into two camps:

- **Imperative async Python shell** — the entry point (live Postgres pools, a RAG DB, a
  background event drainer, `ContextVar`-scoped handler installation, the `composer.bind`
  import-time DI/monkeypatch bootstrap), the frontend (a Textual TUI / stdout console owning
  the event loop), and `main()`. None of this is data; it is service lifecycle and UI. It
  **must stay Python**.
- **Pure logic + declarations** — the backend's decisions (covered in the backend doc), the
  on-disk artifact *format*, and a handful of *declarations* (what phases exist, what CLI
  flags exist, what the deliverable layout is). This is what Rust is good for.

So the same principle that governs the backend seam governs the whole application:

> **Rust declares and decides; Python wires and does.** Rust never owns an event loop, a DB
> connection, a Textual widget, or an `async with`. It contributes data (a descriptor) and
> pure step functions (the backend). Python owns every imperative, stateful, async edge.

Under that principle, "a Rust application" is really **a Rust backend + a Rust
*descriptor*, both consumed by a reusable Python host.** The rest of this doc defines that
descriptor and the host.

---

## 2. Where each piece lands

| Piece | Camp | Rust contributes | Python owns |
|---|---|---|---|
| **Phase enum `P`** | declaration | list of phase names + which map to the 4 core slots | synthesizes the `enum.Enum` at runtime from that list |
| **Entry point / Executor** | Python shell | a declarative arg schema, a sync precondition-validation hook, a RAG-DB default | argparse, all service setup, `WorkflowContext`, `composer.bind`, the `runner` closure |
| **Pipeline wrapper** | Python shell | — | the thin `run_<app>_pipeline` that builds backend + `PipelineRun` + calls `run_pipeline` |
| **Backend** | Rust core | the `PipelineBackend`/`PreparedSystem`/`Formalizer` logic (see [backend doc](./rust-formalization-backends.md)) | the adapter shim + the Tier-2 effect loop |
| **Frontend** | Python shell | domain-event *content* (pushed across FFI), phase labels + section order | the `MultiJobApp`/console handler, `get_stream_writer()` emission, all rendering |
| **Artifact store** | mixed | the format-specific bundle formatter, the deliverable layout paths | the `ArtifactStore` shell + base writes (`properties.json`, `commentary.md`, …) |
| **`main()`** | Python shell | — | the ~20-line glue; a *generic* one parameterized by the descriptor |

The striking result of grounding this in the code: **only the backend and the artifact
*formatter* are genuinely Rust; everything else is either a declaration Rust hands over as
data, or Python the extension author does not have to write at all** — if we build the host
in §3.

---

## 3. The `AppDescriptor` and the generic host

The leverage move is to make the entire non-backend surface **declarative**, so a single
reusable Python "app host" can synthesize the phase enum, the argparse, the entry point, the
frontend, and `main()` from one struct the Rust wheel exports.

### 3.1 What Rust exports

```rust
pub struct AppDescriptor {
    name: String,                       // "myprover"
    header_text: String,                // TUI header
    phases: Vec<PhaseSpec>,             // ordered; each: { key, label, core_slot: Option<CoreSlot> }
    args: Vec<ArgSpec>,                 // extra CLI flags beyond the 3 positional inputs
    rag_db_default: Option<String>,     // override the --rag-db default (cf. foundry cheatcodes DB)
    event_kinds: Vec<EventKind>,        // domain events the frontend should render (see §4.4)
    artifact_layout: ArtifactLayout,    // deliverable dir conventions (see §4.6)
    // the backend itself:
    backend: RustBackendHandle,         // constructs the PipelineBackend (backend doc)
}

enum CoreSlot { Analysis, Extraction, Formalization, Report }

struct PhaseSpec { key: String, label: String, order: u32, core_slot: Option<CoreSlot> }
struct ArgSpec   { flag: String, help: String, default: ArgDefault, required: bool }
```

The whole thing serializes to JSON at load time; the four `CoreSlot` values are exactly the
[`CorePhases`](../composer/pipeline/core.py) TypedDict keys the driver tags, so `phases` both
defines the UI vocabulary and supplies the `core_phases` mapping.

### 3.2 What the Python host does with it

```python
# composer/rustapp/host.py  (NEW, write-once, reused by every Rust app)
def build_application(desc: dict) -> Application:
    Phase = enum.Enum(f"{desc['name']}Phase",           # ← synthesized enum, safe (§4.1)
                      {p["key"]: p["key"] for p in desc["phases"]})
    labels = {Phase[p["key"]]: p["label"] for p in desc["phases"]}
    core   = CorePhases({slot: Phase[p["key"]]          # analysis/extraction/formalization/report
                         for p in desc["phases"] if (slot := p.get("core_slot"))})
    return Application(
        phase=Phase, labels=labels, section_order=[...],
        make_entry_point=lambda summary: _generic_entry_point(desc, Phase, core, summary),
        make_frontend=lambda: _GenericRustApp(desc, Phase, labels),   # MultiJobApp subclass
        make_backend=lambda store, run: RustBackendAdapter(desc["backend"], core, store),
    )
```

An extension author then ships **only** the Rust wheel (backend + descriptor) plus a
one-line registration; the generic `main()`/entry point/frontend come from the host. That is
the end state: **zero bespoke Python per application.**

> **Scope decision.** The generic host is net-new infrastructure. It is worth building only
> once we expect ≥2 Rust applications. For the first one, hand-writing the ~100 lines of
> Python glue (per [application-abstraction.md §10](./application-abstraction.md)) around the
> Rust backend is faster and lower-risk. §7 phases this.

---

## 4. Piece by piece

### 4.1 Phase enum — Rust declares, Python synthesizes

Grounded in the code: `P` is used at runtime **only** for `.name` (logging/labels,
[multi_job.py](../composer/io/multi_job.py)) and as a hashable dict key (`phase_labels`,
`CorePhases`). There are no `isinstance` checks on phases, no enum-identity comparisons, and
the generic bound is `enum.Enum`, not the concrete class (and PEP 695 bounds aren't enforced
at runtime anyway). So `enum.Enum(f"{name}Phase", {...})` synthesized from the descriptor is
safe — the *only* rule is that `phase_labels` must be keyed by those same synthesized members
(member identity drives the lookup at [multi_job_app.py](../composer/ui/multi_job_app.py)),
which §3.2 satisfies by construction.

### 4.2 Entry point / Executor — stays Python, Rust declares its inputs

The entry point is irreducibly imperative async Python: it opens `standard_connections` (four
Postgres-backed pools — checkpointer, store, pgvector indexed store, memory backend), a RAG
DB, the async tool context, and a thread logger under a nested `async with`; builds the
`ServiceHost` env + `WorkflowContext`; reads the design doc via the async `FileUploader`; and
runs `import composer.bind` for its import-time DI registration and test-tape monkeypatching
([autoprove_common.py](../composer/spec/source/autoprove_common.py),
[composer/bind.py](../composer/bind.py)). **A Rust backend slots in only at the leaf** — the
`Formalizer` the pipeline eventually calls.

What Rust *does* contribute here is purely declarative, consumed by the generic entry point:

- **Extra CLI args** — the `args: Vec<ArgSpec>` become `parser.add_argument(...)` calls; the
  three positional inputs (`project_root`, `main_contract`, `system_doc`) are always present.
  This replaces the per-app `AutoProveArgs`/`FoundryArgs` Protocol.
- **A precondition-validation hook** — foundry validates `foundry.toml` exists *in the entry
  point before opening services* ([application-abstraction.md §5](./application-abstraction.md)).
  Rust exposes a **sync** `fn validate_preconditions(args_json) -> Result<(), String>` the
  generic entry point calls right after arg parsing. Sync is fine — it's pure filesystem
  checks, no async needed.
- **The RAG-DB default** — `rag_db_default` overrides `--rag-db`'s default, exactly as
  `FoundryRAGDBOptions` does today, without a new flag.

### 4.3 Pipeline wrapper + backend — see the backend doc

The `run_<app>_pipeline` wrapper stays a thin Python function (build backend + `PipelineRun`
+ call `run_pipeline`); the generic host provides it. The backend itself — `prepare_system`,
`prepare_formalization`, `formalize` (incl. the Tier-2 LLM authoring loop), `fetch_verdicts`,
`finalize` — is the subject of [rust-formalization-backends.md](./rust-formalization-backends.md)
and is not re-derived here. The one link back: the descriptor's `core_phases` mapping (§3.1)
is the backend's `core_phases` slot; the phase enum synthesized in §4.1 is what stamps
`TaskInfo`.

### 4.4 Frontend — Python renders; Rust supplies event *content*

The frontend is a Textual `MultiJobApp` / stdout console — it owns the event loop and every
widget, and it must stay Python. A generic `MultiJobApp` subclass driven by the descriptor's
`phase_labels`/`section_order` handles the structure for free.

The subtlety is **domain event streaming**. Applications stream backend-specific events to
their task panels (`forge_test_run`, `prover_output`, `cloud_polling`). Grounded in the code,
these are emitted by calling LangGraph's `get_stream_writer()(payload)` from *inside a running
graph node/tool*, or `emit_custom_event(payload)` off-graph — both require being inside the
async `with_handler` scope, and both take a plain `dict` with a discriminating `type` key
([foundry/runner.py](../composer/foundry/runner.py),
[composer/prover/runner.py](../composer/prover/runner.py),
[composer/io/context.py](../composer/io/context.py)). **Rust cannot call `get_stream_writer()`.**

The clean resolution reuses the backend doc's Tier-2 machinery. The Python effect loop that
drives the Rust decider *is* inside the handler scope, so add one fire-and-forget command to
the vocabulary:

```rust
Command::Emit { kind: String, payload: Json },   // Rust → Python: "stream this to my panel"
```

The Python driver, being in-scope, does `get_stream_writer()({"type": cmd.kind, **cmd.payload})`
and immediately resumes the Rust decider with `Observation::Ack`. This mirrors exactly how the
Certora prover surfaces stdout lines through the `ProverEventCallbacks` shim today — Rust
controls the *content*, Python does the emission. The descriptor's `event_kinds` tells the
generic frontend how to render each `type` (which panel/log, and whether it's plain text), so
even the `handle_event` body is generated, not hand-written.

### 4.5 Artifact store — Python shell, Rust formatter

Same split as the backend doc: the `ArtifactStore` subclass stays a Python shell (the base
writes the shared `properties.json` / `commentary.md` / property→units map / `token_usage.json`),
and Rust supplies the format-specific bundle formatter and the deliverable-layout paths (the
descriptor's `artifact_layout`). This is where an app declares that, e.g., its deliverables go
under `<test dir>/*.t.sol` with metadata under `certora/<app>/` — the "share a project root
without colliding" convention ([application-abstraction.md §8](./application-abstraction.md)).

### 4.6 `main()` — generic, parameterized by the descriptor

The two `main()` shapes (TUI worker vs console-direct) are ~20 lines each and differ only in
who owns the event loop. The host provides both, parameterized by the descriptor; the
extension author picks TUI/console at the CLI entry point. The one invariant to preserve:
`import composer.bind as _` runs first (import-time DI/tape bootstrap).

---

## 5. New infrastructure this requires

Most of the application already composes for free; the genuinely net-new pieces are:

1. **The `AppDescriptor` JSON schema** — the versioned contract between a Rust wheel and the
   host (phases, args, event kinds, artifact layout). Shared with the backend doc's
   marshalling/ABI schemas.
2. **The generic Python host** (`composer/rustapp/host.py`) — synthesizes enum + argparse +
   entry point + frontend + `main()` from a descriptor. This is the reusable payoff; write it
   once.
3. **A generic `MultiJobApp` subclass** whose `create_task_handler` / `handle_event` are
   data-driven by `event_kinds`, rather than a hand-written subclass per app.
4. **The `Command::Emit` extension** to the Tier-2 effect vocabulary (§4.4), so a Rust
   backend can stream domain events without touching `get_stream_writer()`.
5. **A registration entry point** — how a Rust wheel advertises its descriptor to the host
   (e.g. a Python `[project.entry-points]` group, or an explicit `register(desc)` call).

Everything else — the driver, the seam (`HandlerFactory`), the `MultiJobApp` base, caching,
the report — is inherited unchanged.

---

## 6. Hypothetical: a Rust "MyProver" application end to end

Putting §§3–4 together, standing up a Rust application called `myprover`:

```
myprover (Rust wheel)                          composer host (Python, reusable)
├─ AppDescriptor {                             build_application(desc):
│    name: "myprover",                           Phase = enum.Enum("MyproverPhase", …)   ← §4.1
│    phases: [Analyze→analysis, Extract→          labels/section_order  from desc
│             extraction, Prove→formalization,    core_phases           from desc.core_slot
│             Report→report, +Setup(no slot)],  _generic_entry_point(desc):              ← §4.2
│    args: [--solver-timeout, --parallel],        argparse from desc.args
│    rag_db_default: "myprover_kb",               validate_preconditions(args)  ↺ Rust (sync)
│    event_kinds: [solver_line, proof_step],      open Postgres pools / RAG / ctx / bind
│    artifact_layout: certora/myproof/…,          yield runner(handler):
│  }                                                RustBackendAdapter(desc.backend, …)   ← backend doc
├─ validate_preconditions()  (sync)            _GenericRustApp(desc):                     ← §4.4
├─ backend: PipelineBackend                       phase panels from labels
│    ├─ prepare_system / prepare_formalization     handle_event dispatches by event_kind
│    └─ formalize:  Tier-2 decider  ─────┐      generic main() (TUI or console)           ← §4.6
│         emits Command::Emit{solver_line}│
└─ artifact bundle formatter             └──────▶ Python effect loop calls
                                                  get_stream_writer()({type:"solver_line",…})
```

The author writes Rust (a backend + a descriptor) and *nothing else*: no argparse, no Textual
code, no entry-point service wiring, no `main()`. The `Setup` phase with no core slot shows an
app contributing its own UI-only phase, exactly as autoprove's harness/autosetup phases do.

---

## 7. Work breakdown

### Phase A — one Rust application, hand-written Python glue
Prove the vertical before generalizing. Reuse the backend milestones from
[rust-formalization-backends.md §6](./rust-formalization-backends.md), then hand-write the
~100 lines of app glue per [application-abstraction.md §10](./application-abstraction.md):
a concrete phase enum, a concrete `_entry_point`, a concrete `MultiJobApp` subclass, a
`run_<app>_pipeline`, and both `main()`s. Wire domain events through a `ProverCallbacks`-style
shim. This ships a working Rust application and surfaces the real friction.

### Phase B — extract the `AppDescriptor` + generic host
Once Phase A works, factor the hand-written glue into the descriptor schema (§3.1) and the
generic host (§5.1–3). Migrate the Phase-A app onto it; a second Rust app should then need
zero bespoke Python.

### Phase C — event-emission ergonomics
Add `Command::Emit` (§4.4) and the data-driven `handle_event`, so streaming domain events is
declarative rather than a per-app Python callback.

---

## 8. Open questions

1. **Build the host, or hand-write glue per app?** The generic host pays off only at ≥2 Rust
   apps. Until then Phase A's hand-written glue is cheaper. Decide based on the roadmap.
2. **Interactive applications (`H ≠ None`).** Both current apps are non-interactive
   (`H = None`; handlers raise on HITL). An interactive Rust app would need the backend to
   *await* a human response mid-loop — which, unlike everything else here, genuinely needs the
   Tier-3 async bridge (or an `Emit`-style command whose observation is the human's answer,
   routed through the Python HITL handler). The `Emit`-with-response route keeps us bridge-free
   and is preferred.
3. **Descriptor registration.** Python entry-points group vs explicit `register()` — how does
   the host discover a Rust wheel's descriptor, and how are version mismatches surfaced at
   load time?
4. **Event rendering richness.** `event_kinds` as "write this text to this log" covers the
   current apps. If an app wants a custom widget (progress bar, table), does it fall back to a
   hand-written `MultiJobApp` subclass, or do we grow the descriptor? Start with text; grow
   only on demand.

---

## 9. Key files

| Concern | File |
|---|---|
| The five pieces of an application | [application-abstraction.md](./application-abstraction.md) |
| The Rust backend seam (incl. Tier-2 loop) | [rust-formalization-backends.md](./rust-formalization-backends.md) |
| Phase-enum runtime use (`.name`, dict key) | [composer/io/multi_job.py](../composer/io/multi_job.py) · [composer/ui/multi_job_app.py](../composer/ui/multi_job_app.py) |
| Entry point / service wiring (must stay Python) | [composer/spec/source/autoprove_common.py](../composer/spec/source/autoprove_common.py) · [composer/foundry/entry.py](../composer/foundry/entry.py) |
| DI / tape bootstrap | [composer/bind.py](../composer/bind.py) |
| Domain-event emission (`get_stream_writer` / `emit_custom_event`) | [composer/io/context.py](../composer/io/context.py) · [composer/prover/runner.py](../composer/prover/runner.py) · [composer/foundry/runner.py](../composer/foundry/runner.py) |
| Frontend base + seam | [composer/ui/multi_job_app.py](../composer/ui/multi_job_app.py) · [composer/io/multi_job.py](../composer/io/multi_job.py) |
| `CorePhases` / driver | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| Generic host (NEW) | `composer/rustapp/host.py` |
