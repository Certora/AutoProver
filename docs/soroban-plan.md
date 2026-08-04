# Plan — Soroban / Sunbeam support in AutoProver

> **Purpose.** A planning document: what it takes for AutoProver to go from a Soroban (Stellar)
> Rust contract to verified CVLR rules run on Certora's Soroban Prover ("Sunbeam"), broken into
> workstreams that can be sized, sequenced, and handed to different people. It is deliberately
> high level — no APIs are specified here beyond what is needed to see where a seam falls.
>
> **Reading order.** [ecosystem-abstraction.md](./ecosystem-abstraction.md) defines the
> ecosystem/backend split this leans on; skim §1–§2 of it first if the two-axis vocabulary
> (ecosystem = front half, backend = back half) isn't familiar. The Solana prototype on
> `eric/crucible-app` is the closest prior art and is inventoried in §6.
>
> **Status:** proposal. Nothing in §5 has started.

---

## 1. Goal

One command takes a Soroban contract repository and produces, without human authoring:

1. a **system model** of the contract (its functions, storage, authorization, invariants),
2. **properties** in natural language, attributed to parts of the contract,
3. **CVLR rules** — Rust functions annotated `#[rule]` — that formalize those properties,
4. the **project scaffolding** Sunbeam needs (Cargo wiring, build command, `certora_build.py`,
   conf files),
5. **verdicts** from real Soroban Prover runs, and
6. the usual AutoProver **report** tying property → rule → verdict → counterexample.

The deliverable to a user is the same shape as today's EVM deliverable: a `certora/`-style
bundle they can run themselves and a report explaining what held and what didn't — with one
important difference, §4.1: part of the deliverable is a **modification to their crate**.

Backend implementation language: **Python**, matching the CVL/prover backend
([composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py)) rather than the Rust
+ PyO3 route Crucible took (§6.2).

---

## 2. What Sunbeam actually is

Everything in this section is what AutoProver must learn to produce. It is worth internalizing
because it differs from CVL in kind, not just in syntax.

