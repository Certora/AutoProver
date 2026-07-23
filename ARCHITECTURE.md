# AutoProver — Architecture & High-Level Design

> A companion to the [README](README.md). The README tells you how to *run* AutoProver;
> this document explains how it is *built* — the major subsystems, the abstractions that
> hold them together, and the data flow through a run.

## 1. What it is

AutoProver (internally "AI Composer") is a **multi-agent pipeline that turns a Solidity
codebase + a design document into verified formal specifications.** Given a project root, a
main contract, and a system description, it drives a fleet of LLM agents to analyze the
system, formulate properties, author CVL (Certora Verification Language) specs, and run the
Certora Prover in a feedback loop until the specs verify (or the agent gives up with a
reason).

The same generic pipeline also powers two sibling workflows — **Foundry test generation**
and **NatSpec greenfield code generation** — which reuse the shared analysis/extraction/
reporting machinery behind a backend protocol.

## 2. Design philosophy

A handful of decisions shape the whole codebase:

- **Generic driver, pluggable backends.** One backend-agnostic driver
  ([composer/pipeline/core.py](composer/pipeline/core.py)) owns the steps that are the same
  for everyone (system analysis, property extraction, caching, report assembly). Anything
  backend-specific (how a spec is authored and verified) is contributed through a small
  protocol. Adding the Foundry backend required *no* changes to the shared steps.
- **Phase-chain immutability.** Each phase produces an immutable object that is the
  constructor input to the next: `Backend → PreparedSystem → Formalizer`. The *existence* of
  a `Formalizer` proves that system analysis and preparation already succeeded — ordering is
  a type-level dependency, not a call-order convention, so there is no half-initialized state.
- **Agents are graphs; everything is checkpointed.** Every agent is a LangGraph state graph
  built from a reusable framework ([graphcore/](graphcore/)). State, conversations, and
  intermediate results are persisted to Postgres so any run can be resumed, time-traveled,
  inspected, or replayed.
- **Tight LLM↔tool loops with hard validation gates.** Agents don't emit free text that is
  hoped to be correct — every CVL write passes the Certora type-checker, every spec is run
  through the actual prover, and a separate "judge" agent adjudicates whether a property was
  legitimately handled. Invalid output is rejected at the tool boundary with actionable
  feedback.
- **Deterministic, LLM-free replay for tests.** A "tape" system records real LLM responses
  per task and replays them, letting the entire pipeline run end-to-end in CI with no API
  calls.

## 3. The big picture

```
                            ┌──────────────────────────────────────────────┐
  CLI / TUI                 │              GENERIC PIPELINE DRIVER           │
  (console_autoprove,       │            composer/pipeline/core.py           │
   tui_autoprove)           │                                                │
        │                   │   1. System Analysis ───────► SourceApplication│
        │  builds context   │   2. backend.prepare_system ─► PreparedSystem  │
        ▼                   │   3. prepare_formalization ∥ property extraction│
  AIComposerContext         │   4. per-component formalize (parallel)        │
  (LLM, RAG, prover opts,   │   5. backend-agnostic Report                   │
   VFS, handler factory)    └───────────────┬────────────────────────────────┘
        │                                    │ PipelineBackend protocol
        │                  ┌─────────────────┼─────────────────┐
        ▼                  ▼                 ▼                 ▼
   ┌─────────┐      Prover backend     Foundry backend   NatSpec workflow
   │ graphcore│     (CVL specs +       (.t.sol tests +   (greenfield stubs
   │ agent fw │      Certora Prover)    forge test)       + CVL)
   └────┬─────┘
        │ each agent = LangGraph state graph + tools
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Tools: filesystem/VFS · CVL author+typecheck · prover · RAG      │
  │         search · knowledge base · human-in-loop · memory          │
  └──────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Persistence (Postgres): rag_db · langgraph_store_db ·            │
  │  langgraph_checkpoint_db · memory_tool_db · audit_db              │
  └──────────────────────────────────────────────────────────────────┘
```

## 4. The generic pipeline driver

[composer/pipeline/core.py](composer/pipeline/core.py) is the spine. `run_pipeline()` executes
five steps and never inspects anything backend-specific:

1. **System analysis** (shared). Runs `run_component_analysis` to produce a
   `SourceApplication` — the contracts, components, external actors, and their interactions.
   Always yields the same type regardless of backend.
