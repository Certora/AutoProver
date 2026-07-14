# Design Doc — Defining an AutoProver application *purely* in Rust

> Today a Rust *backend* is a passive wheel ([rust-backend-api.md](./rust-backend-api.md)) and
> a generic Python host ([rust-applications.md](./rust-applications.md)) turns any wheel into a
> runnable application — phase enum, argparse, entry point, frontend, store, `main()`. That
> works end-to-end for a *simple* app: `echoprover` ships as `console_main("echoprover")` with
> **zero bespoke Python**.
>
> Crucible does not. It carries a whole `composer/crucible/` package that *forks* the generic
> host in ~10 specific places (a crate-shaped deliverable, a shared setup fixture, dependency
> warming, an `.so` pre-build, a RAG env, sandbox grants, a verdict summary). This doc
> inventories every one of those forks and proposes a Rust-side seam for each, so that Crucible
> becomes what echoprover already is: a wheel + a descriptor, launched by the generic host with
> no application-specific Python.
>
> Companion to [rust-applications.md](./rust-applications.md) (§4.5 already anticipated "Python
> shell, Rust formatter" for the store — this doc discharges that and the rest) and
> [rust-backend-api.md](./rust-backend-api.md) (the callout surface we extend).

---

## 1. Goal and non-goal

**Goal.** An application is *defined* entirely by its Rust wheel (`rust/<app>-app`) and the
`AppDescriptor` it exports. Standing up a new verifier — even one as involved as Crucible —
requires **no** new Python package. `console-<app>` / `tui-<app>` are two-line shims over the
generic `composer.rustapp.cli`.

**Non-goal — the ecosystem stays shared Python.** "Pure Rust *app*" does not mean "pure Rust
*everything*". The pipeline's **front half** (system analysis + property extraction) is
parametric over an *ecosystem* ([ecosystem.py](../composer/pipeline/ecosystem.py)), and the
`solana` ecosystem — its `SolanaApplication` model, j2 prompts, `locate_main`, global-extraction
strategy — is **chain-specific, not app-specific**. It is legitimately shared by any Solana
backend (Crucible today, a future Solana app tomorrow) and stays Python. The wheel *selects* an
ecosystem by tag (`descriptor.ecosystem = "solana"`); it does not reimplement it.

So the line this doc draws is:

> Everything downstream of "which ecosystem" that is specific to **this verifier** moves into
> the wheel. Everything that is shared **service lifecycle** (Postgres, the TUI event loop,
> `composer.bind`) or shared **chain** logic (the ecosystem) stays Python.

**Security invariant (unchanged, load-bearing).** The LLM never controls a command line; only
file *contents* may be LLM-derived. Today the *trusted wheel* assembles every `crucible …`
argv and Python authors the `Sandbox` *policy* ([command-sandbox.md](./command-sandbox.md) §2).
Every new seam below preserves this exactly: new toolchain steps are still wheel-authored argv
run under a Python-authored policy. See §7.

---

## 2. The gap inventory — where Crucible forks the generic host

Every item below is Python that exists *only* because the generic host can't yet express what
Crucible needs. Each maps to a seam in §3–§5.

| # | Crucible-specific Python | Location | What it does | Seam |
|---|---|---|---|---|
| 1 | `CrucibleArtifactStore`, `CrucibleHarness`, `CrucibleDep` | [store.py](../composer/crucible/store.py), [harness.py](../composer/crucible/harness.py) | Assemble **one Cargo crate** (deps + shared fixture + one feature-gated test section per property) instead of one file per component | §3.1 deliverables callout |
| 2 | Setup-fixture authoring | [backend.py](../composer/crucible/backend.py) `CruciblePreparedSystem.prepare_formalization` | Author + compile-gate a shared `kind="setup"` artifact once, before per-component formalization | §3.2 declared setup step |
| 3 | Context injection | [backend.py](../composer/crucible/backend.py) `CrucibleFormalizer._context` | Thread the fixture + `fuzz_timeout` into each component's `AuthorInput.context` | §3.3 generic context |
| 4 | Crate scaffolding | `_before_formalize` → `store.prepare_component` | Pre-place `Cargo.toml` with cumulative feature declarations before each unit builds | §4 (subsumed by prep + per-run manifest) |
| 5 | Toolchain serialization | `CrucibleFormalizer(command_sem=Semaphore(1))` | Serialize compile/validate (one shared crate / target dir) | §3.4 descriptor flag |
| 6 | Dependency warming | [store.py](../composer/crucible/store.py) `warm_dependencies` + `write_setup_manifest` | Network `cargo fetch` **outside** the sandbox into the private `CARGO_HOME`, so the confined build runs offline | §4 workspace_prep |
| 7 | Program `.so` pre-build | [pipeline.py](../composer/crucible/pipeline.py) `run_crucible_pipeline` → `build_program` | `cargo-build-sbf` / `anchor build` before the pipeline; the harness loads the `.so` | §4 workspace_prep |
| 8 | RAG env | [pipeline.py](../composer/crucible/pipeline.py) `build_crucible_env` | Wire the `crucible_kb` RAG search tools onto the author env | §5.1 descriptor-driven env |
| 9 | Repo resolution + sandbox grants + default provider | [pipeline.py](../composer/crucible/pipeline.py) `resolve_crucible_repo`, `crucible_sandbox` | Resolve `$CRUCIBLE_REPO`; grant it + the `crucible` binary as sandbox `extra_ro`; default to the `launcher` provider | §5.2 grants callout + descriptor flag |
| 10 | CLI entry points + verdict summary | [cli.py](../composer/crucible/cli.py), [results.py](../composer/crucible/results.py) | `console-crucible` / `tui-crucible`; print a per-invariant verdict tally | §5.3 generic summary |

The current generic path (what echoprover uses) is
[`composer/rustapp/cli.py`](../composer/rustapp/cli.py) `console_main` / `tui_main` →
[`entry.py`](../composer/rustapp/entry.py) `rust_entry_point` →
[`host.py`](../composer/rustapp/host.py) `run_application` →
[`adapter.py`](../composer/rustapp/adapter.py) `RustFormalizer`. Crucible's package is a fork of
exactly this chain. The seams below let Crucible re-join it.

---

## 3. Formalization seams (the loop)

### 3.1 Deliverable assembly → a `finalize`-shaped callout

**Today.** The base [`ArtifactStore`](../composer/spec/artifacts.py) writes the *shared*
metadata every backend produces — `properties.json`, `commentary.md`, the property→units map,
`token_usage.json` — and materializes the artifact bytes as `{prefix}_{slug}.{ext}`, one file
per component. `CrucibleArtifactStore` overrides `write_artifact` to instead fold each
component's section into a `CrucibleHarness` and re-render a whole crate (`Cargo.toml` +
`src/main.rs`). `CrucibleHarness`/`CrucibleDep` duplicate, in Python, crate rendering the wheel
*also* does in Rust (`one_file`, and the dep list the harness pins).