**The spec is Rust source inside the contract's own crate.** From the
[tutorial project](https://github.com/Certora/sunbeam-tutorials/tree/main/projects/token):

```rust
// src/certora/spec.rs — a module of the contract crate
use cvlr::asserts::{cvlr_assert, cvlr_assume, cvlr_satisfy};
use cvlr::clog;
use cvlr_soroban_derive::rule;
use crate::Token;                      // the spec calls the contract directly

#[rule]
fn transfer_is_correct(e: Env, to: Address, from: Address, amount: i64) {
    cvlr_assume!(e.storage().persistent().has(&from) && to != from);
    let before = Token::balance(&e, to.clone());
    Token::transfer(&e, from.clone(), to.clone(), amount);
    let after = Token::balance(&e, to.clone());
    clog!(before, after, amount);
    cvlr_assert!(after == before + amount);
}
```

**The crate is wired for it.** `crate-type = ["cdylib"]`, a `certora` feature, git dependencies
on `cvlr` / `cvlr-soroban` / `cvlr-soroban-derive` / `cvlr-soroban-macros`, and a release profile
with `overflow-checks = true`, `panic = "abort"`, `debug = 0`, `lto = true`.

**The build is a wasm build.** `RUSTFLAGS="-C strip=none" cargo build --release
--target=wasm32-unknown-unknown --features certora` (the tutorial wraps this in a `justfile`).
Requires the `wasm32-unknown-unknown` target; `rustfilt` demangles symbols in reports.

**The run is conf-driven.** `certoraSorobanProver some.conf`, where the conf either names a
build script:

```json
{ "build_script": "../certora_build.py",
  "rule": ["init_balance", "transfer_is_correct"],
  "precise_bitwise_ops": true }
```

…or names a pre-built binary directly: `{"files": ["target/wasm32-unknown-unknown/release/x.wasm"],
"process": "emv", "rule": [...]}`. `certora_build.py` is a ~100-line script parameterized by four
globals (`COMMAND`, `PROJECT_DIR`, `SOURCES`, `EXECUTABLES`) that shells out to the build and emits
JSON describing where the wasm and sources are.

**The spec language (CVLR, "cavalier")** gives us: `#[rule]`; `cvlr_assert!` / `cvlr_assume!` /
`cvlr_satisfy!` plus comparison variants (`cvlr_assert_eq!`, `…_lt!`, `…_if`); `clog!` for
counterexample visibility; `nondet()` for symbolic values and as a summarization tool against
solver timeouts (with `Nondet` impls for user types, Soroban ones in `cvlr-soroban`);
`mathint::NativeInt` for overflow-free arithmetic; a `CvlrLog` derive; and ghost variables by
convention (an ordinary `static`/thread-local the spec reads and the contract updates under
`#[cfg(feature = "certora")]`).

**The plumbing is the prover we already drive.** `certora_cli` ships `certoraSorobanProver`, whose
`run_soroban_prover(args)` builds a context against `App.SorobanApp`, calls `build_rust_project`,
and returns the same `CertoraRunResult` (`.link`, `.is_local_link`) that
[composer/prover/certoraRunWrapper.py](../composer/prover/certoraRunWrapper.py) already consumes
from `run_certora`. Cloud submission, job polling
([composer/prover/cloud.py](../composer/prover/cloud.py)), and results parsing
([composer/prover/results.py](../composer/prover/results.py)) are the same infrastructure.

---

## 3. Where it lands in the architecture

The two existing axes cover this cleanly:

```
            ecosystem axis (front half)                  backend axis (back half)
  ┌──────────────────────────────────────┐   ┌────────────────────────────────────────┐
  source ─analyze─▶ SorobanApplication ─extract─▶ properties ─formalize─▶ CVLR rules ─▶ verdicts
        RUST ⊕ soroban                              SorobanProverBackend (Python)
```

- **New ecosystem: `SOROBAN = RUST ⊕ soroban`.** The `RUST` `Language` facet already exists on
  master and is chain-independent by construction
  ([composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py)) — `"soroban"` is already
  a reserved `ChainTag`. We add a `soroban` chain: system model, prompts, validation, main-unit
  locator, unit split. This is WS-1/WS-2.
- **New backend: `SorobanProverBackend`.** A Python `PipelineBackend` implementing the three phase
  objects, modeled directly on `ProverBackend` / `ProverPrepared` / `ProverRunner`. This is
  WS-4/WS-5/WS-6.
- **Shared, near-unchanged:** the driver, caching, the property loop, source tools, the report
  skeleton, the sandbox, prover submission/polling. The one driver change we do want is the
  **preflight** link at the head of the phase chain (WS-0) — everything else the backend
  contributes through the existing phase objects.

Where the analogy to EVM holds, copy it. Where it does not, §4.

---

## 4. The structural differences (where the risk is)

These are the reasons this is not "the CVL backend with different templates," and they should
drive sequencing.

### 4.1 The artifact is a mutation of the target's crate, not a separate file

A CVL spec is a standalone `.spec` file in `certora/specs/`; the contract is untouched. A CVLR
spec is a **module of the contract crate**, compiled with it into one wasm, calling its internals
directly. Consequences:

- We must **write into a working copy** of the user's repository (a new `src/certora/` module,
  edits to `Cargo.toml`, a build command, possibly `#[cfg]` hooks for ghost state) — and never
  into their tree in place. Needs a staging/working-copy policy and a defined deliverable
  (recommendation: a patch/diff plus the generated files, so the change is reviewable).
- The **artifact store** ([composer/spec/source/artifacts.py](../composer/spec/source/artifacts.py))
  gains a second kind of output: files that live at crate-relative paths rather than under
  `certora/`.
- Every component's rules share one crate. **A later component's spec can break an earlier
  one's build.** Crucible hit exactly this and mitigated it by sealing each component's code
  behind its own Cargo feature; we should adopt that from the start *and* add an end-of-run
  full-crate rebuild gate, because per-feature compilation hides cross-component breakage until
  the user builds everything.

### 4.2 The typechecker loop becomes a compile loop

Today: author CVL → `Typechecker.jar` → feedback → revise, with the prover run as a separate
later step. For Soroban the Rust compiler *is* the typechecker, and it is the same build that
produces the verified wasm. So:

