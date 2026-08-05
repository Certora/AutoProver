# Crucible → Foundry parity: gap analysis

A feature-by-feature comparison of the **Crucible** (Solana fuzzing) backend against
the mature **Foundry** (EVM) backend, to identify the work remaining to bring Crucible
to parity. Scope is strictly *Foundry* parity — several capabilities exist only on the
**autoprove/prover** path and are absent from *both* Foundry and Crucible; those are
called out as "parity (neither has it)" so they are not mistaken for Crucible gaps.

## TL;DR

Crucible is functionally end-to-end and shares the report schema, grouping, caching,
console frontend, precondition checks, and per-component deliverables with Foundry. The
**real Foundry-parity gaps are two, plus two smaller ones**:

1. **No TUI + no live progress telemetry** (Foundry has `tui-foundry` and streams
   `forge_test_run` summaries; Crucible has no `tui-crucible`, and its declared event
   kinds are never emitted). — *largest gap*
2. **No design-doc auto-discovery** (Foundry accepts an optional `system_doc` and
   discovers one; Crucible requires it).
3. Per-component **status artifact** (Foundry writes `*.status.json`; Crucible doesn't).
4. **Build concurrency** (Foundry runs forge processes in parallel; Crucible serializes
   on one shared harness crate).

Everything else is either at parity or is a Crucible-specific rough edge unrelated to
Foundry (dead tuning flags, hardcoded toolchain versions) — see §3.

---

## Parity scorecard

| Capability | Foundry | Crucible | Status |
|---|---|---|---|
| Console entry point | `console-foundry` | `console-crucible` | ✅ parity |
| **TUI entry point** | `tui-foundry` (`FoundryApp`) | `tui-crucible` (`GenericRustApp`) | ✅ parity — *done (2c0f693)* |
| **Progress telemetry** | emits `forge_test_run` summaries | emits `fuzz_pulse`/`fuzz_finding`/`build_output` (post-hoc, per run) | ✅ parity — *done (2c0f693)* |
| **Design-doc auto-discovery** | `system_doc` optional → discovery phase | `system_doc` optional → discovery phase | ✅ parity — *done (55a5959)* |
| Per-component status artifact | `*.status.json` | commentary + property→tests only | ⚠️ minor gap |
| Build/verify concurrency | `--max-forge-runners` parallel | serialized (`Semaphore(1)`, shared crate) | ⚠️ perf gap |
| Shared `report.json` + backend labels | ✅ | ✅ (`crucible` labels wired) | ✅ parity |
| Verdict model (GOOD/BAD only) | GOOD/BAD | GOOD/BAD | ✅ parity |
| Per-component deliverables | commentary/properties/property-tests | commentary/properties/property-tests (+crate) | ✅ parity |
| Upfront precondition validation | lazy (foundry.toml at first run) | eager (`validate_preconditions`: bins + Cargo.toml) | ✅ Crucible ahead |
| cache-ns / memory-ns / result cache | ✅ | ✅ | ✅ parity |
| RAG env | Foundry cheatcode DB | `crucible_kb` DB (optional) | ✅ parity (different DBs) |
| ap-trail / run index (run_id) | ✅ | ✅ | ✅ parity |
| Test coverage | 1 arg-parser test | 7 gates + unit tests | ✅ Crucible ahead |
| `--interactive` (HITL refinement) | flag forwarded; handler **raises NotImplementedError** | flag forwarded; handler **raises NotImplementedError** | ➖ parity (neither services it; autoprove-only) |
| `threat_model` | plumbed but forced `None`, no flag | hardcoded `None`, no flag | ➖ parity (neither; autoprove-only) |
| `write_job_info` / token-usage ledger | not called (`at_exit=None`) | not called (no `at_exit`) | ➖ parity (autoprove-only) |
| Auto HTML report | no (manual `autoprove-report-render`) | no (same manual CLI) | ➖ parity |
| `finalize` run-level artifact | not overridden (no-op) | not implemented (no-op) | ➖ parity |

Legend: ❌ real gap · ⚠️ minor/perf gap · ✅ parity · ➖ parity because *neither* has it (a
capability that exists only on autoprove/prover).

---

## 1. Real Foundry-parity gaps (the work)

### 1.1 TUI frontend + progress telemetry — **DONE (commit 2c0f693)**

> **Resolved.** The decider now issues `Command::Emit` from the setup and per-component
> sessions (`fuzz_pulse` before each fuzz run + on a clean hold, `fuzz_finding` on a
> counterexample, `build_output` on (re)compiles / dry-run outcomes); `RealEffects.emit`
> was fixed to route out-of-graph via `push_custom_update` (it previously dropped every
> event through a stale `get_stream_writer()` call); and `tui-crucible` is wired
> (`GenericRustApp`). Events are **post-hoc per run** (matching Foundry's
> `forge_test_run` summaries) — true intra-run live streaming would need the
> `RunCommand` effect to stream subprocess output and is deferred. Original analysis
> retained below for context.

### ~~1.1 TUI frontend + live progress telemetry — largest gap, L~~ (superseded)

Foundry ships a Textual TUI (`tui-foundry` → `composer/cli/tui_foundry.py`, `FoundryApp`
in `composer/foundry/foundry_app.py:114`) that streams each `forge test` run's summary
into a per-task collapsible panel (`ForgeTestRunEvent`, `composer/foundry/runner.py:73-79`;
rendered `foundry_app.py:79-97`). Crucible has:

- **No `tui-crucible` script** (`pyproject.toml` has only `console-crucible`).
- **No emitted events.** `rust/crucible-app/src/lib.rs` declares three event kinds
  (`fuzz_pulse`, `fuzz_finding`, `build_output`, `lib.rs:666-670`) but contains **no
  `Command::Emit`** anywhere — the sessions only issue `CallLlm`/`RunCommand`/`Publish`/
  `GiveUp`. So there is no live fuzzing pulse, no streamed findings, no build output
  panel. Crash detection is a post-hoc string match on `[FUZZ_FINDING]` in captured
  stdout (`lib.rs:598`), never surfaced live.

The generic machinery exists and is reachable (`GenericRustApp` in
`composer/rustapp/frontend.py:77`; a generic `tui_main` in `composer/rustapp/cli.py:92`),
so this is two pieces of work:
- **(a) Wire a `tui-crucible` entry** that builds the app with the crucible env
  (`build_crucible_env`) + `run_crucible_pipeline` (mirror how `console_crucible`
  injects `run_pipeline_fn`). Small once (b) exists.
- **(b) Emit events from the Rust decider** (`Command::Emit` in the setup/per-component
  sessions of `lib.rs`) for build output and fuzzing progress/findings, so the TUI (and
  console) have something to show. This is the substantive part.

Note: HITL/interactive refinement is *not* part of this gap — Foundry's own handler
raises `NotImplementedError` for HITL (`foundry_app.py:74`), exactly like the Rust
frontend (`frontend.py:58`). Neither backend services interactive refinement.

### 1.2 Design-doc auto-discovery — **DONE (commit 55a5959)**

> **Resolved.** `system_doc` is now optional in the generic rust entry; when omitted,
> `resolve_design_doc` runs as a visible "Design Doc Discovery" task (a dedicated
> UI-only descriptor phase), and the doc-dependent construction moved into the runner
> so a discovered and a supplied doc share the same cache key. Original analysis below.

### ~~1.2 Design-doc auto-discovery — M~~ (superseded)

Foundry (and autoprove) make `system_doc` optional and, when omitted, run a "Design Doc
Discovery" task via the shared `cli_pipeline` (`composer/pipeline/cli.py:194-218`,
`resolve_design_doc` in `composer/spec/source/design_doc_finder.py`). Crucible goes
through the generic Rust entry (`composer/rustapp/entry.py`), where `system_doc` is a
**required positional** (`entry.py:149`) and `design_doc_finder` is never imported. So a
Crucible run must always be handed an explicit design doc.