**Proposal.** Split the store's job cleanly:

- **Metadata stays generic.** `properties.json` / `commentary.md` / the property map /
  token usage are not app-specific — keep them in the base store, unchanged, for every app.
- **Source deliverable becomes a callout.** Add a descriptor field
  `deliverable_mode: "per_component" | "callout"` (default `per_component` = today's
  echoprover behavior). In `callout` mode the base store writes **no** per-component source
  file; instead the wheel renders the whole deliverable from the full result set.

The natural callout is the one that already exists: `finalize`. It already receives the outcome
set and returns `{relpath: contents}` ([lib.rs](../rust/autoprover-sdk/src/lib.rs) `ffi_finalize`
→ the host writes each file, [adapter.py](../composer/rustapp/adapter.py) `RustFormalizer.finalize`).
We enrich its input so the wheel has everything the crate needs:

```jsonc
// finalize input (per outcome), extended:
{ "name": "...", "delivered": true, "unit_file": "...", "run_link": "...",
  "artifact_text": "<the authored test section>",          // NEW
  "property_units": [["<title>", ["c_slug"]]],              // NEW
  "setup": "<the shared fixture source>" }                  // NEW (run-level, see §3.2)
```

`crucible_app::finalize` then renders `fuzz/<program>/Cargo.toml` + `src/main.rs` from the
fixture + the per-property sections — the `CrucibleHarness` logic, but in Rust, as the **single
source of truth** for crate layout (it already half-lives there as `one_file`). `CrucibleDep`'s
pinned dependency stack moves into the wheel too; it reads `$CRUCIBLE_REPO` directly (§5.2).