- the revise-on-error loop is a **cargo build** loop (the crucible branch's `revise_compile.j2`
  is the direct precedent),
- errors are `rustc` diagnostics — rich, but voluminous; they need shaping before they go to an
  LLM,
- the loop is **expensive and stateful**: a cold wasm build of a real Soroban project is
  minutes, so a warm shared `target/` and a serialization policy across concurrent components
  matter for wall-clock,
- it runs **untrusted `build.rs`/proc-macro code** and therefore must go through the confined
  build path (`rust_build_policy` in [composer/sandbox/recipes.py](../composer/sandbox/recipes.py)),
  with dependency fetching (the `cvlr` git deps!) split from the build as the sandbox design
  requires.

### 4.3 Rule identity and job granularity are ours to choose

The conf's `rule` list must name Rust functions exactly, and one wasm can hold every component's
rules. That opens a decision the CVL backend never had (§8, D3): one prover job per component
with a rule subset, or one job for everything. It also means rule-name uniqueness is a
**crate-global** constraint that per-component authoring must respect.

### 4.4 Vacuity is the dominant failure mode

`cvlr_assume!` is a plain macro over Rust expressions, with no typechecker warning you that two
assumptions contradict — and the docs say it outright: contradicting assumes make every
assertion trivially pass. A green rule therefore carries much less information than a green CVL
rule until sanity checking says otherwise. Rule-sanity/vacuity checking must be **on by default**
and a vacuous rule must be reported as *not verified*, not as a pass. The judge (WS-8) needs to
know this too.

### 4.5 The run must be gated on a build before it starts

Nothing has to build before a CVL run: the prover backend's first act reads the analyzed model.
Here, a great deal must build, and the reasons it might not are numerous and entirely
model-independent — a missing `wasm32-unknown-unknown` target, an unfetchable `cvlr` git
dependency, `soroban-sdk` ↔ `cvlr` version skew, a crate that isn't a `cdylib`, an unexpected
workspace shape, a `certora` feature that doesn't wire up. Every one of them is detectable by a
single build of a **trivial injected rule**:

```rust
#[rule] fn sanity(e: Env) { cvlr_satisfy!(true); }
```

which is Crucible's "gate a skeleton harness through the real toolchain," and which validates the
entire WS-4 setup story before a single property exists. Finding any of it *after* analysis,
extraction, and authoring is the worst available ordering. So the build belongs in a **preflight**
(WS-0): gated ahead of the run, and overlapped with system analysis, which genuinely parallelizes
(analysis is LLM-bound, the build is CPU/network-bound).

Preflight is also the natural carrier for the state §4.1 forces us to establish anyway — the
staged working copy, the resolved contract crate, the toolchain facts, the build command, the conf
skeleton — established once and carried forward rather than recomputed by every authoring turn.

The two alternatives are both worse. Building inside `prepare_system` runs after analysis and
loses the gate entirely. Building in the `run_soroban_pipeline` wrapper before `run_pipeline`
keeps the gate but loses the overlap and sits outside the driver's `TaskInfo` / phase /
cancellation reporting — minutes of dead air with no phase to show for it.

---

## 5. Workstreams

Sizes are T-shirts relative to each other, not calendar estimates. "Depends on" is hard
ordering; anything else can run in parallel.

### WS-0 — Lift the `preflight` seam to master · **S** · depends on: nothing

A fourth link at the head of the driver's phase chain —
`Backend ──preflight──▶ Pre ──prepare_system──▶ …` — with two properties: it runs *concurrently
with system analysis* (it reads nothing analysis produces), and it is a **gate** — awaited first,
so a failure cancels the analysis agent racing it instead of letting the run spend on a model it
can no longer use. `Pre` is opaque to the driver and carried forward to `prepare_system`.

This exists on `eric/crucible-app` (commit `3e70413`, "pipeline: the preflight gates") and is the
piece of that branch the Soroban backend most wants, for reasons §4.5 spells out.