Work: integrate `resolve_design_doc` into `rust_entry_point` (make `system_doc`
`nargs="?"` and run discovery when absent), reusing `design_doc_finder`. The
`DesignDocChosenEvent` UI is already consumed by autoprove/foundry frontends and could
be surfaced once the TUI (1.1) exists.

### 1.3 Per-component status artifact — **S**

Foundry writes a `*.status.json` per component (`ComponentTestStatus`:
pass/expected-failure/skip, `composer/foundry/artifacts.py:100-126`). Crucible writes
`commentary.md` + the property→tests map (`composer/crucible/store.py:113-117`) but no
equivalent status file. The baked verdicts land in `report.json`, so this is largely
redundant — decide whether Crucible needs a parallel status artifact for tooling that
reads per-component status off disk.

### 1.4 Build/verify concurrency — **M (design)**

Foundry runs multiple `forge test` processes concurrently, gated by `--max-forge-runners`
(`composer/foundry/pipeline.py:192-203`). Crucible **serializes** all harness builds/fuzz
runs via a single `command_sem = Semaphore(1)` (`composer/crucible/backend.py`) because
every component shares one harness crate (`fuzz/<program>/`). This is a throughput gap on
multi-invariant programs.

**Investigated; deferred (see docs/crucible-unit-granularity.md §7).** The obvious
"crate-per-component" fix does not work: separate `target/` dirs recompile the heavy deps
N× (a regression), and a shared `CARGO_TARGET_DIR` collides on Crucible's hardcoded
`invariant_test` binary name. The only clean path is `crucible run --binary-in` (build
serially into one crate, fuzz binaries in parallel) — a non-trivial formalize-phase
redesign whose payoff only materializes at production fuzz budgets (60–300 s), so it's
deferred until then.