**Tradeoff.** The crate lands on disk only at finalize, not incrementally per component. That's
acceptable: the crate is only *runnable* once complete, and `validate` already materializes a
transient copy for each fuzz run via `run_confined`'s `files` map. We note this in the doc so a
future "stream partial deliverables" need is a known, deliberate follow-up.

**Deletes:** `harness.py` entirely; `CrucibleArtifactStore` (the generic `RustArtifactStore`
in `callout` mode suffices — see §4 for why even the manifest pre-placement goes away).

### 3.2 Shared setup artifact → a declared step

**Today.** `CruciblePreparedSystem.prepare_formalization` authors a `kind="setup"` artifact
(the fixture) via `author_and_compile`, once, before per-component formalization, and stashes
it on the store. The wheel *already* handles `kind=="setup"` in `units` (→ empty), `author_prompt`
(→ fixture prompt), and `compile` (→ probe dry-run). Only the *orchestration* is Python.

**Proposal.** Make the setup step declarative on the descriptor:

```rust
setup: Option<SetupSpec>   // { phase_key: "build_harness", label: "Build Harness",
                           //   context_key: "fixture" }
```

When present, the generic `RustPreparedSystem.prepare_formalization` (in
[adapter.py](../composer/rustapp/adapter.py)) runs the existing `author_and_compile` for a
`kind="setup"` input under `setup.phase_key`, and stashes the compiled spec on the formalizer.
This lifts `CruciblePreparedSystem` into the host verbatim — no new logic, just a descriptor
gate. Apps with no setup (echoprover) omit the field and skip the step.

### 3.3 Context injection → generic

**Today.** `_context` returns `{program, fixture, fuzz_timeout}`; the base returns `{program}`.

**Proposal.** The host always injects into every component's `AuthorInput.context`:
(a) the setup result under `setup.context_key` (if a setup step ran), and (b) each **declared
CLI arg** value (so `fuzz_timeout` — already a descriptor `ArgSpec` — is present). The wheel
reads them exactly as it does now (`ctx_str(input, "fixture")`, `ctx_u64(input, "fuzz_timeout")`).
`_context` and the `CrucibleFormalizer` override disappear.

### 3.4 Toolchain serialization → a descriptor flag

**Today.** `CrucibleFormalizer` passes `command_sem=asyncio.Semaphore(1)` because all
compile/validate runs share one crate / target dir.

**Proposal.** Descriptor flag `serialize_toolchain: bool` (default `false`). When `true`, the
generic formalizer constructs the `Semaphore(1)` itself and threads it into `_run_blocking`
(the plumbing already exists — `RustFormalizer.__init__(command_sem=...)`). Crucible sets it
`true`; echoprover leaves it `false` (its `validate` is a no-op with no shared state).

---

## 4. Workspace preparation — a pure plan the host executes

Items **4, 6, 7** (Cargo manifest placement, dependency warming, the `.so` pre-build) are all
the same shape: a **toolchain step that must run before formalization**, part of it needing
**network** (the dependency fetches). Today they're three ad-hoc Python steps
(`write_setup_manifest`, `warm_dependencies`, `build_program`).

**Design constraint discovered in the code.** The command sandbox *never* gives a confined
process network access — `rust_build_policy` hardcodes `network=False`, and the only network
step, `warm_cargo_cache`, runs **unconfined** ([command-sandbox.md](./command-sandbox.md) §5).
So a "prep sandbox with `network: true`" (an earlier draft of this section) would be a brand-new
security capability — a confined process with a socket — which the codebase deliberately avoids.
The right seam therefore does **not** hand the wheel a confined-with-network policy.

**Proposal.** One new **pure** callout that returns a *plan*, executed by the **host** with the
existing shared helpers:

```rust
fn workspace_prep(&self, input: &AuthorInput) -> WorkspacePrep {
    // { files: {relpath: contents},    // e.g. the harness Cargo.toml (deps only the wheel knows)
    //   warm_dirs: [String],           // dirs to `cargo fetch` (unconfined, network)
    //   build_program: Option<String>  // build this program to its platform binary }
}
```

