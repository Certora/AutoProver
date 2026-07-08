# Proposal — A Solana Verification Application (Crucible backend)

> A plan to stand up a new AutoProver **application** that authors properties for **Solana**
> programs and checks them with **[Crucible](https://github.com/asymmetric-research/crucible)**,
> a coverage-guided fuzzer for Solana. The application pairs the **`solana` ecosystem** (the
> analysis/extraction front half, already built — [ecosystem-abstraction.md](./ecosystem-abstraction.md)
> §8.1, Phase 4) with a **new Crucible backend**, implemented as a **Rust application** on the
> PyO3 framework ([rust-applications.md](./rust-applications.md) ·
> [rust-formalization-backends.md](./rust-formalization-backends.md)).
>
> Companion to [application-abstraction.md](./application-abstraction.md) (the five pieces of an
> application) and [formalization-abstraction.md](./formalization-abstraction.md) (the backend
> seam). The **Foundry** application is the closest existing analog and the running reference
> throughout: like Foundry, Crucible authors a source-language artifact, gates it with an
> external local CLI, and produces *refutation-oriented* verdicts. **Status: proposal / for
> review.**

---

## 1. What we are building, in one paragraph

AutoProver already has two orthogonal axes ([ecosystem-abstraction.md §2](./ecosystem-abstraction.md)):
the **ecosystem** (front half — how we model and reason about a domain) and the **backend** (back
half — how a property becomes a checked artifact). The `solana` ecosystem front half exists and is
gated: on a real Anchor program it analyzes the program into instructions and extracts sane,
Solana-native properties, today terminating in a `NullSolanaBackend` that only records them. **This
project replaces that null backend with a real one: a Crucible backend that turns each extracted
property into a Crucible fuzz harness, runs the fuzzer, and reports pass/refuted.** Per the design
decision in [rust-applications.md](./rust-applications.md), the backend is written **in Rust** as a
PyO3 wheel implementing the `autoprover-sdk` traits, consumed by the generic Python host — so the
new AutoProver application, `crucible`, is *`ecosystem="solana"` + a Crucible backend wheel* and
needs (ideally) zero bespoke Python.

```
          ┌──────────── solana ecosystem (DONE) ───────────┐   ┌──── Crucible backend (NEW) ────┐
program ─analyze─▶ SolanaApplication ─extract─▶ properties ─formalize─▶ fuzz harness ─verdicts─▶ report
          (SolanaProgram / SolanaInstruction,     (signer/owner,   (a Crucible crate: fixture +   (crash → BAD,
           accounts, PDAs, CPI — model.py)         PDA, overflow…)  actions + invariant tests)     clean → GOOD*)
                                                                    run: `crucible run … --timeout`
                                          *GOOD = no violation found within the fuzzing budget (bounded, not a proof)
```

---

## 2. What already exists (and is reused unchanged)

| Piece | Where | Status |
|---|---|---|
| `solana` ecosystem: `SolanaApplication` model (programs / instructions / account constraints / CPI / authorities) | [composer/spec/solana/model.py](../composer/spec/solana/model.py) | **Done** (Phase 4) |
| `RUST` language facet (Cargo `forbidden_read`, Rust `code_explorer` prompt, `rust/_failure_modes.j2`) + `SOLANA` chain (validate / `locate_main` / `units`) | [composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py) · `composer/templates/{rust,solana}/…` | **Done** (Phase 4) |
| Solana analysis + property-extraction prompts | `composer/templates/solana/…` | **Done** (Phase 4) |
| The Rust-application framework: `AppDescriptor`, `Application`/`FormalizeSession` traits, the IoC `Command`/`Observation` ABI, `export_app!` | [rust/autoprover-sdk/src/lib.rs](../rust/autoprover-sdk/src/lib.rs) | **Done** |
| The generic Python host: enum/argparse/entry-point/frontend synthesis, `resolve_ecosystem`, the IoC effect loop, the `RustBackend` adapter | [composer/rustapp/](../composer/rustapp/) (`host.py`, `entry.py`, `loop.py`, `adapter.py`, `descriptor.py`) | **Done** |
| Ecosystem selection by descriptor (`AppDescriptor.ecosystem`, registry lookup) | [rust/autoprover-sdk/src/lib.rs](../rust/autoprover-sdk/src/lib.rs) · [composer/rustapp/host.py](../composer/rustapp/host.py) | **Done** (Phase 3) |
| A reusable null backend + Anchor `solana_vault` scenario + live gate | [composer/spec/solana/null_backend.py](../composer/spec/solana/null_backend.py) · [test_scenarios/solana_vault/](../test_scenarios/solana_vault/) · [tests/test_solana_gate.py](../tests/test_solana_gate.py) | **Done** |

The net: **the entire front half and the entire Rust-app shell are already built and gated.** This
project is squarely a *backend* effort (formalization-abstraction.md §9's checklist), plus the
Crucible-specific infrastructure that a fuzzing backend needs but the prover/Foundry backends did
not. That new infrastructure — §7 — is the real content of the plan.

---

## 3. What Crucible is (and why Foundry is the right mental model)

Crucible is a **coverage-guided fuzzing framework for Solana programs** (LibAFL + LiteSVM). You
declare a program's actions, write invariants, and the fuzzer searches randomly generated action
sequences for violations. The relevant facts for backend design:

- **The artifact is a Rust *fuzz-harness crate*, not a spec file.** A harness (`fuzz/<program>/`)
  is a standalone Cargo workspace with:
  - `src/main.rs` — a `#[derive(Clone)]` **fixture** with a `setup()` (loads the program `.so`,
    creates accounts, runs init instructions in dependency order), `action_*` methods (one per
    instruction, with `#[range(..)]`-constrained fuzz params), an optional `after_action` hook, and
    one or more **tests**: `#[invariant_test]` fns (stateful, checked after every action) and/or
    `#[crucible_fuzz]` fns (single-operation, random inputs). Invariants use `fuzz_assert_*!` macros
    (which record a violation instead of aborting the process).
  - `Cargo.toml` — a `[[bin]]` plus a **feature per test** (`crucible run <prog> <test>` requires the
    feature name to equal the test name).
  - `idls/<program>.json` — the program IDL, from which `crucible-idl-gen` generates typed
    `instruction`/`accounts`/`state` bindings at compile time (`raw_call()` is the fallback when no
    IDL exists).
- **It is invoked as a local CLI, and verdicts come off its output** (the user's chosen model — like
  `forge test`): `crucible run <program> <test> --release --timeout <secs>`. Structured stdout:
  `[FUZZ_PULSE]` progress, `[FUZZ_FINDING] reproduces:true summary:<msg>` on a violation (a crash
  file + `.meta.json` action sequence written alongside), `[FUZZ_ERROR]` on fatal setup error.
  `--dry-run` compiles + runs one iteration to validate the harness. There is **no run service and
  no run link** — pass/fail is local.
- **Verdicts are refutation-oriented, exactly like Foundry.** A crash *refutes* a property (BAD); a
  clean run to the timeout means *no violation found within the budget* (GOOD\*, bounded — not a
  proof). So the Crucible `backend_guidance` is nearly Foundry's: write universals freely, their
  refutations are valuable, but skip properties a fuzzer can't meaningfully sample (off-chain
  events, pure hash-collision resistance).

Side-by-side, the three backends:

| | prover (CVL) | **Foundry** | **Crucible (new)** |
|---|---|---|---|
| Artifact | `.spec` (+ `.conf`) | `.t.sol` | a **fuzz-harness crate** (`main.rs` + `Cargo.toml` feature + `idls/`) |
| Gate | Certora Prover (cloud, run link) | `forge test` (local) | `crucible run --timeout` (local) |
| Verdict source | prover output service | local exit/parsed output | parsed `[FUZZ_FINDING]` / crash dir |
| Verdict meaning | proof / CEX | bounded refutation | bounded refutation |
| Prep pre-work | AutoSetup ∥ summaries ∥ invariants | none (identity) | **build program `.so` + IDL + author the shared fixture/actions** |
| Deliverable granularity | one `.spec` per component | one `.t.sol` per component | **one *test* (feature+fn) per component, in one shared crate** |

The last row is the crux and the source of most new infrastructure (§7.1): unlike the prover and
Foundry, a Crucible deliverable is **one multi-file crate shared across all components**, not one
file per component.

---

## 4. The application: `crucible`

Following the naming of `foundry` (an app named for its tool), the application is **`crucible`**:
`ecosystem="solana"` + the Crucible backend wheel. Mapped onto
[application-abstraction.md](./application-abstraction.md)'s five pieces, everything but the backend
comes free from the Rust-app host:

| Piece | Source | Crucible specifics |
|---|---|---|
| Phase enum `P` | host, synthesized from `descriptor.phases` | `Analysis → Extraction → BuildHarness(setup) → Formalization → Report` (Build/setup is a UI-only phase, cf. autoprove's harness phase) |
| Entry point / Executor | host (`_generic_entry_point`) | positional `project_root main_program system_doc`; declared args: `--crucible-binary`, `--fuzz-timeout`, `--fuzz-cores`, `--stateful`, `--max-actions`; `validate_preconditions` checks the toolchain + a Cargo/Anchor project (§6) |
| Pipeline wrapper | host (`run_rust_pipeline`) | passes `ecosystem=SOLANA` (resolved from `descriptor.ecosystem="solana"`) |
| **Backend** | **Rust wheel (new)** | **the whole of §5** |
| Frontend | host (`GenericRustApp` / console) | `event_kinds`: `fuzz_pulse` (coverage/exec-rate), `fuzz_finding` (crash), `build_output` (cargo/build-sbf) |
| Artifact store | host shell + Rust formatter | **needs the multi-file-crate extension, §7.1** |
| `main()` | host | unchanged |

So the deliverables of *this* project are: the Rust wheel (§5), the ABI/host extensions a fuzzing
backend forces (§7), the toolchain/preconditions (§6), and a scenario + gate (§8).

---

## 5. The Crucible backend, method by method

The backend is a Rust `Application` (in a new crate, e.g. `rust/crucible-app/`) that decides via a
`FormalizeSession`. The three phase objects of the formalization abstraction
([formalization-abstraction.md §2](./formalization-abstraction.md)) map as follows. **The governing
idea mirrors the CVL backend's structural-invariant pattern**: author the expensive, program-wide
scaffold *once* in `prepare_formalization`, then have each per-component `formalize` contribute only
its own test — just as CVL builds `invariants.spec` once and each per-component spec `import`s it.

### 5.1 `prepare_system` — build the program + IDL + fixture skeleton

Roughly Foundry's identity transform, but with real pre-work because a Crucible harness must
*compile against a built program*:

1. `locate_main` (from the `SOLANA` chain) picks the target `SolanaProgramInstance`.
2. Build the program to sBPF: `cargo build-sbf` (or `anchor build`) → `target/deploy/<program>.so`.
3. Generate/collect the IDL: `anchor idl build` (or convert an existing one) →
   `fuzz/<program>/idls/<program>.json`; the harness uses `crucible_idl_gen::declare_fuzz_program!`.

This is naturally a **UI-only "BuildHarness" phase** with its own `TaskInfo`. Its outputs (the `.so`
path, the IDL, the program id) flow into the next phase as immutable state.

> **Build the Solana build step as shared, reusable infrastructure — not Crucible-specific.** Steps
> 2–3 (`source → .so + IDL`, version-aware per §6.1) are needed by *every* Solana backend, and a
> future Certora-Prover-style Solana backend will go further and **munge the source and rebuild** it
> (harness lift, mocks, `cvlr` hooks) — the exact analog of the EVM CVL backend's `prepare_system`
> harness-lift ([formalization-abstraction.md §4.1](./formalization-abstraction.md)). So factor a
> reusable "Solana build" capability — `source → [optional munge] → .so + IDL` — that Crucible calls
> in its *no-munge* mode and a Prover backend calls in its *munge-and-rebuild* mode, mirroring how the
> EVM backends share solc/harness tooling. A **user-supplied prebuilt `.so`** is then just an optional
> fast-path for the no-munge (Crucible) case; it does not remove the pipeline, since other backends
> must rebuild, so it is a minor optimization at most.

### 5.2 `prepare_formalization` — author the shared fixture + actions (once)

The single most important step, and the biggest LLM authoring job. Using the `SolanaApplication`
model + source, author the **shared harness scaffold** that every per-component test reuses:

- the `#[derive(Clone)] struct <P>Fixture { ctx, program_id, … }` and its `#[fuzz_fixture] setup()`
  (init order, admin/authority whitelists, PDA seed encoding, token accounts — the *Harness Guide*
  is essentially the agent's playbook here);
- one `action_*` per instruction (typed `ctx.program(..).call(..).accounts(..).signers(..).send()`,
  or `raw_call` when no IDL), with `#[range(..)]` bounds inferred from the property set;
- an `after_action` hook if useful.

Gate: `crucible run <prog> <test> --dry-run` must succeed (harness compiles + `setup()` runs one
iteration). This is the "loud, fail-fast setup" the Harness Guide demands, surfaced as a hard
validation gate exactly like the CVL prover gate. The returned `Formalizer` carries the built `.so`,
the IDL, the compiled scaffold, and the run config as immutable state.

> This is the direct analog of CVL's `prepare_formalization`: expensive, program-wide, run once,
> and *its output is a shared precondition for every component* — here the fixture/actions rather
> than `invariants.spec`.

### 5.3 `formalize` — per component, author one Crucible test (the IoC loop)

For each extracted component (a `SolanaInstructionInstance`, or property group — see §9 Q1), author
**one Crucible test** encoding that component's properties, sharing the scaffold from §5.2:

- pick the test form per property kind: `#[invariant_test]` for conservation/solvency/consistency
  invariants (checked after each action), `#[crucible_fuzz]` for single-instruction properties
  (overflow, access-control-on-one-call);
- write the `fuzz_assert_*!` checks against **on-chain** state (`read_anchor_account`), not local
  mirrors;
- register the test's feature in `Cargo.toml` (feature name = test name);
- gate with `crucible run <prog> <test> --dry-run` (compiles + one iteration), then run a bounded
  `crucible run <prog> <test> --release --timeout <budget> [--stateful]`.

The **verification loop is the Rust IoC decider** ([rust-formalization-backends.md](./rust-formalization-backends.md)
Tier 2): the `FormalizeSession` emits `CallLlm` to draft/revise the test, a "run Crucible" effect to
compile+fuzz, interprets the parsed result (compile error → revise; `[FUZZ_FINDING]` → the property
is *refuted*, record the minimized action sequence via `crucible tmin` and either publish-as-refuted
or, if the harness itself is wrong, revise; clean timeout → publish-as-held), and terminates with
`Publish`/`GiveUp`. Python owns every effect; Rust only decides. The "run Crucible" step is a
**general run-a-command-over-files effect** (§7.2): crucially, the *Rust decider* fixes the command
line and the LLM only ever authors file *contents*.

### 5.4 `fetch_verdicts` — refuted vs held, off the local result

Like Foundry's `_foundry_verdicts` (read pass/fail straight off the stored result, no run service):
each test's outcome is known at `formalize` time (a `[FUZZ_FINDING]` was emitted or not), so it is
baked into the published `Formalized` and `fetch_verdicts` maps it to `Verdict{ BAD (refuted, with
the crash's action sequence / line) | GOOD (no violation within budget) | ERROR (build failed) |
TIMEOUT }`. `run_link` is `None` (no run service).

### 5.5 `finalize` — assemble/emit the buildable crate

The one hook that sees all outcomes at once: ensure `Cargo.toml` lists every generated test's
feature and write the crash artifacts / a `{test → crash sequence}` map under the metadata dir. If
the per-component artifact model can't produce a single compilable `main.rs` incrementally, `finalize`
is where the shared scaffold + all per-component test fns are stitched into the final crate (§7.1).

---

## 6. Toolchain & preconditions

A fuzzing backend has heavier local prerequisites than the prover (which offloads to the cloud) or
Foundry (just `forge`). `validate_preconditions` (a **sync** Rust hook, run before any service opens
— [rust-applications.md §4.2](./rust-applications.md)) should check, with actionable error messages:

- **`crucible`** on `PATH` (or `--crucible-binary`); `crucible --version`.
- **The Solana/Anchor build toolchain**: `cargo build-sbf` (Solana CLI) and/or `anchor`, plus a
  `rust-toolchain.toml` compatible with the target program (the examples pin one).
- **A buildable target program**: a Cargo/Anchor workspace with the program under `programs/<name>/`
  (mirror Foundry's `foundry.toml`-exists precondition).
- **Version skew** between the program's Solana deps and Crucible's — the docs explicitly call this
  out and offer `crucible-idl-gen` (IDL → types without a crate dep) as the escape hatch. The backend
  should prefer the standalone-IDL path to stay robust across program toolchains.

These are environment facts the gate (§8) must provision, analogous to the prover's `solc`/AutoSetup
and Foundry's `forge`.

### 6.1 Version compatibility (Crucible / Solana-Anchor / Rust)

Unlike `forge` (one self-contained binary) or the cloud prover (server-pinned), a Crucible run is a
**version matrix**, and getting it wrong shows up as a compile error deep in the fuzz phase. This is
the Rust/Solana analog of the `solc`-version pinning the autoprove pipeline already needs (the Counter
scenario's `pragma ^0.8.29` vs a 0.8.21 default — [ecosystem-abstraction.md §10](./ecosystem-abstraction.md)),
but with more moving parts. Three axes:

- **A Crucible version pins a whole stack.** The harness depends on `crucible-fuzzer` /
  `crucible-test-context` / `crucible-idl-gen`, and a given Crucible release pins `litesvm`,
  `anchor-lang`, `solana-*`, and `solana-sbpf` (in Crucible's workspace today: `litesvm = 0.9`,
  `anchor-lang = 1.0.1`, `solana-* = 3.0`, `solana-sbpf = 0.13`). The generated harness must depend on
  the *matching* set or it won't build.
- **The target program's Solana version may differ from the fuzzer's.** Crucible's own docs call this
  out and provide the decoupling: `crucible-idl-gen`'s `declare_fuzz_program!` generates typed bindings
  **from the IDL, with no crate dependency on the program**, so the harness need not compile against the
  program's Solana version. Prefer this path always. (Orthogonal axis: `litesvm` must still be able to
  *load and execute* the program's compiled `.so` — i.e. loader/sBPF compatibility between the
  program's build and the fuzzer's `litesvm`.)
- **Two Rust toolchains, not one.** The **harness** is a *native* build — Crucible forces
  `RUSTUP_TOOLCHAIN=stable` ([try_cargo_build, lib.rs:2031](../../crucible/crates/crucible-fuzz-cli/src/lib.rs#L2031))
  — so it needs a host `stable` recent enough for that Crucible version's MSRV. The **program `.so`** is
  an *sBPF* build via `cargo build-sbf` / `anchor build`, which uses Solana's **platform-tools** bundled
  Rust (pinned per Solana/Anchor version, driven by the project's `rust-toolchain.toml` / `Anchor.toml`),
  not host stable. Both must be present and mutually compatible.

What this means for the backend:

1. **Make the version explicit and selectable**, not "whatever's on `PATH`": a `--crucible-version`
   (release tag / git ref) plus detection of the program's Solana/Anchor version from its
   `Cargo.toml` / `Anchor.toml` / `rust-toolchain.toml`. `validate_preconditions` resolves these to a
   concrete, compatible combination up front and fails fast (with the required versions) on a mismatch
   or a missing toolchain.
2. **The backend owns the pin.** Because the backend (not the LLM) authors `Cargo.toml` (§7.2/§7.4), it
   generates the harness manifest from a small **compatibility table** — `Crucible version → { crucible
   crate refs, litesvm, anchor, solana-*, min host rustc }` — so version selection is one trusted lookup,
   not something the LLM can perturb.
3. **Support a *curated set*, via per-version sandbox images.** §7.4 already requires an offline,
   vendored build inside the sandbox; make the sandbox image the unit of version support — one immutable
   image per supported `(Crucible × Solana/Anchor)` combo, each carrying the matching `crucible` binary,
   the host `stable` toolchain, the Solana platform-tools, and the vendored crate set. "Support a new
   version" then means "add a vetted image to the matrix," which also bounds the combinatorics — we
   support a known list, not arbitrary versions.
4. **Record the resolved versions** in the deliverable (the generated `Cargo.toml` pins are part of it)
   and **fold them into the formalize cache key**, so a result built against Crucible vX / Solana vY is
   not silently reused when a different combination is selected (cf. the CVL backend threading `config`
   through its result for reproducibility, [formalization-abstraction.md §5](./formalization-abstraction.md)).

---

## 7. New infrastructure a fuzzing backend forces

The prover and Foundry backends fit the existing seam cleanly. Crucible stresses four assumptions
that were previously EVM/prover/Foundry-shaped. These are the genuinely new build items.

### 7.1 Multi-file, one-crate-shared-across-components deliverable

**The problem.** `ArtifactStore` and `AppDescriptor.artifact_layout` assume *one deliverable file per
component*: `<prefix>_<slug>.<ext>` ([formalization-abstraction.md §6](./formalization-abstraction.md);
the layout fields in [autoprover-sdk](../rust/autoprover-sdk/src/lib.rs) are `artifact_prefix` +
`artifact_extension`). A Crucible deliverable is **one Cargo crate** whose `main.rs` holds a *shared*
fixture/actions plus *one test fn per component*, with a *per-component feature* in a shared
`Cargo.toml`. There is no clean "one file per component."

**Options.**
- **(a) Per-component `[[bin]]`/module, shared code in a lib.** Each component → its own
  `src/bin/<slug>.rs` (or a `#[cfg(feature)]` module) that imports the shared fixture from a sibling
  `lib.rs`. Restores one-file-per-component and lets the generic layout mostly stand; costs a
  slightly less idiomatic harness and more Cargo wiring.
- **(b) A Crucible-specific `ArtifactStore` that owns a crate.** The per-component "artifact_text" is
  that component's test fn (for the report/commentary), and the store *assembles* the final
  `main.rs` + `Cargo.toml` from the shared scaffold + all per-component fns (in `finalize`, §5.5).
  Most faithful to Crucible; requires extending the Rust-app host's artifact model beyond the
  single-file `ArtifactLayout` (a new descriptor shape, or an app-owned store).

**Recommendation:** start with **(a)** to reuse the fan-out + caching + generic store with minimal
host changes; move to **(b)** only if the split harness proves awkward. Either way this is the
first concrete deliverable to prototype, because it shapes everything downstream.

### 7.2 A general "run a local command over a set of files" effect

Today the IoC vocabulary ([autoprover-sdk](../rust/autoprover-sdk/src/lib.rs) · [loop.py](../composer/rustapp/loop.py))
is prover-specific: `RunProver { spec: String }` + `RunFeedback`, a *single* spec string checked by
*the* verifier. That shape is wrong here for two independent reasons:

1. **There is no single "verifier for an ecosystem."** We intend multiple backends per ecosystem
   (Crucible is one Solana backend; others will follow), each driving its own tool(s) — `crucible`,
   `cargo build-sbf`, `anchor idl`, and whatever a future backend needs. The effect must therefore be
   **backend-agnostic**: *the Rust decider names the command*, rather than the framework hardcoding a
   per-ecosystem prover.
2. **A harness is a multi-file crate,** not one `spec: String`.

So replace the prover-specific pair with **one general effect** — materialize a set of files, run a
command over them, return the output — which the run-Crucible, build-`.so`, and IDL steps all reuse:

```rust
// Command (Rust → Python)
RunCommand {
    program: String,              // e.g. "crucible"      ── authored by the Rust decider
    args: Vec<String>,            // e.g. ["run","vault","inv","--release","--timeout","60"]
    files: BTreeMap<String, String>,  // workdir-relative path → contents (merged into the session workdir)
}
// Observation (Python → Rust)
CommandResult { exit_code: i32, stdout: String, stderr: String }
```

`RealEffects` gains a runner that writes `files` into a per-session sandbox workdir, executes
`program`+`args` there **bounded by a semaphore + timeout** (exactly Foundry's `_ForgeRunConfig`
discipline — critical because `crucible --cores` is greedy), tees stdout/stderr to the frontend
(the `fuzz_pulse`/`build_output` event kinds), and returns the result for the Rust decider to parse
(`[FUZZ_FINDING]`, cargo errors, …). It is additive — new `Command`/`Observation` variants + one new
`Effects` method with a default — so the prover/echoprover path is untouched, and any future
CLI-gated backend reuses it.

#### The trust boundary: Rust owns argv, the LLM owns only file *contents*

The parties differ in trust: the **driver** (Python) and the **backend wheel** (compiled Rust) are
trusted; the **LLM** is not. The invariant to enforce — and the reason `program`/`args` are separate
structured fields, never a shell string:

> **The LLM never influences the command line. It authors only the *contents* of input files.**

Concretely, the LLM's *only* output channel is `CallLlm` replies. The Rust decider parses those
replies into file **contents** and places them in `files` under **paths it chooses**; it constructs
`program`/`args` from its own compiled logic. The LLM has no path into `program`, `args`, or the file
*paths*. Python then enforces this defensively:

- **Exec, not shell.** Run via `asyncio.create_subprocess_exec(program, *args)` — never
  `create_subprocess_shell` / a shell string. File contents can't inject argv even in principle.
- **Path confinement.** Every `files` key must be a relative path that stays inside the session
  workdir (reject `..` / absolute paths), so the LLM's contents can't land at `~/.bashrc` etc.
- **Program allowlist (optional, defense-in-depth).** The descriptor can declare the binaries a
  backend is permitted to invoke, so even a buggy wheel can't launch an arbitrary program.

One honest caveat this rule does **not** cover — and it is bigger than it looks. It is tempting to
think the SVM sandboxes the LLM's code, but it does not, and this was **verified against Crucible's
source**, not assumed:

- The **harness** (fixture `setup()` + `action_*` + invariant fns — the LLM-authored part) is built
  with plain `cargo build --release --features <test>` for the **host** target (`RUSTUP_TOOLCHAIN=stable`),
  *not* `cargo build-sbf` — a **native binary** (`try_cargo_build`,
  [crucible-fuzz-cli/src/lib.rs:2031](../../crucible/crates/crucible-fuzz-cli/src/lib.rs#L2031)).
- The CLI executes that binary **directly and unwrapped**: `Command::new(&binary_path).…status()`
  ([lib.rs:531](../../crucible/crates/crucible-fuzz-cli/src/lib.rs#L531),
  [:727](../../crucible/crates/crucible-fuzz-cli/src/lib.rs#L727)) — no launcher, no isolation.
- **LiteSVM is a linked-in library, not a boundary** (`litesvm = "0.9.0"`, a struct field
  `TestContext { pub svm: LiteSVM, … }`,
  [crucible-test-context/src/lib.rs:1326](../../crucible/crates/crucible-test-context/src/lib.rs#L1326));
  it is "a lightweight Solana VM that runs inside your tests" — [litesvm.com](https://www.litesvm.com/docs/getting-started)).
  It interprets the **program-under-test's sBPF bytecode** in-process — sandboxing the user's `.so`,
  not the native harness around it.
- A scan of the whole dependency tree for sandbox tech (seccomp / landlock / namespaces / nsjail /
  bubblewrap / gvisor / wasmtime / rlimit / chroot / unshare / capabilities) found **none**.

So the LLM-authored code runs **natively, with full process privileges**, at two points: **build
time** (`build.rs` / proc-macros during `cargo build`) and **run time** (`setup()`, every
`action_*`, the invariant fns — only the *instructions they submit* execute as sandboxed sBPF). The
argv boundary is therefore necessary but nowhere near sufficient — `crucible run` on LLM-authored
source is arbitrary native code execution regardless. The whole build+fuzz must run in a real sandbox
that **we** provide (Crucible provides none); how, is §7.4.

### 7.3 Verdict semantics + report nouns for a fuzzer

Reuse Foundry's refutation semantics wholesale, but the report should read in Solana/fuzzing nouns
("program"/"instruction"/"fuzz test"/"violation" vs "contract"/"rule"). This is
[ecosystem-abstraction.md §11 Q3](./ecosystem-abstraction.md) (report labels by ecosystem) surfacing
for real; a small `ecosystem`/`backend_tag`-driven noun map suffices. Minor.

### 7.4 Sandboxing the untrusted build+fuzz

Because the LLM-authored harness runs as native code (§7.2), each build+fuzz must execute in an
isolation boundary *we* provide — the outer AutoProver container protects the host *from* AutoProver,
but not AutoProver's own secrets/network *from* code running inside it. **The scope is every step that
compiles or runs untrusted Rust — not just the harness fuzz.** That includes the shared Solana build
(§5.1): `cargo build-sbf` on *user-supplied* program source (and, for a Prover-style backend, on
*LLM-munged* source) runs that source's `build.rs`/proc-macros natively too. So the sandbox wraps
`cargo build-sbf`, `cargo build` (harness), and `crucible run` alike. The sandbox must guarantee, at
minimum:

- **No network** (blocks exfiltration and the cloud metadata endpoint alike);
- **A clean, secret-free environment** — none of `ANTHROPIC_API_KEY` / `CERTORA` / `AWS_*` / `PG*`
  inherited; only benign build vars;
- **A minimal filesystem** — a scratch workdir (rw) holding only the step's inputs (the program
  source crate for a build step; the harness crate + built `.so` + IDL for a fuzz step), nothing else
  of the host;
- **Resource caps + a supervisor-enforced wall-clock kill** (CPU / memory / pids / disk);
- **An offline, vendored build** — the *backend* (not the LLM) owns `Cargo.toml` and forbids
  `build.rs`, so the dependency set is fixed and pre-vendored, and `cargo build` runs with no
  network (this also closes the build-time supply-chain vector).

**Mechanism — decided direction (built last, in §9 Phase 6; required for done).**

- **Linux (production + CI):** **[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`)** —
  unprivileged user/mount/net namespaces, no daemon and no added privilege, so it works *inside* the
  existing container without weakening it (`--unshare-net`, ro binds, tmpfs, `--die-with-parent`, plus
  seccomp + rlimits). If a stronger boundary is later warranted for untrusted native code, escalate to
  a per-run gVisor/Kata workload (e.g. a K8s Job) behind the same seam — explicitly *not* privileged
  Docker-in-Docker or a mounted `docker.sock`, both of which trade away the outer boundary.
- **macOS (developer machines only):** `bwrap` does not exist on macOS, so a **separate mechanism is
  needed** — either Seatbelt (`sandbox-exec` profile) or, more simply, run the Linux `bwrap` path
  inside a Linux VM/container (Docker Desktop / Colima / Lima). To be chosen when the sandbox is built.

The sandbox sits entirely behind the `RunCommand` effect (§7.2): `RealEffects` launches `program`+`args`
in the sandbox instead of directly, so nothing in the Rust decider, the driver, or the ABI changes when
it lands. That seam is what lets it be **built last** (§9 Phase 6) without disturbing the earlier
phases — but it is **required, not optional**: the backend is not considered done until *every*
`RunCommand` invocation is sandboxed, and until then the backend may run only in a trusted, offline
environment on trusted input (the gate scenario, §8), never on an untrusted program. See the
definition of done in §9.

---

## 8. The gate

Mirror the Solana front-half gate ([tests/test_solana_gate.py](../tests/test_solana_gate.py)) and the
autoprove end-to-end test, but run the **real Crucible backend** on the existing
[`solana_vault`](../test_scenarios/solana_vault/) Anchor scenario (it already has three instructions
and a clear invariant — balance = deposits − withdrawals, only-authority-withdraws, no underflow):

1. **Build + dry-run gate (cheap, no LLM):** provision `crucible` + the sBPF toolchain; assert
   `prepare_system`/`prepare_formalization` build the `.so`, generate the IDL, author a fixture, and
   pass `crucible run --dry-run`. This alone validates §6 + §7.1.
2. **Full live gate (expensive):** real LLM authoring loop → for each component, a compiling Crucible
   test that runs to a bounded timeout; assert at least one property is expressed and the pipeline
   reaches a report. Bonus signal: seed a *known bug* into a `solana_vault` variant (e.g. an
   unchecked withdraw) and assert Crucible **refutes** the relevant property (a `[FUZZ_FINDING]`), and
   `crucible tmin` minimizes it — the fuzzing analog of a prover CEX.

Provisioning notes carry over from prior gates: `env -u CERTORA`, testcontainers Postgres, a
deterministic embedder. New: the Solana build toolchain + `crucible` binary must be on `PATH` in the
gate environment (§6). Because fuzzing is nondeterministic, gate on *"a violation was found for the
seeded bug within N seconds"* with a generous budget, not on an exact crash sequence.

---

## 9. Phased plan

Each phase has a concrete gate, in the style of [ecosystem-abstraction.md §10](./ecosystem-abstraction.md).

1. **Toolchain + preconditions + build/IDL pre-work.** `validate_preconditions` (§6) including
   version resolution (§6.1) — a `--crucible-version`, detection of the program's Solana/Anchor
   toolchain, and the compatibility-table lookup that pins the harness manifest; the `prepare_system`
   build-`.so`-and-IDL step (via `crucible-idl-gen`); the general `RunCommand`/`CommandResult` ABI +
   `Effects` addition (§7.2) with a Python `RealEffects` runner (exec-not-shell, path-confined,
   semaphore-bounded). **Gate:** given `solana_vault`, the pipeline resolves a concrete version combo,
   builds the `.so`, emits an IDL, and a hand-written trivial fixture passes `crucible run --dry-run` —
   no LLM, no property authoring.
2. **Deliverable model.** Decide + implement §7.1 (recommend option (a)); a Crucible `ArtifactStore`
   / layout that writes a *compilable* crate. **Gate:** a hand-authored fixture + one hand-authored
   invariant test, written through the store, compiles and `crucible run <test> --dry-run` passes;
   `certora/crucible/` metadata + `fuzz/<program>/` deliverable coexist with any EVM outputs.
3. **The `prepare_formalization` authoring loop (shared fixture/actions).** The LLM IoC decider that
   authors the fixture + `action_*` from `SolanaApplication`, gated by `--dry-run`. **Gate:** on
   `solana_vault`, the agent authors a fixture whose `setup()` + all three actions succeed
   (`--dry-run` green) with no human edits.
4. **`formalize` per-component tests + verdicts.** Per component, author one test, run bounded fuzz,
   `fetch_verdicts` off the result (§5.3–5.4); `finalize` assembles the crate (§5.5). **Gate:** the
   full live gate (§8.2) — every component yields a compiling test that runs to timeout; the seeded
   bug is refuted and `tmin`-minimized.
5. **Package as the `crucible` application.** The Rust wheel's `AppDescriptor`
   (`ecosystem="solana"`, phases, args, event kinds, layout) + `export_app!`; register with the host;
   TUI + console entry points. **Gate:** `console-crucible <project> <program> <system.md>` runs the
   whole vertical from the CLI with zero bespoke Python beyond the wheel (still restricted to trusted
   input until Phase 6 lands).
6. **Sandbox every `RunCommand` (required — §7.4).** Move all command execution (`cargo build-sbf`,
   `anchor idl`, `crucible run`) behind the sandbox in `RealEffects`: Linux `bwrap` with network-off,
   a clean/secret-free env, a minimal bind-mounted workdir, resource caps + wall-clock kill, and an
   offline vendored build; pick the macOS dev mechanism (§10 Q2). **Gate:** an *escape test* — a
   harness whose `setup()` / `build.rs` attempts to read a planted secret env var, open a host file
   outside the workdir, and reach the network (incl. `169.254.169.254`) — and assert every attempt is
   denied while the legitimate `solana_vault` gate (§8) still passes unchanged. Only after this gate is
   green may the backend run on untrusted input.
7. **Polish (optional).** Report nouns (§7.3); coverage surfacing (`--coverage`/LCOV as a report
   attachment); stateful-mode tuning; crash-artifact rendering in the frontend.

**Definition of done.** The task is *not* complete until Phase 6 is green: every command run via the
`RunCommand` effect is executed inside the sandbox, verified by the escape test. Phases 1–5 may run
beforehand, but only in a trusted, offline environment on trusted input; the backend must not be
pointed at an untrusted program until the sandbox is in place. (Phase 7 is genuinely optional and may
trail.)

---

## 10. Open questions

1. **Unit granularity for a fuzzer.** The `SOLANA` ecosystem's `units` are per-instruction, but a
   Crucible invariant is inherently *cross-instruction* (checked over a random action sequence). Do
   we (a) keep per-instruction components, each authoring one test that drives the full sequence but
   asserts that component's properties, or (b) collapse to a **single whole-program harness** (units
   → one)? (a) reuses the fan-out/caching and matches Crucible's feature-per-test structure; (b) is
   more natural for global invariants but discards per-component parallelism. This is exactly
   [ecosystem-abstraction.md §11 Q1](./ecosystem-abstraction.md); recommend (a) first, with a
   `render_unit` hook if per-unit shape diverges.
2. **Sandbox specifics (required work — §7.4, §9 Phase 6).** The isolation *requirements* and the
   Linux mechanism are decided — `bwrap`, network-off, clean env, offline vendored build, behind the
   `RunCommand` seam — and building it is an in-scope, definition-of-done phase (not deferrable). Two
   choices remain open within that phase: (a) the **macOS dev mechanism** (`sandbox-exec` vs running
   the `bwrap` path inside a Linux VM), and (b) whether production eventually needs a **stronger
   boundary** than `bwrap` (a gVisor/Kata per-run workload) for genuinely untrusted programs.
3. **Fixture authoring difficulty.** `setup()` for real DeFi programs is hard (init order, admin
   whitelists, account patching — the Harness Guide is a long playbook). Is a single authoring pass
   enough, or does `prepare_formalization` need its own multi-round refine loop (like the bug-round
   loop) driven by `--dry-run`/short-explore feedback? Budget for iteration; measure on the gate.
4. **Nondeterminism in verdicts + caching.** A clean fuzz run is "no bug found in N seconds," not a
   stable fact — re-running may differ. How do we cache a `formalize` result keyed by the property
   batch when the *verdict* isn't deterministic? Likely cache the authored harness (deterministic)
   but re-fuzz on demand, or record the seed (`--seed`) for reproducibility. Decide in Phase 4.
5. **Whole-crate deliverable vs the host's single-file layout.** §7.1 (a) vs (b) — a real fork in how
   much the generic Rust-app host must grow. Prototype (a) in Phase 2 before committing.
6. **Coverage as a first-class signal.** Crucible emits LCOV and edge counts. Should low coverage
   *downgrade* a GOOD verdict (i.e. "held, but the fuzzer barely explored this path")? Powerful but
   out of scope for v1; note it and defer (Phase 6).

---

## 11. Key files

| Concern | File |
|---|---|
| The backend seam (the contract to implement) | [docs/formalization-abstraction.md](./formalization-abstraction.md) · [composer/pipeline/core.py](../composer/pipeline/core.py) |
| The Rust-app framework (backend in Rust) | [docs/rust-applications.md](./rust-applications.md) · [docs/rust-formalization-backends.md](./rust-formalization-backends.md) |
| The Rust SDK ABI to extend (§7.2) | [rust/autoprover-sdk/src/lib.rs](../rust/autoprover-sdk/src/lib.rs) |
| The Python host / IoC loop / adapter to extend | [composer/rustapp/host.py](../composer/rustapp/host.py) · [composer/rustapp/loop.py](../composer/rustapp/loop.py) · [composer/rustapp/adapter.py](../composer/rustapp/adapter.py) |
| The Solana ecosystem (reused front half) | [composer/spec/solana/model.py](../composer/spec/solana/model.py) · [composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py) · `composer/templates/{rust,solana}/…` |
| Closest backend precedent (local-CLI, refutation) | [composer/foundry/pipeline.py](../composer/foundry/pipeline.py) · [composer/foundry/runner.py](../composer/foundry/runner.py) |
| The Crucible backend crate (new) | `rust/crucible-app/` |
| Scenario + gate | [test_scenarios/solana_vault/](../test_scenarios/solana_vault/) · `tests/test_crucible_gate.py` (new) |
| Crucible itself (docs) | `/home/eric/src/crucible/docs/` (harness-guide, writing-tests, cli-reference, remote-fuzzing) |