2. **`backend.prepare_system(analyzed)`** — the backend's transform. The prover backend lifts
   the source app into a *harnessed* application (generating harness contracts for external
   dependencies); the Foundry backend is an identity transform.
3. **`prepared.prepare_formalization()` runs concurrently with property extraction.** Neither
   depends on the other, so the prover's expensive AutoSetup/summary/invariant work overlaps
   with per-component property inference. Property extraction fans out one agent per component,
   bounded by a semaphore (`--max-concurrent`).
4. **Per-component formalization** (parallel). For each component's properties, the backend's
   `Formalizer.formalize()` is invoked. Results are cached by the backend's result type.
5. **Report assembly** (shared, best-effort). The driver collects per-component verdicts via a
   backend-supplied `fetch_verdicts` callback, an LLM groups properties into semantic clusters,
   and a coverage check validates the result. A failure here never fails the run.

### The backend contract

A backend implements `PipelineBackend[P, FormT, H, A]` plus three phase objects:

| Object | Responsibility | Prover impl | Foundry impl |
|---|---|---|---|
| `PipelineBackend` | Phase-enum map, analysis prompt, artifact store, `prepare_system` | [spec/source/pipeline.py](composer/spec/source/pipeline.py) | [foundry/pipeline.py](composer/foundry/pipeline.py) |
| `PreparedSystem` | Holds the located main contract; builds the `Formalizer` | harness-lifted app + AutoSetup/invariants | identity |
| `Formalizer` | `formalize()` one component; `fetch_verdicts`; `finalize` | `batch_cvl_generation` + prover | `batch_foundry_test_generation` + `forge test` |

The phase enum (`CorePhases`) lets each backend label its own phases while the driver tags the
three universal ones (analysis / extraction / formalization) for the UI and the replay tapes.

## 5. The agent framework (graphcore + workflow)

Every LLM agent in the system is a LangGraph state graph, constructed through a reusable,
type-safe framework.

- **[graphcore/](graphcore/)** is a standalone library (its own package, used by other Certora
  products too). It provides a fluent `Builder[State, Context, Input]` that binds a model,
  state type, tools, system/initial prompt templates, and an optional summarization config,
  then compiles a checkpointed `StateGraph`. The agent loop is the standard
  *LLM → tool calls → tool results → LLM …* cycle, terminating when the model produces a
  final structured output instead of more tool calls.
- **Tools** are Pydantic models with `run()` implementations and mixins for injecting graph
  state / tool-call id. Tools return either a string, a `Command` that merges into state, or an
  `interrupt()` that pauses the graph for human input.
- **[composer/workflow/](composer/workflow/)** wires graphcore to this application: model
  capability parsing ([llm.py](composer/workflow/llm.py) — thinking budgets, interleaved
  thinking, memory-tool beta, cache control), Postgres checkpointer + store services
  ([services.py](composer/workflow/services.py)), and summarization tuned for long spec runs
  ([summarization.py](composer/workflow/summarization.py)).
- **[composer/io/](composer/io/)** is the execution and observability layer. `run_task`
  ([multi_job.py](composer/io/multi_job.py)) wraps each schedulable unit with a handler factory,
  an optional concurrency semaphore, and lifecycle hooks. Graph execution emits an immutable
  event stream (Start / StateUpdate / Checkpoint / CustomUpdate / End) onto a lock-free queue;
  a background drainer dispatches events to the active IO handler (console or TUI). Nested
  sub-agent graphs are tracked transparently so handlers can reconstruct the full call path.

### State, context, and editing

- `AIComposerState` ([core/state.py](composer/core/state.py)) extends LangGraph's message state
  with a virtual filesystem (VFS), validation results, and the working spec being edited.
- `AIComposerContext` ([core/context.py](composer/core/context.py)) is the run-scoped dependency
  container: the bound LLM, RAG connection, prover options, and VFS materializer.
- Spec edits go through `replace_unique` ([core/edit.py](composer/core/edit.py)) — a surgical,
  match-exactly-once text replacement that fails with actionable guidance on ambiguity, keeping
  the model honest about what it's changing and saving tokens.

## 6. The prover (default) backend — phase by phase

Implemented under [composer/spec/source/](composer/spec/source/). The phases map onto the
README's Phase 0–5:

- **System analysis** ([spec/system_analysis.py](composer/spec/system_analysis.py),
  [system_model.py](composer/spec/system_model.py)) — an agent reads the design doc and source
  to produce a `SourceApplication` of contracts → components, each with entry points, state
  variables, interactions, and requirements.
- **Harness setup** ([spec/source/harness.py](composer/spec/source/harness.py)) — a classifier
  agent categorizes external contracts (singleton / multiple / dynamic, ERC20s, interfaces) and
  generates harness contracts, lifting the system to a `HarnessedApplication`.
- **AutoSetup** ([spec/source/autosetup.py](composer/spec/source/autosetup.py)) — shells out to
  the [certora_autosetup/](certora_autosetup/) package to compile the project and produce a
  prover `compilation_config.conf` plus summaries for known externals.
- **Custom summaries** ([spec/source/summarizer.py](composer/spec/source/summarizer.py)) —
  generates CVL summaries for ERC20s and external interfaces.
- **Structural invariants** ([spec/source/struct_invariant.py](composer/spec/source/struct_invariant.py))
  — a two-agent loop: one proposes invariants, a judge accepts/rejects each (not structural /
  not inductive / unlikely to hold / …). Survivors become `certora/specs/invariants.spec`,
  importable by later phases.
- **Per-component property extraction** ([spec/prop_inference.py](composer/spec/prop_inference.py))
  — multi-round agent producing `PropertyFormulation`s (attack vectors, safety properties,
  invariants), optionally refined interactively or against a threat model.
- **CVL generation** ([spec/cvl_generation.py](composer/spec/cvl_generation.py),
  [spec/source/author.py](composer/spec/source/author.py)) — the core feedback loop. The agent
  authors CVL with `put_cvl`/`edit_cvl` (type-checked on every write), runs the prover via a
  `verify_spec` tool, analyzes any counterexamples, and revises. A property-feedback judge
  validates coverage and adjudicates the agent's objections (e.g. "this property is vacuous
  because…"). Output is a `GeneratedCVL` carrying the spec, skipped properties with reasons,
  the property→rule mapping, and the final prover run link.

### Outputs and artifacts

`ArtifactStore` ([spec/artifacts.py](composer/spec/artifacts.py), prover subclass in
[spec/source/](composer/spec/source/)) owns the on-disk layout under the project's `certora/`:
specs in `certora/specs/`, configs in `certora/confs/`, per-spec metadata (properties,
property→rules, commentary) in `certora/properties/`, and a final `certora/ap_report/report.json`.

## 7. Caching & resumption

Two complementary mechanisms:

- **Phase cache** (`--cache-ns`). `WorkflowContext` / `CacheKey`
  ([spec/context.py](composer/spec/context.py)) form a hierarchical, type-parameterized cache
  in the LangGraph store: `None → SourceApplication → Properties → ComponentGroup → CVLGeneration`.
  Each key incorporates a hash of its inputs, so changing the project, doc, or contract
  invalidates exactly the affected subtree. Repeated runs skip completed phases.
- **Checkpointing** (`--thread-id` / `--checkpoint-id`). LangGraph persists graph state after
  every node to `langgraph_checkpoint_db`, enabling crash recovery and "time travel" — resuming
  from any prior checkpoint, even a non-latest one.

## 8. Prover integration

[composer/prover/](composer/prover/) abstracts running the Certora Prover. `run_prover`
([core.py](composer/prover/core.py)) spawns `certoraRun`, streams stdout, and resolves results
through either a **local** results path or a **cloud** path ([cloud.py](composer/prover/cloud.py),
polled via job URL) depending on `ProverOptions.cloud`. Violated rules are fed to a
counterexample analyzer ([analysis.py](composer/prover/analysis.py)) whose findings go back to
the authoring agent. A callback protocol streams per-rule outcomes to the UI and the audit DB.

## 9. Knowledge: RAG + curated KB

- **RAG** ([composer/rag/](composer/rag/)) — the CVL manual, chunked and embedded
  (`nomic-embed-text-v1.5`) into `rag_db` (pgvector, with a ChromaDB fallback). Agents query it
  for CVL syntax/semantics during authoring. Read-only at runtime; rebuilt offline from the docs.