- **Deliverables:** the `Pre` type parameter, `preflight()` on `PipelineBackend`, and the
  gate-and-cancel ordering in [composer/pipeline/core.py](../composer/pipeline/core.py) — roughly
  90 lines plus the doc section. `PipelineBackend` is a `Protocol` on master, so the prover,
  foundry, and null-Solana backends each grow a trivial `async def preflight(...) -> None`.
- **Lift the seam, not the machinery:** leave `PreflightSpec`, `AuthorKind="preflight"`, and the
  wheel-declared preflight in `rustapp/descriptor.py` / `wire.py` where they are — that is
  plumbing for a backend *authored in Rust*, which this one is not (§6.2).
- **Sequencing:** its own PR, and an independent one. Nothing in M1 needs it (the null backend
  builds nothing); WS-3/WS-4 need it at M2. So it can land early and blocks nothing.

### WS-1 — Soroban system model · **L** · depends on: nothing

The `SorobanApplication` pydantic model and its instance wrappers, the peer of
[`SolanaApplication`](../composer/spec/solana/model.py). This is the single largest front-half
deliverable and the one everything else's prompts are written against, so it should start first
and be reviewed by someone who knows Soroban.

Domain shape to capture: contracts (`#[contract]` / `#[contractimpl]`) and their public
functions; the three storage kinds (persistent / temporary / instance) with TTL/archival
semantics; `Address` and `require_auth`-based authorization (contrast: Solana's passed-in
accounts and signers, EVM's `msg.sender`); cross-contract invocation via clients; events;
`Env`; custom error enums and panics. Plus the `FeatureUnit` component split.

- **Deliverables:** `composer/spec/soroban/model.py`; validation function
  (`_soroban_validate`, mirroring `_solana_validate`'s structure and its
  component↔function totality rule); unit tests.
- **Decision it forces:** how faithful to make the storage/TTL model in v1 (§8 D1).

### WS-2 — The `soroban` ecosystem + prompts · **M** · depends on: WS-1

Bind WS-1 into an `Ecosystem`: analysis prompt pair, property prompt pair, `locate_main`,
`units`, `unit_type`, `analysis_extra_input`, `supports_greenfield=False`; register it in
`ECOSYSTEMS` / `Ecosystems`.

- **Deliverables:** `SOROBAN` in [ecosystem.py](../composer/pipeline/ecosystem.py);
  `composer/templates/soroban/*.j2` including a `soroban/_vulnerability_patterns.j2` composed
  with the existing shared `rust/_vulnerability_patterns.j2`; a null-backend gate test mirroring
  `test_solana_gate` (analysis + extraction with no verifier).
- **Note:** the shared Rust language facet already exists — reuse, don't fork. Soroban-specific
  failure modes to enumerate: missing `require_auth`, wrong storage kind (temporary where
  persistent is needed), TTL/archival bugs, `i128` overflow, panic-on-arithmetic,
  unwrap on absent storage, auth-context confusion in cross-contract calls.
- **Milestone value:** this alone gives a demoable "AutoProver reads a Soroban contract and
  proposes properties" with no prover work at all.

### WS-3 — Build + toolchain capability · **L** · depends on: nothing (WS-0 to wire it in)

The `source → wasm` capability, the peer of `composer/spec/solana/build.py` on the crucible
branch and reusing its crate-resolution helper (`composer/spec/cargo.py`).

- **Deliverables:** crate/manifest resolution for Soroban layouts (workspace vs single crate,
  which crate is the contract); a build function producing the release wasm under the confined
  policy; toolchain provisioning (`wasm32-unknown-unknown` target, pinned toolchain, `rustfilt`)
  that checks and explains rather than failing opaquely; the split fetch/build step for the `cvlr`
  git deps; build-error capture shaped for LLM consumption.
- **Where it runs:** this capability is backend-agnostic, but its first caller is the backend's
  `preflight` (WS-0), which builds the injected sanity rule and gates the run on it (§4.5). The
  authoring loop (WS-5) then calls the same capability per revision, warm.
- **Risks:** network access for git deps inside a confined build; `soroban-sdk` version ↔ `cvlr`
  version compatibility (we do not control either release cadence); build wall-clock.
- **Gate:** a fixture Soroban contract builds to wasm in CI, sandboxed, offline after fetch.

### WS-4 — Project setup ("Soroban autosetup") · **M** · depends on: WS-3

Everything needed to turn a plain Soroban repo into a Sunbeam-runnable project — the peer of
what `certora_autosetup` does for EVM, but far smaller and mostly deterministic.

- **Deliverables:** inject the `cvlr*` dependencies and the `certora` feature into `Cargo.toml`;
  ensure the release profile Sunbeam needs; create the `src/certora/` module and hook it into
  `lib.rs` under `#[cfg(feature = "certora")]`; generate `certora_build.py` (parameterized) and
  the per-run conf files; verify by building.
- **Recommendation:** template these deterministically, with an LLM fallback only for the
  crate-shape cases templates can't handle (unusual workspaces, existing `certora` module, no
  `cdylib`). Every mutation must be idempotent and reversible.