---

## 2. Already at parity (including limitations Foundry shares)

These are **not** Crucible gaps — Foundry lacks them too (they exist only on the
autoprove/prover path). Listed so they aren't mistaken for work:

- **Interactive HITL refinement** — both backends forward `--interactive` to the driver
  but their handlers raise `NotImplementedError` (`foundry_app.py:74`, `frontend.py:58`).
- **Threat model** — Foundry forces `threat_model=None` (`foundry/entry.py:103`); Crucible
  hardcodes `None` (`rustapp/host.py:207`). Only autoprove exposes `--threat-model`.
- **`write_job_info` / token- & prover-usage ledger** — called only by autoprove
  (`autoprove_common.py:109`); Foundry passes `at_exit=None`, Crucible has no `at_exit`.
  Both only `print(RunSummary.format())`.
- **Auto HTML report** — neither renders HTML in-pipeline; both rely on the shared
  `autoprove-report-render` CLI over `report.json`.
- **`finalize` run-level artifact / `extra_report_inputs`** — neither overrides these
  (both no-op); only the prover uses them.
- **Verdict granularity** — both emit only GOOD/BAD (never ERROR/TIMEOUT). For a
  coverage-guided fuzzer, "ran to the fuzzing budget with no counterexample ⇒ GOOD"
  (`lib.rs:604`) is the intended semantics, not a misclassification; a build failure
  becomes a `GiveUp`/failure rather than a verdict, matching Foundry's compile-failure
  handling.

---

## 3. Crucible-specific rough edges (remaining work beyond Foundry parity)

Independent of Foundry, these are worth closing for a production-quality backend:

- ~~**Inert tuning flags.**~~ **DONE (266f421)** — `--fuzz-cores`, `--stateful`, and
  `--crucible-version` were parsed but never threaded to `crucible run`; removed from the
  descriptor (only `--fuzz-timeout` remains, which is honored). Re-add each with its wiring
  if/when the fuzz command grows the corresponding knob.
- **Hardcoded toolchain versions.** anchor 1.0.1 / solana 3.0 / libafl 0.15.1 are pinned
  in `composer/crucible/harness.py:32-43` ("Hardcoded for now"); `--crucible-version` was
  meant to drive a version table (see docs/crucible-toolchain-versioning.md) but is inert.
- **No persisted usage ledger.** `ArtifactStore.write_token_usage` exists
  (`artifacts.py:112`) but has **zero callers**; a Crucible run persists no token/cost
  record (Foundry is the same, but this matters for demo/observability).
- **Provisional deliverable layout.** The descriptor `artifact_layout` is marked
  "provisional" (`lib.rs:671`) and is superseded by the hand-written `CrucibleArtifactStore`.
- **Verdict metadata.** `line` / `duration_seconds` are always `None` on Crucible verdicts
  (`lib.rs:316`), so the report's timing/line columns are blank.

---

## 4. Where Crucible already exceeds Foundry

- **Eager precondition validation** — `validate_preconditions` checks required binaries
  (`crucible`, `cargo-build-sbf`, `anchor`) and `Cargo.toml` up front (`lib.rs:685-721`);
  Foundry only discovers a bad `foundry.toml` lazily at first `forge test`.
- **Test coverage** — 7 gates (build/dry-run, setup, formalize, e2e, sandbox, solana) plus
  harness unit tests, vs Foundry's single arg-parser regression test.
- **Command sandboxing** — every external command is confined (Landlock+seccomp),
  fail-closed; Foundry runs `forge` unconfined.

---

## 5. Suggested prioritization

1. ~~**1.1 emit decider events + `tui-crucible`**~~ — **DONE (2c0f693)**.
2. ~~**1.2 design-doc discovery**~~ — **DONE (55a5959)**.
3. ~~**§3 inert flags**~~ — **DONE (266f421)**, removed. (Version-table work remains,
   tracked in docs/crucible-toolchain-versioning.md.)
4. **1.4 crate-per-component** (concurrency) and **1.3 status artifact** — lower priority;
   1.4 is a larger design change tracked separately.

Not recommended as "parity" work (they'd be new capability for *both* backends, better
scoped as cross-backend features): interactive HITL, threat-model input, usage ledger,
auto-HTML.
