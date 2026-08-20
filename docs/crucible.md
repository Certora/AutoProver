# Crucible

AutoProver's Solana verification application. It authors
[Crucible](https://github.com/asymmetric-research/crucible) fuzz harnesses for a Solana
program and gates them with the local `crucible` CLI.

The application is `ecosystem="solana"` plus a Rust backend wheel (`rust/crucible-app`),
consumed by the generic Python host. There is no Crucible-specific Python package: the
entry points (`console-crucible`, `tui-crucible`) are the same two-line shims any Rust
app uses. How a wheel talks to the host is
[rust-applications.md](./rust-applications.md); how Solana is modelled and split into
units is [ecosystem-abstraction.md](./ecosystem-abstraction.md); how to run it is
[crucible-demo.md](./crucible-demo.md).

```
program ─analyze─▶ SolanaApplication ─extract─▶ properties ─formalize─▶ fuzz harness ─verdicts─▶ report
         (solana ecosystem)                      (this wheel: one crate, one campaign per check)
```

A verdict is **refutation-oriented**, like Foundry: a crash *refutes* a property (`BAD`);
a clean campaign that actually evaluated the assertion is `GOOD` — no violation found
within the budget, not a proof.

---

## 1. What Crucible is, as a checker

Crucible is a coverage-guided fuzzer for Solana (LibAFL + LiteSVM). The artifact is a
Rust **fuzz-harness crate**, not a spec file:

- a `#[derive(Clone)]` **fixture** with `setup()` (loads the program `.so`, creates
  accounts, runs init) and one `action_*` per instruction;
- one `#[invariant_test]` per component, checked after every action;
- assertions via `fuzz_assert*!` (they record a violation instead of aborting);
- invoked as `crucible run <program> <feature> --release --timeout <secs>`.

The CLI hardcodes the binary name `invariant_test`. A component is therefore selected
by a **Cargo feature** whose name equals the crate-root test fn — not by a second
`[[bin]]`. `--dry-run` compiles and runs one iteration. There is no run service and no
run link.

The fuzzer drives **random sequences of the whole program's actions**. A component is an
authoring and attribution scope over that whole-program execution, not a narrower
execution scope. Interesting violations are cross-instruction; you cannot (and should
not) fuzz "just the deposit component."

---

## 2. Units

`SOLANA.units` returns one `SolanaComponentInstance` per `ProgramComponent` of the
**main** program — the Solana analog of EVM's `ContractComponent`. The split lives on
the ecosystem axis, so every Solana backend inherits it.

A component is a named capability produced by system analysis ("deposits and share
accounting", "admin/config"), not an instruction and not the whole program. It
*references* instructions and account types; it does not own them.
`SolanaProgram.instructions` stays the flat source of truth. An instruction may appear
in more than one component.

Validation (`_solana_validate`) requires unique component names and slugs within a
program, resolvable interactions, and a **valid and total** component↔instruction
mapping (every named instruction exists; every instruction is claimed by at least one
component). Overlap is allowed.

Property extraction is per component. The Solana property prompt is backend-neutral;
fuzzer-specific framing lives in this wheel's `backend_guidance.j2`.

---

## 3. A run

The descriptor synthesizes these phases. Preflight overlaps analysis; the fixture is
authored after extraction, from the union of every component's properties.

| Phase | Role | What happens |
|---|---|---|
| Design Doc Discovery | `Discovery` | If `system_doc` is omitted |
| Build Preflight | `Preflight` | Build the program `.so` (+ IDL if needed); dry-run a wheel-authored skeleton |
| System Analysis | `Analysis` | `SolanaApplication` + components |
| Property Extraction | `Extraction` | One agent per component |
| Harness Fixture | `Setup` | Author the shared fixture + actions, once |
| Harness Authoring | `Formalization` | Per component: author the section, dry-run, fuzz, verdicts |
| Report | `Report` | Shared report assembly |

CLI flags this wheel actually threads through:

- `--fuzz-timeout` — per-campaign budget in seconds (default 60).
- `--program-idl FILE` — force the IDL type path (any Anchor IDL format).

Declared event kinds: `fuzz_pulse`, `fuzz_finding`, `build_output`, `judge`, `verdict`.
They are emitted per run (a pulse before each campaign, a finding on a counterexample,
build output on compile/dry-run), not streamed mid-subprocess.

Toolchain runs are **serialized** (`serialize_toolchain: true`): every campaign shares
one crate and one `target/`. `crucible run` always executes `invariant_test`, so
per-crate isolation would rebuild the heavy graph and collide on that binary name.

---

## 4. The harness crate

One crate per program, assembled by this wheel, under the deliverable tree rather than
Crucible's default `./fuzz/`:

```
certora/crucible/fuzz/<program>/
  Cargo.toml              [[bin]] name = invariant_test; [features] one per target
  rust-toolchain.toml     pins host `stable` (the CLI forces that channel)
  .gitignore              `target/`, corpus, crashes — ~900 MB, not committed
  src/preflight.rs        wheel-authored skeleton; record of the preflight, not a later target
  src/main.rs             fixture + one gated entry per component
  src/c_<slug>.rs         that component's authored `pub fn invariants`
idls/<lib>.json           only on the IDL path
```

Metadata (`properties.json`, commentary, property→checks) lands under
`certora/crucible/`. Reports under `certora/crucible/reports/`. The fuzzer's
unbounded corpus and crash dirs live under `.certora_internal/crucible/`, which the
host withholds from source tools.

`crucible run` chdirs into the harness, so every path the crate itself names — the
program `.so`, a path dep on the program crate — is relative to it. The crate root
declares `const PROGRAM_SO`; the fixture prompt asks for that name.

### Names

Two namespaces, disjoint by construction:

| | Scaffolding | One component |
|---|---|---|
| Cargo feature / `crucible run` selector | `preflight` | `c_<slug>` |
| Crate-root `#[invariant_test]` fn | `preflight` (inline) | `c_<slug>` (delegates into the module) |
| Source file | `src/preflight.rs` | `src/c_<slug>.rs` |
| Authored fn inside the module | — | always `invariants` |

The host owns the slug (`slugify_filename`). The wheel only spells it as a Rust
identifier (`ident_of`: lowercased, non-`[a-z0-9_]` → `_`) and prefixes `c_`. A
component named "preflight" is `c_preflight`, not the scaffolding target.

The Crucible macro self-gates `main()` by `fn` name == feature. The load-bearing rule
is that those two strings match. The author never names the varying identifier: they
write `pub fn invariants`; the wheel generates the crate-root entry.

### Who writes which file

`crate_root` (and the setup compile, which holds the same two inputs) writes the
manifest and `src/main.rs` **once**, from the authored fixture plus the whole unit set.
Every unit gets a feature and a `#[cfg]`-gated `mod`, including units that will later
give up — a `#[cfg]`-disabled `mod` is stripped before rustc resolves the file.
`finalize` only writes section files: an authored suite, or a `compile_error!` naming
why there is none. A failing test there would look like a counterexample against the
user's program.

A per-component compile or validate therefore materializes **only that section**. The
gated build *is* the delivered crate with one feature selected, not a separately
assembled lookalike.

The preflight gate is a crate of exactly one file (`src/preflight.rs`). It runs before
analysis, so nothing that belongs in `src/main.rs` exists yet; writing that path early
would leave a half-crate at the deliverable root if the run died. From setup on, the
manifest points at `src/main.rs`. `preflight` survives as a user-runnable sanity check
(`crucible run <program> preflight --dry-run`): the fixture compiles and the program
loads. Its body is inline in the crate root, so it cannot drift from what the gate ran.

---

## 5. Crate path and IDL path

Where the harness gets the program's Anchor types is decided **once**, in
`workspace_prep`, and then held as a prep fact so every later render agrees.

| | Crate path | IDL path |
|---|---|---|
| Types from | a path dep on the program crate | `declare_fuzz_program!` over its IDL |
| Extra deps | — | `crucible-idl-gen`, `bytemuck`, `ctor` |
| Requires | the program's Anchor major == the harness's (`1.0.1`) | an IDL (any Anchor format) |
| Fixture writes | `use <lib>::*` | `use <lib>::*` (the generated module is named for the crate's lib target) |

The program's own `anchor-lang` requirement forces the choice. Anchor's generated
`InstructionData` / `ToAccountMetas` impls belong to the exact `anchor-lang` that
produced them, so a 0.29 program cannot satisfy `crucible-test-context`'s 1.0.1 trait
bounds — and the two Solana graphs often cannot even co-resolve (Solana 1.17 pins
`ahash =0.8.5`; `libafl 0.15` needs `^0.8.11`). Under the IDL path the program's graph
is never resolved; the generated types belong to *our* `anchor-lang`. LiteSVM still
loads the built `.so` either way.

`--program-idl` supplies an IDL and forces that path. Without an IDL the run can
produce, prep fails fast rather than dying in a later build. The host fills a missing
program address (Anchor 0.29's `idl build` writes none) from `Anchor.toml`'s
`[programs.<cluster>]`, else from a lone `declare_id!`. Several `declare_id!`s and no
manifest entry is an error.

The `.so` is named after the crate's **lib target**, not the analysis identifier. The
fixture loads `target/deploy/<lib>.so`.

On the IDL path, an absent optional account must be the program id, not `None`.
Anchor's own derive emits the program id for a `None`; `crucible-idl-gen` does not. A
`None` compiles, dry-runs, and only fails once a campaign draws the action, so
`compile` rejects it before the build.

---

## 6. Toolchain

`validate_preconditions` is a cheap, synchronous filesystem check. It collects every
problem rather than returning the first:

- `crucible`, `cargo-build-sbf`, and `anchor` on `PATH`;
- a `Cargo.toml` at the project root;
- a `Cargo.toml` inside the program crate the host resolved
  ([`composer/spec/cargo.py`](../composer/spec/cargo.py) — a workspace member's
  directory, package name, and analysis identifier need not agree);
- `CRUCIBLE_REPO` pointing at a checkout, because the harness path-depends on
  Crucible's crates.

Two builds, two toolchains:

- The **program `.so`** is an sBPF build (`cargo build-sbf` / `anchor build`) using
  Solana's platform-tools rustc, driven by the project's own pin.
- The **harness** is a *native* build. The CLI forces `RUSTUP_TOOLCHAIN=stable`, so
  the generated crate ships its own `rust-toolchain.toml` pinning that channel —
  otherwise the project's pin would claim every cargo invocation in that directory.

The harness pins a fixed stack (`anchor-lang 1.0.1`, `solana 3.0`, `libafl 0.15.1`,
host `stable`). A program on another Anchor major takes the IDL path (§5). A program
whose sBPF toolchain is older than the image still needs its era of `cargo-build-sbf`
and `anchor` on `PATH`; the harness build does not.

`workspace_prep` warms **both** the harness dir and the program crate into the run's
private `CARGO_HOME`. Under the IDL path the harness has no path dep on the program,
so nothing would otherwise fetch the program's graph, and the later confined
`cargo-build-sbf` would die offline. The host fetches unconfined and builds
confined + offline.

Building the `.so` and placing the IDL is a shared Solana capability
([`composer/spec/solana/build.py`](../composer/spec/solana/build.py), requested through
`autoprover-solana`), not a Crucible-only step.

The container image (`scripts/Dockerfile.crucible`) bakes one blessed combo on top of
the lean AutoProver base — host Rust, platform-tools, `anchor`, `crucible` — so a run
can happen entirely inside the container. Bumping the combo is bumping the image ARGs.

---

## 7. Authoring

The wheel is a passive `Backend`. Python owns the loop; Rust decides prompts, file
contents, argv, and verdicts. The LLM authors only file *contents*. The decider
chooses paths and constructs every command line.

### Shared fixture (setup)

Authored **once**, after extraction, from the **union of every component's
properties**. That is `StagedFormalizer.begin`: the fixture decides which `action_*`
exist, and therefore which properties are checkable at all. A first-component-wins
authoring would leave the rest with honest `// UNCOVERABLE` comments.

The fixture:

- loads the `.so`, creates accounts, runs init in dependency order;
- exposes one `action_*` per instruction the properties need, with `#[range(..)]`
  bounds;
- for a negative / "must be rejected" property, **attempts** the call and **records**
  the outcome on `pub(crate) accepted: Accepted` (`|=` so one wrong acceptance
  sticks). It does **not** assert. A tagged `fuzz_assert*!` in an `action_*` (or any
  `[<title>]` message in the fixture) would fire in every component's campaign and
  mis-attribute (§8).

`setup()` may panic: that is the harness failing, not a property. An `action_*` that
panics on fuzzed input (`Pubkey::find_program_address` with a derived seed, an
`unwrap` on a derivation) aborts the process outside the fuzz target; LibAFL records
no crash, every property in the campaign returns `ERROR`, and a shared corpus spreads
the input to later components. The cheat sheet forbids that class; there is no
fixture judge.

Gate: `crucible run <program> preflight --release --dry-run` against the authored
fixture in the real crate root. Compiles + `setup()` runs once. The setup session
has no properties to review, so `judge` returns `None`.

### Per-component section

Each component authors `src/c_<slug>.rs` containing `pub fn invariants(f: &mut Fixture)`.
The section:

- asserts standing relations against **on-chain** state (`read_anchor_account`), not
  local mirrors;
- tags every assertion `[<property title>]` — that string is how a finding is placed;
- for a rejection property, asserts on the fixture's recorded `accepted.<attempt>`;
- if the fixture has no action for a property, writes `// UNCOVERABLE:` rather than
  a vacuous assertion.

The section file is `#[cfg(feature = "c_<slug>")]`-sealed. One component's helpers
are not visible to another's. The wheel strips a stray `#[invariant_test]` (it would
expand `main()` inside the module) and makes `invariants` `pub`.

### Judge

Component suites are reviewed **in the author loop**, the way Foundry's
`feedback_tool` works. The wheel supplies `judge` + `judge_instruction`; the host
binds a `request_review` tool and refuses `result` until the current draft was
accepted (or the review budget is exhausted — then the draft goes forward with the
open concerns recorded). The compile gate and the fuzzer still run. The wheel API
does not change; `echoprover` (no judge) stays a single-shot author.

---

## 8. Checks, campaigns, and verdicts

The shared check contract is [rust-applications.md §6](./rust-applications.md). This
backend's unit of evidence is **one tagged assertion**, so it is stricter than the
seam's many-to-one allowance.

### Declaration

The author declares the property→check mapping (`map_checks`). The wheel answers only
`target_for`, and it answers `None`: **every check is its own campaign**
(`c_<property slug>`), because a campaign covering several would spend its budget shaped by
whichever check it refuted first and report the rest as though nothing had been found against them
([per-check-targets.md](./per-check-targets.md)). A component is a *module*, not a build target.

Two shapes are refused in `validate` *before* a campaign is spent: a check that maps several
properties (unattributable however long it ran), and a check named anything but what its property
states (the crate has no feature for it, so `crucible run` would answer about a missing feature
instead).

A verdict names the section file its assertion was written into (`c_<slug>.rs`). The
report keys a rule row by `(file, name)`; without the file, two components whose
authors named the same invariant would collapse into one row.

### A campaign

`validate` runs:

```
crucible run <program> <feature> --release
  --corpus-in/--corpus-out .certora_internal/crucible/corpus
  --crashes-out .certora_internal/crucible/crashes
  --timeout <budget> --coverage
```

It does **not** pass `--mode explore`. That preset also turns on `--stop-on-crash`,
which would end the campaign at the first finding. The host's `Target.stakes` decides
that instead (`Feedback` adds `--stop-on-crash` and caps the budget at
`FEEDBACK_BUDGET_S`; a stamping run is `OfRecord` and spends `--fuzz-timeout`).

`--coverage` requires `--timeout`. Without a timeout Crucible treats `--coverage` as
replay-only and fuzzes nothing. LCOV is preserved per component as the campaign
emits it; a leftover file in the shared harness dir would be published as the next
component's coverage.

### Attribution

Every assertion is tagged `[<property title>]`. A finding is classified three ways
against `AuthorInput.run_props` (every title the run extracted):

| Finding's title | `OfRecord` | `Feedback` |
|---|---|---|
| one of **this** target's checks | that check `BAD`; the rest stay `GOOD` (subject to the tally) | that check `BAD`; the rest `UNKNOWN` |
| **another component's** | left alone — it cost one test case, not the campaign | `UNKNOWN`, detail naming the owner |
| **unknown to the run** | all `BAD` | all `BAD` |

The last row is the safety net: never silently pass a real counterexample. A title
that belongs elsewhere is not the same thing as an unplaceable one.

Crucible dedups crashes by action-variant sequence, so two properties refuted along
the same sequence of action *types* still yield one finding.

### A clean campaign is not a passing check

`fuzz_assert!` records only a violation. Silence means *no counterexample among the
states explored*, which is weaker than `GOOD` ("the property holds"). A typical
Solana invariant is guarded (`if let Some(vault) = f.read_vault()`); if
`action_initialize` is always rejected the vault never exists, the assertion never
runs, the campaign exits 0, and coverage numbers (program edges, not assertion
sites) look healthy.

The crate root interposes counting wrappers on every `fuzz_assert*!`
(`templates/tally_macros.j2`). Authored code reaches those names only through globs
that funnel through the crate root, so every call site expands the wrapper. Each
site prints `[FUZZ_TALLY]` at powers of two. `tally::gate` keeps a `GOOD` only when
some title the check claims has a count above zero (and the row says "evaluated at
least N times"); otherwise it becomes `UNKNOWN`. A section that path-qualifies
around the wrapper loses its tally and cannot earn `GOOD`.

An assertion that *is* evaluated and is vacuous anyway (`unwrap_or_default()` then
`0 >= 0`) is the judge's residue.

### What a row says

Every verdict — including green ones — is annotated with what the campaign spent:
component name, budget vs wall clock, executions, and the last pulse's
reached/total edges and branches. A `GOOD` from a twelve-second campaign is not the
same claim as one that ran to a ten-minute budget; the report has nowhere else to
say so.

A non-zero exit with no finding and no build markers is `ERROR`, with a reason
taken from how the campaign *stopped* (a panic on fuzzed input is the usual case),
not from the build log's tail.

---

## 9. What a property can check

A Crucible property is a predicate over on-chain account state, evaluated
**between** actions. It cannot see which instruction just ran, cannot compare
against the state before it, and cannot see inside a transaction. The property
author is told this *before* the fixture exists (`backend_guidance.j2`), because
the fixture is authored from these properties.

- Prefer the **standing relation** a per-call delta is protecting. "After
  `switch_group` the obligation is stale" is not observable; "an obligation marked
  fresh has every reserve it references fresh" is.
- **Name the account fields** that decide it. A quantity the program computes and
  never stores leaves nothing to read.
- A rejection or liveness claim must **name the exact attempt**. A correctly
  rejected instruction and one that was never sent leave identical state. The
  named attempt becomes a fixture action that records `accepted.<attempt>`.
- Constrain what the **program** writes, not what the harness writes. A bound the
  action's own `#[range]` already keeps holds under every implementation.
- A Rust panic and a returned `Err` are indistinguishable afterwards (both roll
  back). An intra-transaction excursion the same transaction restores is invisible.

Skip off-chain events and pure hash-collision resistance. Arithmetic overflow and
Anchor constraint failures surface as crashes and are worth stating.

---

## 10. Sandbox

The LLM-authored harness is a **native** binary. LiteSVM sandboxes the program
`.so`, not the fixture, the actions, or `build.rs`. Every compile or run of
untrusted Rust — `cargo build-sbf`, the harness `cargo build`, `crucible run` —
goes through the shared command sandbox
([command-sandbox.md](./command-sandbox.md)): Landlock + seccomp + env allowlist +
rlimits, behind `run-confined`.

This wheel sets `confine_by_default: true` and adds read-only grants for
`CRUCIBLE_REPO` and the `crucible` binary's directory. If the launcher cannot be
established the run fails closed rather than executing untrusted native code
unconfined. macOS is not supported; a Mac developer runs inside the Linux
container.

---

## 11. Knowledge

Authoring needs Crucible-specific knowledge the base model will not reliably have.
Two layers:

1. **Static injection** — a harness cheat sheet, a test cheat sheet, a worked
   example fixture, and a "PROGRAM API FACTS" block (crate id, `declare_id`, each
   instruction's names / args / accounts) in the setup prompt.
2. **`crucible_kb` RAG** — the committed manifest
   [`rust/crucible-app/crucible_kb.rag.json`](../rust/crucible-app/crucible_kb.rag.json)
   (126 sections), imported by the generic
   [JSON RAG mechanism](./rag-import-format.md). `composer/tools/crucible_rag.py`
   exposes keyword / vector / get-section tools. Absent an embedder, the run
   degrades to the cheat sheet.

Knowledge is per-backend (`AppDescriptor.rag_db_default`), not per-ecosystem.

---

## 12. Key files

| Concern | Where |
|---|---|
| The wheel | [`rust/crucible-app/`](../rust/crucible-app/) |
| Prompt / crate templates | [`rust/crucible-app/templates/`](../rust/crucible-app/templates/) |
| Solana types the wheel and host share | [`rust/autoprover-solana/`](../rust/autoprover-solana/) |
| Host / IoC loop | [`composer/rustapp/`](../composer/rustapp/) |
| Entry points | [`composer/crucible_launch.py`](../composer/crucible_launch.py) |
| Solana ecosystem | [`composer/spec/solana/`](../composer/spec/solana/), [`composer/pipeline/ecosystem.py`](../composer/pipeline/ecosystem.py) |
| Shared Solana build | [`composer/spec/solana/build.py`](../composer/spec/solana/build.py) |
| Knowledge | `crucible_kb` · [`crucible_kb.rag.json`](../rust/crucible-app/crucible_kb.rag.json) · [`composer/tools/crucible_rag.py`](../composer/tools/crucible_rag.py) |
| Scenarios + gates | [`test_scenarios/solana_vault/`](../test_scenarios/solana_vault/), [`test_scenarios/solana_vault_idl/`](../test_scenarios/solana_vault_idl/), `tests/test_crucible_*` |
| How to run it | [crucible-demo.md](./crucible-demo.md) |