- **Where it runs:** in `preflight` (WS-0), ahead of and concurrent with system analysis — none of
  it reads the analyzed model, and the sanity-rule build that proves it worked is the run's gate
  (§4.5). What preflight establishes (working copy, contract crate, toolchain facts, build command,
  conf skeleton) is what it hands forward as `Pre`.
- **Open:** whether to hand the prover a `build_script` or a pre-built `files:` wasm (§8 D2); and
  how far setup should go to *repair* a project before failing the run (§8 D8).

### WS-5 — Rule authoring loop · **L** · depends on: WS-2, WS-3, WS-4

The formalizer: properties → CVLR rules that compile.

- **Deliverables:** author agent + prompts (system prompt, CVLR cheat sheet, per-component
  authoring prompt, `revise_compile` prompt); the compile→feedback→revise loop with a round
  budget; per-component Cargo feature sealing (§4.1); rule-name allocation that is unique
  crate-wide; a `GeneratedCVLRSpec` pydantic result type (must be cacheable, like `GeneratedCVL`);
  the "declined" path (`GaveUp`) for properties not expressible in CVLR.
- **Reuse:** the whole shape of [composer/spec/source/author.py](../composer/spec/source/author.py)
  and `batch_cvl_generation`, and the crucible branch's authoring templates as a model for
  Rust-authoring prompts.
- **Gate:** a recorded fake-LLM tape (`/generate-tape`) that replays authoring end to end with no
  live LLM.

### WS-6 — Prover interface + verdicts · **M** · depends on: WS-4

- **Deliverables:** generalize
  [certoraRunWrapper.py](../composer/prover/certoraRunWrapper.py) /
  [certora_env.py](../composer/certora_env.py) to dispatch `run_soroban_prover` alongside
  `run_certora` (the return type is already shared); a Soroban prover tool + options
  (rule-sanity on by default, timeouts, `precise_bitwise_ops`); verdict fetching mirroring
  [report_prover.py](../composer/spec/source/report_prover.py).
- **Verify early, cheaply:** that `results.py`'s `treeViewStatus` parsing and `cloud.py`'s job
  polling work unchanged on a real Soroban job. A hand-run of the tutorial project against the
  cloud answers this in an afternoon and de-risks the whole workstream. **Do this first.**
- **Also:** widen `ReportBackend` (`Literal["prover","foundry"]` in
  [report/schema.py](../composer/spec/source/report/schema.py)) and give the new tag its wording
  in `report/render.py`.

### WS-7 — CVLR RAG corpus · **M** · depends on: nothing

The peer of the CVL manual corpus, so authoring agents can look things up rather than
hallucinate macros.

- **Sources:** the Sunbeam docs, the Sunbeam tutorials, the `cvlr` and `cvlr-soroban` repos
  (notably `cvlr-soroban/src/nondet.rs`), `soroban-sdk` docs, and our own curated recipes.
- **Deliverables:** a corpus producer + import, a `soroban_kb` DB role/connection, a search tool
  in `composer/tools/`, a populate script, and a retrieval-quality test.