- The host writes `files` (path-confined via `confined_join`), runs `warm_cargo_cache` on each
  `warm_dirs` (**unconfined, network** — a fetch runs no untrusted code), and, if
  `build_program` is set, calls the shared `build_program` capability (which itself warms the
  *program* crate then builds it **confined + offline**).
- **Posture unchanged and Python-owned end to end**: fetches unconfined, code-executing builds
  confined+offline. The wheel touches no command line — it contributes only file *contents* and
  declarative intent (which dirs, which program). Strictly within the existing trust model; no
  new capability.
- `build_program` is the shared Solana build capability
  ([solana/build.py](../composer/spec/solana/build.py)); the generic host invokes it lazily when
  the plan requests it (shared ecosystem capability, not app-specific Python).

**Bonus simplification — the cumulative-feature manifest race disappears (item 4).** The reason
`prepare_component` reserves features *cumulatively* on a shared on-disk `Cargo.toml` is that
concurrent per-component sessions each rewrite it and could drop each other's feature. But with
`serialize_toolchain: true` (§3.4), runs are serialized, and the wheel can materialize
`Cargo.toml` (deps + exactly the one feature this build needs — features are inert `f = []`
entries that don't affect dep resolution) in the **`files` map of each `compile`/`validate` run**.
No shared-manifest mutation across runs ⇒ no race ⇒ no `reserve_features` / `_reserved` /
`prepare_component` machinery at all. The `workspace_prep` plan places only the deps-only manifest that
warming needs. This is the last thing keeping the `CrucibleArtifactStore` alive; with it gone,
§3.1's generic `callout` store fully suffices.

**Deletes:** `write_setup_manifest`, `warm_dependencies`, the `build_program` pre-step call, and
the whole feature-reservation path in `harness.py`.

---

## 5. Entry-point seams (services & UI)

These stay Python (service lifecycle / event loop — the "shell" of
[rust-applications.md](./rust-applications.md) §1), but are made **descriptor-driven** so no
Crucible fork is needed.

### 5.1 RAG env from the descriptor

**Today.** `build_crucible_env` builds the `crucible_kb` RAG tools and is passed as
`env_builder=` everywhere. The generic `build_neutral_env` builds *no* RAG even though the
descriptor already declares `rag_db_default: "crucible_kb"`.

**Proposal.** Fold `build_crucible_env`'s logic into the generic env builder, gated on
`descriptor.rag_db_default`: when set, open that RAG DB and add its search tools (falling back
to no-RAG on failure, exactly as `build_crucible_env` does today); when `None`, the neutral env.
The `env_builder=` override parameter can stay for exotic cases but Crucible stops needing it.

### 5.2 Sandbox grants + default confinement

**Today.** `crucible_sandbox` resolves `$CRUCIBLE_REPO`, grants it + `which("crucible")` as
`extra_ro`, and defaults the provider to `launcher` (fail-closed). `resolve_crucible_repo`
validates the checkout.

**Proposal.**
- **Grants → a pure callout** `sandbox_grants(args) -> { extra_ro: [String], extra_env: [String] }`.
  The wheel resolves `$CRUCIBLE_REPO` and scans `$PATH` for `crucible` in Rust (it already scans
  `$PATH` in `on_path`). The host unions the returned grants into its `SandboxConfig`.
- **Default confinement → a descriptor flag** `confine_by_default: bool` (true for any wheel
  with real toolchain callouts). The generic entry builds the `launcher` `SandboxConfig` when
  set (still overridable by `COMPOSER_SANDBOX_PROVIDER=none`), replacing the hardcoded default in
  `crucible_sandbox`.
- **Repo validation → `validate_preconditions`.** The wheel already validates the workspace
  there (`Cargo.toml` present, required binaries on `$PATH`); add the `$CRUCIBLE_REPO` /
  `crates/crucible-fuzzer` check. `resolve_crucible_repo` disappears; the `--crucible-repo` flag
  becomes a descriptor `ArgSpec` the wheel reads from `args`/env.

### 5.3 Verdict summary → generic

**Today.** `results.py` (`summarize_verdicts`, `format_verdict_lines`) turns
`RustFormalResult.verdicts` into a console tally; `crucible/cli.py` prints it. But `verdicts` is
**already a generic field** on the generic result type.

**Proposal.** Move `results.py` into `composer/rustapp/` and have the generic `console_main` /
`tui_main` print the verdict tally whenever the results carry verdicts (empty ⇒ prints nothing,
exactly as today for echoprover). Parametrize the outcome wording by `descriptor.backend_tag`
(the report's `outcome_label(tag, …)` already takes the tag). One nicety: make the component
noun a descriptor field `component_noun: "instruction"` (default `"component"`) so Crucible's
"Instructions:" line and echoprover's "Components:" line come from the same code.

---

## 6. What Crucible collapses to

After §3–§5:

- **Deleted:** `composer/crucible/` in its entirety — `backend.py`, `store.py`, `harness.py`,
  `pipeline.py`, `results.py`, `cli.py`.
- **`pyproject.toml`:**
  ```toml
  console-crucible = "composer.crucible_launch:console_crucible"   # 2-line shim, or:
  console-crucible = "composer.rustapp.cli:console_main"           # via a --module arg
  tui-crucible     = "composer.rustapp.cli:tui_main"
  ```
  (A thin `crucible_launch.py` = `def console_crucible(): return console_main("crucible_app")`
  keeps the bare `console-crucible` command with no positional module arg. This is the *only*
  Python left, and it's shared-shaped — echoprover has the identical shim.)
- **The wheel (`rust/crucible-app`)** grows: `workspace_prep`, `sandbox_grants`, a richer
  `finalize` (crate rendering, absorbing `CrucibleHarness`/`CrucibleDep`), the repo precondition,
  and a descriptor carrying `setup`, `deliverable_mode: "callout"`, `serialize_toolchain: true`,
  `confine_by_default: true`, `component_noun: "instruction"`. It reads `$CRUCIBLE_REPO` itself.
- **Everything downstream of "ecosystem = solana"** is the shared front half — unchanged.

The proof obligation: `console-crucible <project> <program> <doc> --fuzz-timeout N` produces a
byte-identical deliverable + report to today, at parity runtime (the e2e Vault gate — 16 GOOD,
~41 min baseline).

---

## 7. Security invariant — preserved, and audited per seam

> The LLM controls file *contents* only; the trusted wheel controls every argv; Python authors
> every sandbox *policy*.

| Seam | New capability | Who controls argv | Who authors policy | Net |
|---|---|---|---|---|
| §3.1 finalize deliverable | writes files under project root | — (host writes, path-confined via `confined_join`) | n/a | same as today's store |
| §4 workspace_prep | warm dirs + build a program | — (host runs the shared `warm_cargo_cache` / `build_program`; wheel supplies only file *contents* + which dirs/program) | **Python** `SandboxConfig` | **identical** to today (fetch unconfined, build confined+offline) |
| §5.2 sandbox_grants | adds `extra_ro`/`extra_env` | n/a (data) | Python unions into its policy | same grants, now wheel-declared |

No seam gives the *LLM* argv control, and no seam lets the *wheel* invent a sandbox policy. §4 is
a *pure declaration* — the host runs the same shared warm/build helpers it does today, so the
network posture (fetch unconfined, code-executing build confined + offline) is byte-for-byte the
current behavior. Nothing here weakens or "tightens" confinement; it only moves *who declares the
plan* into the wheel.

---

## 8. New surface, summarized

**Descriptor (`AppDescriptor`, mirrored in [descriptor.py](../composer/rustapp/descriptor.py)):**

```rust
setup: Option<SetupSpec>,          // { phase_key, label, context_key }  (§3.2)
deliverable_mode: DeliverableMode, // PerComponent | Callout, default PerComponent  (§3.1)
serialize_toolchain: bool,         // default false  (§3.4)
confine_by_default: bool,          // default false  (§5.2)
component_noun: Option<String>,    // default "component"  (§5.3)
```

**New callouts on the `Backend` trait (both pure):**

```rust
fn workspace_prep(&self, input) -> WorkspacePrep { default }   // { files, warm_dirs, build_program }  (§4)
fn sandbox_grants(&self, args: &serde_json::Value) -> SandboxGrants { default }  // { extra_ro, extra_env }  (§5.2)
// finalize's input gains artifact_text / property_units / setup (§3.1) — signature unchanged.
```

All are **defaulted**, so existing wheels (echoprover) keep working untouched — this is a
backward-compatible extension of the passive-backend API, not a new protocol.

---

## 9. Work breakdown

1. **Descriptor + defaults** — add the five fields to `AppDescriptor` (Rust + pydantic mirror),
   all defaulted; `workspace_prep` / `sandbox_grants` pure trait methods with no-op defaults.
   *No behavior change; echoprover + Crucible-via-Python still run.*
2. **Generic host honors them** — setup step, context injection, `serialize_toolchain`,
   `callout` deliverable via enriched `finalize`, `workspace_prep` execution (write files → warm
   → build), RAG-from-descriptor, `confine_by_default` + grants union, generic verdict summary.
   Land behind the descriptor gates so echoprover is unaffected.
3. **Port `crucible_app`** — move `CrucibleHarness`/`CrucibleDep` rendering into `finalize`;
   implement `workspace_prep` (harness `Cargo.toml` + warm dirs + program build) and
   `sandbox_grants`; add the repo precondition; set the descriptor flags; materialize `Cargo.toml`
   per confined run.
4. **Delete `composer/crucible/`** and repoint the console scripts. Update the gate tests
   (`test_crucible_*`) to drive the wheel through the generic host.
5. **e2e parity** — run the Vault gate; confirm 16 GOOD at ~parity runtime and byte-identical
   deliverable + report. (Posture is unchanged, so no new confinement risk to validate.)

Steps 1–2 are the reusable investment (they benefit *every* future Rust app); 3–5 are the
Crucible port that proves the seam.

---

## 10. Open questions / decisions to confirm

1. **`finalize` vs. a dedicated `render_deliverables` callout.** Reusing `finalize` keeps the
   surface minimal but overloads one method with "side-effect artifacts" and "the primary
   deliverable." A separate `render_deliverables(results) -> {relpath: contents}` is clearer at
   the cost of one more callout. *Leaning: reuse `finalize`, revisit if a second app wants both.*
2. **Where the Solana build capability lives.** `workspace_prep`'s `build_program` routes to the
   shared `composer.spec.solana.build.build_program` — so the generic host gains one lazy import
   of an ecosystem-specific helper. Acceptable (it's shared, not app-specific, and gated on the
   plan), but the cleaner long-term home is a `build` capability on the `Ecosystem` itself.
   *Leaning: lazy import now; promote to the ecosystem seam if a second ecosystem needs it.*
3. **Per-run `Cargo.toml` materialization vs. a stable on-disk crate.** Materializing the
   manifest per confined run (§4) is what removes the feature-race, but it means the on-disk
   crate between runs is whatever the last run wrote. Since the *authoritative* crate is produced
   at `finalize`, this is fine — but if any tooling expects a stable mid-run crate dir, we'd keep
   a single `workspace_prep`-placed manifest with all features (reintroducing a mild coupling).
4. **`--module`-style generic command vs. per-app shims.** Whether `console-crucible` is a 2-line
   shim (`console_main("crucible_app")`) or the generic `console_main` takes the module as its
   first positional. Shims keep the familiar command names; the generic form is one entry for all
   wheels. *Leaning: shims, matching echoprover.*

---

## 11. Key files

- Wheel: [rust/crucible-app/src/lib.rs](../rust/crucible-app/src/lib.rs),
  [rust/autoprover-sdk/src/lib.rs](../rust/autoprover-sdk/src/lib.rs) (trait + descriptor + `run_confined`).
- Generic host (the reuse target): [composer/rustapp/](../composer/rustapp/) —
  `descriptor.py`, `adapter.py`, `host.py`, `entry.py`, `cli.py`, `frontend.py`, `store.py`.
- To be deleted: [composer/crucible/](../composer/crucible/) —
  `backend.py`, `store.py`, `harness.py`, `pipeline.py`, `results.py`, `cli.py`.
- Shared, staying: [composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py) (the
  `solana` ecosystem), [composer/spec/solana/](../composer/spec/solana/) (the model),
  [composer/sandbox/](../composer/sandbox/) (`SandboxConfig` policy authoring).
</content>
</invoke>
