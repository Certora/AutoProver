# Crucible unit granularity: per-instruction vs whole-program (global) fuzzing scenarios

> **Status: implemented (commit fd51700), then collapsed to a single harness (prototype).**
> Solana first used global extraction with per-invariant fuzz units (recommendation §6). It now
> **collapses all invariants into one whole-program harness + run** — the "single whole-program
> unit" option §3/§6 identified and deferred *for attribution reasons that finding-level
> observability now covers* (each `fuzz_assert` is tagged with its property title, so a
> counterexample names the property it refutes). This is the `Ecosystem.collapse_units` path
> (`composer/pipeline/ecosystem.py` → `SOLANA`; `_extract_all` in `core.py`) with a single
> `c_invariants` fn (`rust/crucible-app/src/lib.rs`). Trade-off realized: one build + one fuzz run
> for the whole program (vs N). Attribution is preserved: each property is still its own report
> row (`c_<slug>`) sharing the single fuzz `target` (`Unit.target` in the SDK), and the host
> (`_attribute_run`) pins a counterexample to the property the finding names (each assertion is
> tagged with its property title), holding the rest GOOD — so a single run yields per-property
> verdicts. Measured e2e: **0:59:22** for the whole vault (13 invariants), vs 1:33:20 for the
> per-invariant in-loop run and 1:53:04 for the original — one build + one campaign instead of 13.
> The rest of this note is the original decision record.

Design note for [crucible-application.md §10 Q1](./crucible-application.md) — "are we
using the right unit granularity for a fuzzer?" It describes moving Crucible's
scenario generation from **per-instruction** units to a **whole-program (global)**
context, the trade-offs, and how each option lines up with the Foundry and CVL/prover
backends. No code change yet — this is to decide the shape.

## 1. Background: what a "unit" is in each backend

The shared driver (`composer/pipeline/core.py`) fans out over
`ecosystem.units(main)`, and for each unit runs **property extraction** (with that
unit's context) → **formalization** (author + gate an artifact) → a per-unit
**verdict** in the report. What a "unit" *is* differs by ecosystem:

| Backend | Unit (`ecosystem.units`) | Granularity | Source |
|---|---|---|---|
| CVL / prover (EVM) | `ContractComponentInstance` | per **component** — a semantic cluster of the contract's behavior, produced by system analysis | `_evm_units` |
| Foundry (EVM) | `ContractComponentInstance` | per **component** (same as CVL) | `_evm_units` |
| **Crucible (Solana)** | `SolanaInstructionInstance` | per **instruction** — one unit per entry point, a flat list, **no grouping** | `_solana_units` |

So EVM already does a coarsening step — the analysis groups related functions into a
handful of named `ContractComponent`s (`composer/spec/system_model.py:61`). Solana does
**not**: `SolanaProgram.instructions` is a flat list (`composer/spec/solana/model.py:99`)
and `_solana_units` emits one unit per instruction (`composer/pipeline/ecosystem.py`).
Crucible therefore sits at the *finest* granularity of the three.

## 2. What is already global today (important)

A subtlety that reframes the question: **the Crucible harness is already
whole-program.** The setup session authors one shared `Fixture` with `action_*`
methods that drive the *entire* program, and the fuzzer runs in `explore` mode —
`crucible run <program> <feature> --mode explore` drives a **random sequence of
actions across all instructions** and evaluates the test after each step
(`rust/crucible-app/src/lib.rs`, `TEST_CHEAT_SHEET`).

What is *per-instruction* today is only:

1. **Property selection** — `run_property_inference` is called with a single
   `SolanaInstructionInstance` as context, so the model proposes properties framed
   around *that instruction* (`composer/pipeline/core.py` `_extract_all`).
2. **The authored test** — one `#[invariant_test] fn c_<slug>` per instruction,
   asserting that instruction's properties (against global state, over the global
   action sequence).
3. **The verdict / report row** — one GOOD/BAD per instruction.

So the fuzzing *engine* is already global; the *authoring and bookkeeping* are
sharded per instruction. The open question is really: **should property generation
and the harness be framed globally, or kept sharded per instruction?**

## 3. The proposed change: global (whole-program) scenarios