- **Strong recommendation:** build this on the crucible branch's **RAG import JSON format**
  (`docs/rag-import-format.md`, `composer/rag/import_format.py`,
  `composer/scripts/rag_import.py`) rather than writing a fourth bespoke ragbuilder. That work
  exists precisely so a new corpus is "parse my docs → emit JSON," and lifting it to master is a
  small, independently valuable PR.
- **Note:** curated recipes matter more here than raw docs. The Sunbeam documentation is thin;
  the tutorials and real verified projects (Blend, reflector-subscription-contract) carry most of
  the operational knowledge.

### WS-8 — Judge + expressibility guidance · **M** · depends on: WS-5

Two distinct prompt surfaces, often conflated:

1. **`backend_guidance`** (property-extraction time) — what CVLR can express, so extraction
   doesn't propose properties the formalizer must decline. The CVLR analog of
   `CERTORA_BACKEND_GUIDANCE`.
2. **The judge** (authoring time) — does this rule actually capture the property? Reuses
   `property_feedback_judge` ([composer/spec/feedback.py](../composer/spec/feedback.py)) with
   Soroban/CVLR-specific criteria.

Judge criteria unique to this backend: **vacuity** (does the assume set contradict? are there
`cvlr_satisfy!` reachability checks?); `clog!` coverage of every `nondet()` value feeding an
assertion; over-summarization (a `nondet` summary that erased the very behavior under test);
`mathint` vs wrapping arithmetic; storage-kind fidelity; whether `require_auth` was assumed away.

### WS-9 — Counterexample handling · **M** · depends on: WS-6

Soroban counterexamples are Rust/wasm call traces with `clog!` values, not CVL call traces. The
existing cex analyzer and remediation prompts (`composer/templates/cex_*.j2`,
[cex_remediation.py](../composer/spec/cex_remediation.py), `analyzer/`) are Solidity/CVL-shaped
and need Rust equivalents. Also needed: the timeout/relaxation story — the CVLR answer to a
solver timeout is `nondet` summarization, which is the analog of
[composer/tools/relaxation.py](../composer/tools/relaxation.py).

### WS-10 — Test fixtures, tapes, and gates · **M** · depends on: WS-2 (grows with each WS)

- A small Soroban contract under `test_scenarios/` with a `system.md`, modeled on
  `test_scenarios/solana_vault/` from the crucible branch — ideally one with a known real bug so
  a counterexample path is exercised.
- Fake-LLM tapes for each phase (front half; authoring; end to end).
- A cheap gate per workstream in the `not expensive` lane, plus one `expensive`-marked
  live-prover end-to-end test.
- Cross-component rebuild gate (§4.1).

### WS-11 — Environment, CI, packaging · **S** · depends on: WS-3

Rust toolchain + wasm target in the Docker images and CI; `uv` groups/extras for anything new;
a CLI entry point (`tui-soroban` / `console-soroban`) alongside the existing ones; pyright clean.

### WS-12 — Docs · **S** · depends on: the above

Update [ecosystem-abstraction.md](./ecosystem-abstraction.md) (a third ecosystem; `soroban` no
longer "reserved") and [ARCHITECTURE.md](../ARCHITECTURE.md); write an operator-facing
"running AutoProver on a Soroban project" guide.

---

## 6. What to reuse from `eric/crucible-app` — and what not to

### 6.1 Reuse (lift to master as its own PR where it isn't there yet)

| Thing | Where | Why it matters here |
|---|---|---|
| The `preflight` phase link — gated, overlapped with analysis, carries `Pre` forward | branch commit `3e70413`, `composer/pipeline/core.py` | WS-0; §4.5 is the whole argument. Take the seam only, not `PreflightSpec`/`wire.py` |
| `RUST` language facet, `rust/_vulnerability_patterns.j2` | already on master | WS-2 gets its language half free |
| Confined Rust build policy, private `CARGO_HOME`, split fetch/build | already on master (`composer/sandbox/recipes.py`, `docs/command-sandbox.md`) | WS-3's security story is solved |
| Crate/manifest resolution | branch: `composer/spec/cargo.py` | WS-3 |
| Build capability shape (`source → binary`, backend-agnostic, one `run_local_command` choke point) | branch: `composer/spec/solana/build.py` | WS-3's design is already argued through |
| RAG import JSON format + shared importer | branch: `docs/rag-import-format.md`, `composer/rag/import_format.py`, `composer/scripts/rag_import.py` | WS-7 |
| Rust-authoring prompt/template patterns (cheat sheets, `revise_compile`, judge guidance) | branch: `rust/crucible-app/templates/*.j2` | WS-5, WS-8 |
| Per-component feature sealing; "later component breaks earlier one" lesson | branch commit "seal each component's tests behind its own feature" | §4.1 |
| Test-scenario layout and gate style | branch: `test_scenarios/solana_vault/`, `tests/test_crucible_*` | WS-10 |
| Toolchain-version handling | branch: `docs/crucible-toolchain-versioning.md`, `composer/rustapp/toolchain.py` | WS-3 |