- **Curated KB** ([composer/kb/](composer/kb/)) — ~30 hand-written articles on CVL pitfalls
  (vacuity traps, ghost semantics, summary misapplication…) stored in the LangGraph store and
  searched semantically by symptom. Agents can also contribute new articles (`KBPut`).

## 10. Alternate backends & workflows

- **Foundry backend** ([composer/foundry/](composer/foundry/)) — same driver, but formalizes
  properties as Solidity `.t.sol` tests verified by `forge test` instead of CVL + prover.
  Verdicts come from test pass/expected-failure status. Entry points:
  [cli/console_foundry.py](composer/cli/console_foundry.py), `tui_foundry.py`.
- **NatSpec** ([composer/spec/natspec/](composer/spec/natspec/)) — a *greenfield* workflow
  (its own asyncio orchestrator, not the generic driver) that goes from a design doc to Solidity
  interfaces, stub implementations, and CVL. A semaphore-serialized "semantic registry"
  ([natspec/registry.py](composer/spec/natspec/registry.py)) lets parallel agents share and
  reuse generated state fields without conflicting.

## 11. CLI, TUI & observability

- **Entry points** ([composer/cli/](composer/cli/)) — `console-autoprove` (headless, prints to
  stdout; best for `print`/log debugging) and `tui-autoprove` (Textual UI with per-phase panels
  and live prover-output logs). Both build the context and call the same pipeline; CLI args are
  parsed by introspecting `Annotated` protocol definitions in [composer/input/](composer/input/).
- **TUI** ([composer/ui/](composer/ui/)) — observes the pipeline purely through the IO event
  stream, rendering per-task lanes, tool calls, and streamed prover output.
- **Diagnostics** ([composer/diagnostics/](composer/diagnostics/) + [scripts/](scripts/)) —
  per-phase timing/token aggregation; `snapshot_viewer.py` replays a single agent's conversation
  by mnemonic; `traceDump.py` renders a full run to HTML; `autoprove_cache_explorer.py` inspects
  (and edits) cached phase results and agent memories.

## 12. Testing — deterministic replay tapes

[composer/testing/](composer/testing/) solves end-to-end testing without an LLM. A
`TapeRecorder` captures real `AIMessage` responses **per task, in call order** (including
out-of-graph calls like CEX analysis and interleaved sub-agents). `HarnessFakeLLM` replays
them by routing on the active task id (a `ContextVar` set by `run_task`). Tapes are
human-editable Python modules with embedded JSON; smoke scenarios live in
[test_scenarios/](test_scenarios/). Recording is curated into a clean tape before use (see the
`generate-tape` and `inspect-run` skills).

## 13. Persistence map

All state lives in Postgres (a single `pgvector/pgvector:pg16` container provisions all five):

| Database | Holds |
|---|---|
| `rag_db` | CVL-manual embeddings for RAG search |
| `langgraph_store_db` | LangGraph document/index store — phase cache, KB articles |
| `langgraph_checkpoint_db` | Per-node workflow checkpoints (resume / time-travel) |
| `memory_tool_db` | Hierarchical LLM context memory (per agent) |
| `audit_db` | Run history, VFS snapshots, prover results, summaries (resumption + `traceDump`) |

## 14. Top-level packages at a glance

| Package | Role |
|---|---|
| [composer/](composer/) | The application — pipeline, agents, backends, tools, UI |
| [graphcore/](graphcore/) | Reusable LangGraph agent-building framework (separate package) |
| [certora_autosetup/](certora_autosetup/) | Solidity project analysis, compilation, harness/conf generation (Phase 1) |
| [analyzer/](analyzer/) | Standalone counterexample analyzer (also used inline by the prover backend) |
| [sanity_analyzer/](sanity_analyzer/) | Diagnoses unsatisfiable / sanity-failed prover runs |
| [scripts/](scripts/) | Docker entry, DB/RAG setup, trace & snapshot debugging tools |
| [tests/](tests/), [test_scenarios/](test_scenarios/) | Unit tests and tape-based smoke scenarios |

---

*This document is a high-level map. For runtime/setup details see [README.md](README.md) and
[AICOMPOSER_INFRA.md](AICOMPOSER_INFRA.md); for the canonical contract between the driver and a
backend, read the module docstring and protocols in
[composer/pipeline/core.py](composer/pipeline/core.py).*