Generate properties and harnesses from a **whole-program view** — the model sees the
entire program (all instructions, accounts, PDAs, authorities, cross-instruction
flows) at once and proposes **global invariants** ("total deposits always equal vault
balance", "only the admin can ever change the fee", "no sequence of actions drains a
user's escrow") rather than instruction-local properties. The fuzzer explores action
sequences (as it already does) and checks these invariants after every step.

Concretely, in the current architecture this is a small set of localized changes:

- **`ecosystem.units` for Solana** returns **one whole-program unit** (the
  `SolanaProgramInstance` itself), instead of one per instruction. The driver then
  fans out over a single unit.
- **Property extraction context** becomes the whole program (it already receives the
  analyzed model as front-matter; the *unit* context widens from one instruction to
  all). The prompt shifts from "properties of this instruction" to "global invariants
  of this program".
- **Harness authoring** produces a set of global `#[invariant_test]` functions in one
  crate (the fixture is unchanged — it is already whole-program).
- **Verdict / report** granularity becomes per-**invariant** (or one program-level
  row) rather than per-instruction.

A middle option (call it **grouped**) mirrors EVM: have Solana analysis group
instructions into a few semantic **components** (like `ContractComponent`) and fan out
per component. That lands between per-instruction and whole-program.

There are thus three points on the spectrum:

```
per-instruction (today)   →   per-component (EVM-style grouping)   →   whole-program (global)
   finest, most fan-out            middle                                coarsest, one unit
```

## 4. Pros and cons

### Global (whole-program) — pros
- **Matches the tool's semantics.** A coverage-guided fuzzer explores the whole state
  machine; invariants are inherently cross-instruction. Framing them globally removes
  an artificial instruction boundary and lets the model express properties that *span*
  instructions (deposit-then-withdraw conservation, auth escalation across a sequence).
- **Fewer, higher-value properties.** Per-instruction extraction tends to produce
  near-duplicate or trivially-local assertions for each entry point; a global view
  yields a smaller set of meaningful system invariants (closer to how an auditor
  writes them).
- **No redundant re-fuzzing.** N per-instruction tests each fuzz the *same* global
  action space for the full budget; one global harness fuzzes it once, so the time
  budget buys deeper exploration instead of N shallow re-runs of the same sequence.
- **Cheaper authoring.** One harness authored/validated instead of N (each currently
  pays a compile + dry-run + fuzz cycle — the dominant cost).
- **Cleaner story for the shared-crate serialization.** Per-instruction units share one
  harness crate and therefore serialize their builds today (parity gap #4 /
  command-sandbox.md §10); with one unit there is nothing to serialize.

### Global — cons
- **Loses per-instruction fan-out and its parallelism/caching.** The driver's
  per-unit concurrency and result cache key off units; one unit means no per-unit
  parallelism (though today the shared crate already serializes builds, so little is
  lost in practice) and coarser caching.
- **Coarser attribution.** A per-instruction verdict tells you *which entry point* a
  counterexample implicates; a single global run says "some sequence violated invariant
  X" and leans on the counterexample trace for locality. Mitigated by keeping
  per-**invariant** units (still global context, but each invariant is its own report
  row) rather than collapsing to one.
- **Bigger single harness.** One crate with many invariants is a larger authoring
  target; a compile error blocks all of them (vs isolating a failure to one
  instruction's test today).
- **Diverges from the generic driver's per-component assumption.** The EVM backends
  fan out per component; making Solana emit one unit is fine (the driver is
  unit-agnostic) but the report/labels read "1 component", which is a cosmetic mismatch
  with the "instructions" framing.

### Per-instruction (today) — pros
- Reuses the generic fan-out, caching, and per-unit report rows unchanged; isolates
  authoring failures; gives instruction-level attribution.

### Per-instruction — cons
- The artificial boundary described above: duplicated/local properties, N× redundant
  fuzzing of the same global space, N authoring cycles, and it can't express
  cross-instruction invariants naturally.

## 5. Comparison to Foundry and CVL

| Dimension | CVL / prover | Foundry | Crucible today (per-instruction) | Crucible global (proposed) |
|---|---|---|---|---|
| Unit of fan-out | per component | per component | per **instruction** | per **program** (or per invariant) |
| Property framing | per-component rules | per-component; `test_*` per-function + `invariant_*` whole-contract | per-instruction | **whole-program invariants** |
| Execution model | symbolic — reasons over **all** states, no sequences | concrete: property fuzzing (per-function) **and** stateful invariant fuzzing (random call sequences, whole-contract) | concrete: `explore`-mode random action sequences (whole-program) | same engine, global framing |
| Cross-unit invariants | natural (any state) | natural for `invariant_*` (stateful) | awkward — split across instruction units | **natural** |
| Attribution on failure | the violated rule | the failing test / invariant | the instruction's test | the violated invariant (+ trace) |

The key alignment: **Foundry's `invariant_*` stateful fuzzing is already
whole-contract** — its runner calls all functions in random sequences and checks
invariants globally (`composer/templates/foundry_property_generation_system_prompt.j2`:
"An `invariant_*` function is run by foundry's *stateful* fuzzer"). Crucible's
`explore` mode is the direct analogue. So a **global** Crucible framing makes Crucible
match Foundry's *fuzzing* model, while the *authoring fan-out* (per component) is a
separate axis that Foundry keeps per-component mainly because it also emits
per-function `test_*` properties — which Crucible does not.

CVL/prover is the outlier: no action sequences at all (symbolic over all states), so
its per-component split is about *proof modularity*, not scenario construction — not a
useful precedent for how a fuzzer should be scoped.

Net: the fuzzing backends (Foundry stateful, Crucible) both *want* whole-program
scenario semantics; only Crucible currently imposes a per-instruction authoring shard
that neither the engine nor the Foundry precedent requires.

## 6. Recommendation

Move Crucible to a **global scenario context** for property generation and harness
authoring, but keep **per-invariant units** rather than collapsing to a single opaque
run — i.e. the model sees the whole program and proposes a set of global invariants,
and each invariant is a unit (its own harness fn + report row). This preserves
attribution and the report's per-row structure while fixing the artificial
instruction boundary and the N× redundant fuzzing.

Staging (each independently shippable):

1. **Widen the extraction context** to whole-program (prompt + the unit the driver
   passes to `run_property_inference`) while still emitting per-instruction units — a
   low-risk first step that improves property quality without changing fan-out.
2. **Switch `ecosystem.units` (Solana) to per-invariant** (extract global invariants
   first, then fan out over them), or to a single whole-program unit if per-invariant
   proves awkward. This is the `units` / `render_unit` hook §10 Q1 anticipated.
3. **Re-fuzz vs cache** (ties into §10 Q4): with one global action space, cache the
   authored harness (deterministic) and re-fuzz on demand / record the seed — more
   important now that a single budget covers the whole program.

Interactions to keep in view: per-invariant units mean N harnesses build + fuzz
serially on the shared crate (see §8 on why that's hard to parallelize), and it pairs
naturally with coverage-as-signal (§10 Q6).

## 7. Concurrency: why per-invariant fuzzing serializes (and the only clean fix)

With N per-invariant harnesses the build+fuzz runs serialize (a single build semaphore
in `CrucibleFormalizer`, because they share one crate `fuzz/<program>/`). Making them
parallel is **harder than it looks**, and the obvious "one crate per invariant" does
NOT work:

- **Separate `target/` per crate** → every crate recompiles the heavy `litesvm` /
  `libafl` / `solana` deps from scratch (minutes, ~900 MB each). N× the build work — a
  net regression, not a win.
- **Shared `CARGO_TARGET_DIR`** → the deps compile once, BUT Crucible hardcodes the
  harness binary name (`[[bin]] name = "invariant_test"`, and `find_fuzz_binary` expects
  it), so every crate compiles to the *same* `…/target/release/invariant_test` — the
  builds clobber each other and concurrent runs race on the binary. **Unusable.**

The only clean path to parallel fuzzing is **`crucible run --binary-in`**: build each
invariant's binary *serially* (shared crate/target, cheap incremental, no collision),
copy it to a per-invariant path, then fuzz all binaries *in parallel* via `--binary-in`.
That requires splitting build from fuzz and orchestrating it in the Crucible backend
(not the per-batch decider, which does author→build→fuzz→publish as one unit) — a
non-trivial formalize-phase redesign.

**Decision: deferred.** The serialized-fuzz cost is small at the e2e's 12 s budget (the
27-min e2e is dominated by LLM authoring + incremental builds, which this doesn't
parallelize), and only becomes the bottleneck at **production fuzz budgets** (60–300 s),
where N × serial fuzzing dominates. Revisit then, as a scoped `--binary-in` project.
This supersedes the "crate-per-component" framing of parity gap #4 / command-sandbox.md
§10 — the parallelism lever is fuzz-via-`--binary-in`, not per-component crates.

## 8. Open sub-questions for this change
- **Per-invariant vs one-unit:** is the extra report granularity of per-invariant units
  worth a second extraction round (invariants first, then fan-out)? Prototype both.
- **How many invariants** should the model target for a program, and does the fixed
  fuzz budget get split per-invariant or shared? (Fewer, deeper is the fuzzer's
  preference.)
- **Attribution:** is the counterexample trace enough to locate the offending
  instruction, or do we still want the model to tag each invariant with the
  instructions it stresses?
- **EVM symmetry:** should Solana analysis gain a `ContractComponent`-style grouping so
  the *grouped* middle option is available, or is whole-program sufficient for Solana's
  typically-smaller instruction sets?