### 6.2 Do not reuse

The Rust application framework itself — `composer/rustapp/*`, `rust/autoprover-sdk`,
`rust/crucible-app`, the PyO3 wire/descriptor/host layers, the IoC loop. That machinery exists to
let a backend be *written in Rust*; the Soroban backend is Python, so it would be pure overhead.
Read `docs/rust-formalization-backends.md` and `docs/formalization-abstraction.md` on the branch
for the reasoning, then take the design lessons, not the code.

One caveat worth deciding early (§8 D5): the analyzed *contract* is Rust either way, so anything
Crucible learned about **building Rust** is reusable, while everything it learned about **hosting
a Rust backend** is not. Keep that line crisp when lifting files.

---

## 7. Milestones

Each milestone is demoable on its own, which is what makes them useful for splitting work.

| | Milestone | Contains | Proves |
|---|---|---|---|
| **M0** | Spike (days, one person) | Hand-run the tutorial project through `certoraSorobanProver`; drive it via `run_soroban_prover` from our code; parse the results | The back-half plumbing is as reusable as it looks. Kills the biggest unknown before anyone builds on it |
| **M1** | Front half | WS-1, WS-2, WS-10 (front-half gate) | AutoProver models a Soroban contract and proposes properties, verified against a null backend |
| **M2** | Build + setup | WS-0, WS-3, WS-4, WS-11 | We can take an arbitrary Soroban repo and make it Sunbeam-runnable, reproducibly and sandboxed — gated by a sanity-rule build before the run spends anything |
| **M3** | First verified rule | WS-5, WS-6, WS-7 | End-to-end: property → CVLR rule → compiles → prover run → verdict |
| **M4** | Production quality | WS-8, WS-9, remaining WS-10, WS-12 | Vacuity-aware verdicts, counterexample triage, timeout handling, docs |

M0 gates everything. M1 and M2 are independent of each other and can run in parallel by
different people; M3 needs both. WS-0 is the one item that touches shared driver code, so it
wants to land as its own early PR rather than riding along with M2's Soroban-specific work.

---

## 8. Decisions needed

- **D1 — Storage/TTL fidelity in the system model (WS-1).** Soroban's persistent/temporary/
  instance storage with TTL and archival is genuinely different from both EVM storage and Solana
  accounts. Model it fully in v1, or start with "keyed storage + a kind tag" and extend? Affects
  which properties are even statable.
- **D2 — `build_script` vs pre-built `files:` (WS-4/WS-6).** Handing the prover a wasm we built
  ourselves keeps the build inside our sandbox and our error handling. Handing it a build script
  is the documented path and puts source in the report. Possibly: build ourselves for the
  authoring loop, ship a `build_script` conf as the user-facing deliverable.
- **D3 — Prover job granularity (§4.3).** One job per component (parallel, attributable
  timeouts, more jobs) vs one job for all rules (cheaper, coarser). EVM precedent is per
  component; keep it unless cost says otherwise.
- **D4 — What we hand the user (§4.1).** A patch against their repo? A fork/branch? A generated
  directory plus instructions? This is a product decision with a code consequence and should be
  settled before WS-4 finishes.
- **D5 — How much of the crucible Rust-build layer to lift to master, and when.** A focused
  "lift `cargo.py` + the Rust build capability + RAG import format to master" PR would unblock
  WS-3 and WS-7 cleanly; the alternative is re-implementing them inside the Soroban work.
- **D6 — Ghost variables.** Whether v1 authors them at all. They require `#[cfg]`-gated edits to
  contract code beyond a spec module, which raises the stakes on D4.
- **D7 — Multi-contract Soroban systems.** Single main contract in v1 (see §10) — confirm that's
  acceptable for the target design partners.
- **D8 — Preflight failure: repair or fail? (WS-0/WS-4).** On the crucible branch a preflight
  failure cancels the analysis racing it and raises. But several Soroban preflight failures are
  exactly what WS-4 exists to fix — no `cdylib`, no `certora` feature, an incompatible `cvlr` pin —
  so hard-failing would reject projects we could have set up, while repairing inside preflight
  makes the gate slower and its failure semantics fuzzier. Where the boundary falls between
  "repair and retry" and "fail with an explanation" is **open**, and it needs a call before WS-4
  finishes. Note it interacts with D4: repair means more mutation of the user's crate.

---

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `cvlr` / `cvlr-soroban` are git-dependency crates outside our release control; `soroban-sdk` version skew | Builds break for reasons unrelated to us | Pin known-good combinations; catch it in the preflight gate (§4.5) before the run spends, and fail with an actionable message rather than a raw cargo error |
| Cold wasm builds are minutes, and the authoring loop rebuilds | Wall-clock and cost blow up | Warm shared `target/`; per-component feature sealing so a rebuild is incremental; cap revise rounds |
| Vacuous rules reported as passes (§4.4) | We deliver false assurance — the worst possible failure | Rule sanity on by default; vacuity is a *not verified* verdict; judge checks the assume set |
| Sunbeam docs are thin; most real knowledge is in tutorials and verified projects | Authoring agent hallucinates macros/APIs | WS-7 curated corpus; compile loop catches the rest |
| Mutating a user's crate | Trust, and hard-to-debug interference with their build | Never edit in place; work on a copy; idempotent, reversible, reviewable output (D4) |
| Cross-component build breakage found only at the end (§4.1) | Late, confusing failures | Feature sealing + an end-of-run full rebuild gate, in from the start |
| Solver timeouts on realistic contracts | Rules neither pass nor fail | WS-9's `nondet` summarization loop; set expectations that timeouts are a normal outcome |

---

## 10. Non-goals for v1

Multi-contract Soroban systems (cross-contract verification); greenfield mode (`supports_greenfield
= False`); mutation testing; Sui or any further chain (though a clean `RUST` facet is the point of
doing this well); authoring ghost variables if D6 says no; local (non-cloud) prover runs beyond
whatever falls out for free.

---

## 11. References

**Sunbeam / CVLR**
- [Sunbeam docs](https://docs.certora.com/en/latest/docs/sunbeam/index.html) ·
  [installation](https://docs.certora.com/en/latest/docs/sunbeam/installation.html) ·
  [user guide](https://docs.certora.com/en/latest/docs/sunbeam/usage.html)
- [CVLR spec language](https://docs.certora.com/en/latest/docs/solana/speclanguage.html) ·
  [rule sanity](https://docs.certora.com/en/latest/docs/solana/sanity.html)
- [Sunbeam tutorials](https://certora-sunbeam-tutorials.readthedocs-hosted.com/en/latest/) ·
  [tutorial source](https://github.com/Certora/sunbeam-tutorials)
- [`cvlr`](https://github.com/Certora/cvlr) · [`cvlr-soroban`](https://github.com/Certora/cvlr-soroban)

**In this repo**
- [ecosystem-abstraction.md](./ecosystem-abstraction.md) — the front-half seam
- [command-sandbox.md](./command-sandbox.md) — the confined-build mechanism WS-3 must use
- [composer/spec/source/pipeline.py](../composer/spec/source/pipeline.py) — the backend to model
  `SorobanProverBackend` on
- On `eric/crucible-app`: `docs/formalization-abstraction.md`, `docs/rust-applications.md`,
  `docs/rag-import-format.md`, `docs/crucible-toolchain-versioning.md`,
  `docs/crucible-component-units.md`
