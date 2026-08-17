# Rust applications: the wheel API and the generic host

How an AutoProver application whose backend is written in **Rust** is defined, and how the generic
Python host runs it. One Rust wheel plus the `AppDescriptor` it exports *is* the application: the
host synthesizes the phase enum, the CLI, the entry point, the frontend, the artifact store and
`main()` from that declaration, and drives the wheel's callouts through the shared pipeline.

Reference for the seam as built. The driver it plugs into is
[formalization-abstraction.md](./formalization-abstraction.md) (`PipelineBackend` →
`PreparedSystem` → `Formalizer`); the confinement it runs its toolchain under is
[command-sandbox.md](./command-sandbox.md); the RAG corpus a wheel can declare is
[rag-import-format.md](./rag-import-format.md). The first real application on this
seam is [crucible.md](./crucible.md).

---

## 1. What a Rust application is

**Rust declares and decides; Python wires and does.** The wheel never owns an event loop, a DB
connection, a Textual widget or an `async with`. It contributes a descriptor (data) and callouts
(pure functions, plus two that spawn a subprocess and block). Python owns every imperative,
stateful, async edge: the LLM turns, the retry loop, Postgres, event streaming, caching,
confinement policy, the TUI.

So the backend is a **passive service**, not a driver. The Python pipeline runs the
author→compile→judge→validate loop and calls the wheel when it needs an answer. Nothing in the
wheel holds state across calls, there is no resume/step protocol, and there is no async runtime in
Rust — no tokio, no `pyo3-async`, no `future_into_py`, no GIL-across-await marshalling.

Two things are deliberately orthogonal:

- **The backend's implementation language** — the wheel is compiled Rust.
- **The ecosystem**, i.e. the language of the *code being analyzed*. The wheel selects one by tag
  (`descriptor.ecosystem` = `evm` | `solana` | `soroban`) and the host resolves it against
  `composer.pipeline.ecosystem.ECOSYSTEMS`. The `echoprover` demo is a Rust wheel analyzing
  Solidity.

The ecosystem stays shared Python: the pipeline's front half (system analysis + property
extraction) is parametric over it, and a chain's system model, prompts and `locate_main` are
chain-specific, not app-specific — legitimately shared by every backend targeting that chain. The
line this draws:

> Everything downstream of "which ecosystem" that is specific to **this verifier** lives in the
> wheel. Shared **service lifecycle** (Postgres, the TUI event loop, `composer.bind`) and shared
> **chain** logic (the ecosystem) stay Python.

---

## 2. The FFI surface

Eleven callouts, all **synchronous**, all speaking JSON strings.
[`export_app!`](../rust/autoprover-sdk/src/export.rs) generates every one of them from a `Backend`
impl.

| Callout | Kind | Role |
| --- | --- | --- |
| `descriptor() -> str` | pure | the declarative spine (§3), read once at load |
| `validate_preconditions(args_json) -> str \| None` | pure | fail before any service opens; `None` = ok |
| `target_for(input_json, check) -> str \| None` | pure | which invocation a declared check runs under; `None` = its own (§6) |
| `author_prompt(input_json) -> str` | pure | the instruction (+ domain system prompt) for one authoring *session* (§5) |
| `check_syntax(input_json, spec) -> str \| None` | pure | reject a spec at write time; `None` = accept. Cheap — it runs on every put/edit |
| `judge(input_json) -> str \| None` | pure | who reviews this input's drafts; `None` = no judge. Asked once, before anything is authored |
| `judge_instruction(input_json, spec) -> str` | pure | what to ask that reviewer about this draft, per round (text, not JSON) |
| `compile(input_json, spec \| None, workdir, sandbox_json) -> str` | **blocking** | build the whole spec once — how setup and preflight build; `None` is the preflight, which has no spec |
| `validate(input_json, spec, target_json, workdir, sandbox_json) -> str` | **blocking** | build + check one target — which arrives with the rows it covers — returning a verdict per row (§6) |
| `workspace_prep(input_json) -> str` | pure | a *plan* the host executes (§7) |
| `crate_root(input_json) -> str \| None` | pure | the run's build scaffolding, rendered once from the whole unit set (§9) |
| `sandbox_grants(args_json) -> str` | pure | extra grants to union into the host's policy (§8) |
| `finalize(outcomes_json) -> str \| None` | pure | run-level artifact files, `{relpath: contents}` (§9) |

A callout that cannot produce its payload — bad input JSON, a serialize failure — returns
`{"kind":"error","message":…}` instead. The host raises before that string can be read as a
successful empty answer (no judge, no files, the check is its own target) or as a domain failure
the author should revise. `None` on an optional callout stays a successful empty answer.

`compile` and `validate` run the real toolchain: each spawns `run-confined` and waits, for minutes.
They stay off the event loop without a bridge, by the pair that makes this whole design work — the
`#[pyfunction]` wraps its work in `py.allow_threads(…)`, and Python calls it with
`await asyncio.to_thread(...)` (via [`_blocking`](../composer/rustapp/session.py)). The wheel
just spawns and waits; Python moves the wait to a thread. That is also why the wheel spawns the
sandbox launcher *directly* rather than awaiting a Python runner: `run-confined` is a standalone
binary, so the wheel needs nothing from Python at run time except the policy data.

### Where the ABI lives

Every string crossing the boundary is a model in one of three places — two files holding the ABI
proper, plus the confinement wrapper, which belongs to the sandbox layer:

- [descriptor.py](../composer/rustapp/descriptor.py) — the declarative half (`AppDescriptor` and
  friends), mirroring the Rust structs.
- [wire.py](../composer/rustapp/wire.py) — the runtime half: `AuthorInput`, `Prompt`, `Failure`,
  `Check`, `Verdict`, `CompileResult`, `ValidateOutcome`, `WorkspacePrep`, `SandboxGrants`,
  `FinalizeInput`, and `CalloutError` (the envelope a callout returns instead of its payload).
  Every `json.loads` of a wheel's answer happens in one of its `parse_*` functions, so a renamed
  field fails at the boundary naming the field, not three frames later as an empty string.
- `BackendSpec` in [sandbox/config.py](../composer/sandbox/config.py) — the `sandbox_json` argument
  the two blocking callouts receive, mirroring `autoprover_sdk::sandbox::Sandbox`. A `TypedDict`
  rather than a model, because the sandbox layer deliberately carries no pydantic dependency; it is
  held to the same round trip regardless, which is why `timeout_s` is bounded below (the mirrored
  field is a `u64`, so a negative is refused at the wheel — the bound says so on this side too).

Every payload that comes in more than one shape is a **tagged union** on both sides
(`#[serde(tag = …)]` ↔ pydantic discriminated union), so `CompileFailed` carries `errors` and no
verdicts, `ValidateVerdicts` the reverse, a `preflight` `AuthorInput` has neither a unit nor a
model, and a component that gave up carries nothing past its name. None of them can be asked for a
field another owns.

Nothing on either side tolerates a missing or unexpected field — see **The strictness rule** below.

Keeping the two halves in step is checked, not just asked for:
[test_wire_roundtrip.py](../tests/test_wire_roundtrip.py) round-trips every payload root through the
other language and back, in both directions, under Hypothesis. Its generator for the inbound
direction lives in Rust ([`autoprover_sdk::fuzz`](../rust/autoprover-sdk/src/fuzz.rs), behind the
`fuzz` feature, driven by the `wire-echo` binary over a pipe) rather than being derived from the
pydantic schema, because a generator built from the host's own models cannot produce a field only
the *Rust* side declares — the drift that matters most for an outbound payload, where it means the
wheel expects something the host never sends.

A round trip is blind to one thing, but only on a *tolerant* seam: a field only one side declares,
which the other quietly defaults. Then "an older wheel omitted it" and "no wheel will ever send it"
are the same observation, and the field reads as `""` / `None` / an empty vec forever.

This seam is not tolerant, so both cases fail at the callout that carries them, and the round trips
catch every class of drift on their own. `test_wire_roundtrip.py` keeps a deterministic per-type
field-set check beside them only so a one-sided field is named directly rather than surfacing as a
serde error inside a shrunk example.

### The strictness rule

The SDK and the host ship as a unit, so a payload missing a field is never version skew — it is a
mirror that drifted, and defaulting it converts a caught bug into a silent one. **The side that
deserializes requires everything.** Concretely, and in both directions:

| | host → wheel | wheel → host |
| --- | --- | --- |
| a field only the *sender* declares | `#[serde(deny_unknown_fields)]` | `extra="forbid"` |
| a field only the *receiver* declares | no `#[serde(default)]` | no pydantic default |

Two mechanical notes for anyone adding a field:

- **`#[serde(default)]` on an `Option<T>` does nothing.** serde deserializes a missing field of that
  type as `None` on its own, whatever attributes are present, so the attribute reads as a deliberate
  compatibility decision where none was made. Requiring such a field takes
  [`crate::required::present`](../rust/autoprover-sdk/src/required.rs) via `deserialize_with`, which
  serde cannot satisfy from an absent key.
- **Nothing carries `skip_serializing_if`.** An empty optional is spelled `null`, always present, so
  absence is never a second way to say "nothing" and neither side has to treat the two alike.

Two exceptions, both structural: [`AuthorInput`](../rust/autoprover-sdk/src/authoring.rs) cannot
carry `deny_unknown_fields` because serde rejects it alongside the `flatten` that `Authored` needs
(so a host-only field there is caught by the round trip rather than at the callout), and the outbound
*pydantic* models keep their defaults — Python only serializes those, `model_dump_json` writes every
field regardless, and an empty `source_unit` is how the host says it resolved none.

`RustAppModule` is that surface as a Protocol, and `CALLOUTS` is derived from its annotations — so
[`load_module`](../composer/rustapp/host.py) rejects a module that isn't an AutoProver wheel (or is
one built against an older SDK) *at load*, naming the missing callouts, instead of dying with an
`AttributeError` several phases into a run.

### Results, errors, and declining

The pipeline result type (`FormT`) stays a Python pydantic model —
[`RustFormalResult`](../composer/rustapp/result.py) — because the driver's cache is keyed on it
(`cache_get(formalizer.formalized_type)` / `cache_put`) and must round-trip it. The wheel has no
result type of its own: it answers per target and the host accumulates. To *decline*, the host
returns the driver's `GaveUp` (a reportable outcome, not a crash); raised exceptions are reserved
for genuine failures the driver captures per component.

---

## 3. The descriptor

One struct, serialized at load time, that drives everything non-backend.
[Rust](../rust/autoprover-sdk/src/descriptor.rs) ↔ [Python](../composer/rustapp/descriptor.py).

**Identity and vocabulary**

| Field | Drives |
| --- | --- |
| `name` | the app name, task ids (`{name}-{step}`), the synthesized enum's class name |
| `header_text` | the TUI header |
| `ecosystem` | which `Ecosystem` the shared front half uses (default `evm`) |
| `backend_tag` | the report's backend vocabulary. Typed as `ReportBackend`, so a wheel declaring a tag the report doesn't know fails at descriptor load, before the run starts |
| `backend_guidance` | prose injected into the property-extraction prompt: what this verifier can check |
| `analysis_key` | the system-analysis cache key |
| `component_noun` | the human noun for one formalized component in the console/TUI ("instruction"); `None` → "component", read through `unit_noun()` |
| `check_noun` | what this backend calls one check **to the model** ("rule", "harness function"); `None` → "check", read through `check_label()` |
| `evidence_kinds` | the closed set an author may cite when rebutting the judge (§5) |

**Phases.** `phases: [PhaseSpec { key, label, order, role }]`. The host resolves these once into a
`PhaseModel` ([`build_phase_model`](../composer/rustapp/host.py)): the synthesized
`enum.Enum(f"{Name}Phase", …)`, the driver's core-phase mapping, and the frontend's labels and
section order. Synthesizing the enum is safe because members are only ever used for `.name` and as
dict keys — nothing compares them against a static class. Resolving *once* is load-bearing, though:
`enum.Enum(...)` mints a fresh class per call, so labels keyed by one model's members are invisible
to another. Nothing else builds a model, and `build_backend` requires one rather than defaulting to
its own.

A phase's `role` says **which step of the run it groups**:

| `role` | The step |
| --- | --- |
| `analysis`, `extraction`, `formalization`, `report` | the four the driver itself tags. Required — `build_phase_model` raises unless every one is claimed |
| `discovery` | the design-doc task the *entry point* runs before the pipeline. Unclaimed, that task falls back to the first declared phase |
| `preflight` | the toolchain check (§4.2) |
| `setup` | the shared artifact authored before the fan-out (§4.3) |
| `grouping` (the default) | no step at all — a phase that only organizes the UI, like autoprove's harness/autosetup |

Beyond the required four, **a role no phase claims is a step the application does not have**. For
`preflight` and `setup` — the steps the host runs as their own visible task — the phase is also the
declaration *of* that step: the task is the phase's own label, under the phase itself, with id
`{app}-{role}`. Claiming a step by role rather than by a side struct naming a `phase_key` means
there is no key for one half to spell and the other to match — and no way to point a step at a
phase that doesn't exist.

**CLI.** `args: [ArgSpec { flag, help, default, required }]` become `add_argument` calls on top of
the three positional inputs (`project_root`, `main_contract`, `system_doc`) and the standard flags.
Their parsed values are threaded into `validate_preconditions` (as `AppArgs.declared`) *and* onto
every `AuthorInput.args`, so a knob like a fuzz budget reaches the wheel without a bespoke channel.

**Events.** `event_kinds: [EventKind { kind, label, notice }]` tells the generic frontend how to
render each emitted payload (§10). A `notice` kind becomes a persistent callout plus a toast — for
one-shot important results such as a verdict — rather than a line in the collapsible log.

**Layout.** `artifact_layout: ArtifactLayout` — `deliverable_dir`, `internal_dir`, `report_dir`,
`artifact_dir`, `artifact_prefix`, `artifact_extension`, `property_suffix`.

**Steps and modes** — the fields that let a demanding app stay bespoke-Python-free:

| Field | Effect |
| --- | --- |
| a phase with `role: preflight` | check the toolchain before authoring, as its own visible task (§4.2). No such phase: the workspace prep still runs, and nothing else changes — a wheel with nothing to check just doesn't declare one |
| a phase with `role: setup` | author one shared artifact before the per-component fan-out and hand it to every component as `AuthorInput.setup` (§4.3) |
| `deliverable_mode` | `per_component` (default), or `callout { primary? }` — where `primary` is the `{program}`-templated path used as each component's report link (§9) |
| `serialize_toolchain` | put the blocking callouts behind one `Semaphore(1)` — for an app sharing a single crate / target dir |
| `confine_by_default` | build the fail-closed `launcher` sandbox config by default (§8) |
| `rag_db_default` | the RAG corpus whose search tools the default env binds; validated at load (§10) |

Everything but `name`/`header_text`/`backend_tag`/`backend_guidance`/`analysis_key`/`phases`/
`artifact_layout` is defaulted, so a minimal wheel declares almost none of it — see
[example-app](../rust/example-app/src/lib.rs), which sets every step/mode field to its default and
is a complete application.

---

## 4. The run, end to end

```text
load ─ entry point ─┬─ preflight  (workspace_prep → toolchain → build check)   ─┐
                    └─ system analysis                                          ├─ prepare_system
                                                                                ┘
   ─┬─ prepare_formalization ─────────────────┐
    └─ property extraction ───────────────────┴─ setup (author+compile, cached) ─ fan out
                                                                                    │
                     per component: author ⇄ validate(target) → verdicts ───────────┘
                                                                    │
                                            report ─ store ─ finalize
```

The two build-shaped steps are overlapped with the LLM steps that don't need them, and the shared
setup spec is authored at the one point in the run where it can be (§4.3).

### 4.1 Load and entry

[`build_application`](../composer/rustapp/host.py) imports the module, validates the callouts,
parses the descriptor, resolves the ecosystem, validates the declared RAG corpus, synthesizes the
phase enum + core-phase map + labels + section order, and returns a `RustApplication`. Both
registry references are resolved up front: an unknown ecosystem or an unregistered corpus is a
wheel bug, not something to discover mid-run.

[`rust_entry_point`](../composer/rustapp/entry.py) then does the irreducibly imperative half —
parse args, call `validate_preconditions` (with the run's inputs as `AppArgs` — the resolved
`source_unit`, so a wheel can check up-front that the code it will depend on is where the host
says it is, and the program/source path already split out of `path:Name`), open the four
Postgres-backed pools + RAG + async tool context + thread logger, build the `ServiceHost` env and
`WorkflowContext`, resolve or discover the design doc, apply `confine_by_default`, and yield the
Executor a frontend drives.

### 4.2 Preflight: prepare the workspace, then check the toolchain

[`RustBackend.preflight`](../composer/rustapp/adapter.py) runs **concurrently with system
analysis** — neither step reads the analyzed model, which is what makes the overlap safe — and the
driver cancels the analysis if it raises. It is created with `run.cpu_runner`, not `run.runner`:
the agent semaphore (`--max-concurrent`) budgets concurrent *agents*, and a multi-minute cargo
build charged to it would quietly take a quarter of the default concurrency away from the analysis
it overlaps. It is bounded by the run's other budget instead — the CPU semaphore
(`--max-cpu-tasks`, default 2), which is what keeps two toolchains off the same machine at once.

Two steps, both *declared* by the wheel and executed here:

1. **`workspace_prep`** (§7) — write the plan's files, then warm dependencies / build the program /
   place its IDL through the chain's registered toolchain. Already the run's slowest non-LLM step.
2. **The toolchain check**, when the descriptor declares a `preflight` phase — an
   `Authored::Preflight` `compile` whose `spec` is `None`, not an empty spec. Nothing has been
   authored yet, so the wheel renders a throwaway skeleton of its own: the smallest artifact that
   still exercises what an authored one will depend on. A wheel with nothing worth checking early
   declares no such phase and stops after step 1.

Step 1 alone is not enough, because `cargo fetch` resolves a dependency graph and **compiles
nothing** — and a failed warm is deliberately non-fatal. Skip the check and the first real build in
the run is draft #1's compile, at the far end of extraction — and an authoring agent cannot fix what
breaks there, because it does not own the manifest:

| Failure | Where it would otherwise appear |
| --- | --- |
| Dependency graph won't co-resolve | compiler errors in draft #1 |
| The harness crate won't link (program on another Anchor major) | compiler errors in draft #1 |
| Codegen from the program rejects it | compiler errors in draft #1 |
| The built program isn't where the fixture expects, or won't load | a mystery panic at setup |

So a failure is **terminal**: `run_preflight_gate` raises
[`PreflightFailed`](../composer/rustapp/adapter.py) with the diagnostics the wheel extracted, and
there is no re-author. Two side benefits: the check proves the built artifact actually runs, and it
leaves the target dir warm, so the first *authored* compile builds one crate instead of a graph.

What the step establishes is carried forward as `ProjectFacts { source_unit, prep_facts }` — the driver
holds it opaquely and hands it to `prepare_system` — so the preflight build, every authoring turn and the
delivered artifact all agree on what they are building against. Both halves are chain-shaped (§7).

### 4.3 Setup: the shared artifact

A wheel that declares `setup` has one artifact every component builds on (a fixture, a shared
module). It must be authored from the **union of every component's properties** — that union is what makes them
checkable — which pins it to exactly one point in the run, and
[`prepare_formalization`](../composer/rustapp/adapter.py) is not it: that overlaps extraction, so no
properties exist there yet. Nor can it be lazy on first `formalize`: whichever component won the race
would decide the artifact all the others are then told to work within.

So `prepare_formalization` returns a `StagedFormalizer` instead of a `Formalizer`, and the driver
calls [`RustStagedFormalizer.begin`](../composer/rustapp/adapter.py) between extraction and the
fan-out. `begin` takes every unit's properties unmerged, runs a `setup` session under the declared
step's task, and hands back the `RustFormalizer` built around the result — the artifact is threaded
through the constructor rather than assigned onto a live formalizer, so the formalizer is constructed
once and never mutated.

Each wire `Property` names the unit it was inferred for (`Property.component`), because a title
identifies a property only *within* a unit — the two together are the report's `PropertyKey`. So two
units' same-titled properties are two different properties: both reach the artifact, the wheel can
tell them apart, and each is tied to the surface it has to be checkable against. On a component turn
the field is that turn's own unit; the setup turn is the one that sees more than one.

A setup turn also carries the run's **unit set** (`Authored::Setup::units`, the same values
`CrateRootInput.units` holds). `begin` has it in hand at this point, so a wheel whose setup gate
builds the crate can build the *real* one — scaffolding for every unit included — rather than a
provisional shape something later has to complete. It is passed for the gate's benefit, not the
author's: nothing in the prompt depends on it.

The artifact is **cached** like a formalization result (`RustSetupSpec`), keyed by
[`_setup_identity`](../composer/rustapp/adapter.py): the program and its crate, the analyzed model,
the property set, and whether types come from the crate or a generated IDL. Deliberately not the
whole input — `args` also carries run knobs (a fuzz budget) that don't change what gets authored, and
`units` decides scaffolding rather than content, so a changed slug must not throw the artifact away.
Authoring + compiling this is a full LLM loop and on a large program the longest single step of a
run, so a re-run after a downstream failure must not pay for it twice. As with the driver's other
caches, changing the *prompt* does not invalidate; clear the namespace for that.

A wheel with no `setup` gets its `RustFormalizer` directly from `prepare_formalization` and never
mentions staging.

### 4.4 Formalization: the authoring session

Per component, [`RustFormalizer.formalize`](../composer/rustapp/adapter.py) runs one session (§5).
Nothing about the checks is decided before it starts — the author decides them:

```python
outcome = await run_session(module=…, input=…, kind="component", titles=…)
# inside the session, driven by the agent:
#   put_spec / edit_spec        → the buffer, gated by the wheel's check_syntax
#   map_checks(mapping)         → which checks verify which property. THIS is the work list:
#                                 its distinct names are what runs, grouped by the wheel's target_for
#   validate_spec(checks=None)  → one run per DISTINCT target, each carrying the checks it covers;
#                                 stamps the buffer's digest when every declared check is accounted
#                                 for, and records what it covered as `ran`
#   expect_check_failure(c,why) → a failure that IS the finding stops blocking the gate
#   record_skip(p, why)         → a property left out, with a reason; it must not be mapped
#   feedback_tool(rebuttals=…)  → the wheel's judge, structured; stamps on acceptance
#   result(commentary)          → refused unless every stamp matches the CURRENT buffer, and the
#                                 declared mapping accounts for exactly what `ran` covered
#   give_up(reason)             → a real outcome, reported
```

The component path has **no separate `compile`**: `validate`'s build *is* the compile gate. Fusing
them is the efficiency win — a per-component dry-run before the first fuzz roughly doubled the e2e
— and it is why `ValidateOutcome` has a `BuildFailed` arm at all. Because the checks share one
build, a `BuildFailed` from any target fails the whole run and the author revises.

`compile` is therefore called only for the two kinds that have no checks to validate: `setup` (whose
session is gated by `compile_spec`) and `preflight`.

An authoring turn that produces nothing costs an attempt like any other, but the toolchain never
sees it — there is nothing to build — so the next prompt is told exactly that (`_NO_ARTIFACT`).

### 4.5 Report and finalize

`fetch_verdicts` maps the verdicts `validate` baked into the result onto the report's own
`Verdict` (its `message` is the wire `detail`), filling `unit_file` from the component when the
wheel didn't name one. It reads `RustFormalResult.reported_verdicts()`, not the raw ones: the
wheel reports what its run observed, and the author's `expect_check_failure` declarations are
folded in on top (§6). The store writes the per-component artifacts and metadata (§9), and
`finalize` receives the whole outcome set as `FinalizeInput` — program, crate, IDL path, the shared
setup spec, and per component its `artifact_text`, `property_checks` and the `targets` its
checks ran under — and returns `{relpath: contents}` the host writes under the project root,
path-confined.

`findings` answers a different question from `fetch_verdicts`: *what did this run find*. A Rust
backend does not ask a model to write this up
([findings.py](../composer/rustapp/findings.py)). The finding is already there — the wheel's crash
and reproducing sequence, and the reason the author gave when they called `expect_check_failure`.

`reported_verdicts()` combines those two sources (§6). `findings` then turns each resulting BAD
row into one `Finding`. That includes a check the author declared even if this run did not
reproduce it. `ERROR` and `TIMEOUT` stay in the verdict table: a check that never ran is a
coverage gap, not something the run found.

Each field comes from what the run already has:

| Field | Source |
| --- | --- |
| `title` | `RustFormalResult.display_name` — the property title when the check verifies exactly one property, otherwise the check name. Same rule as the console rollup, so a finding and its verdict row have the same name |
| `content.description` | The row's message: the author's reason first, then `NOT REPRODUCED` if this run produced no counterexample, then the evidence |
| `content.summary` | The first line of that description |
| `content.proof_of_concept` | The wheel's counterexample — only if this run actually produced one |
| `content.impact` | Empty |
| `provenance.risk_reasoning` | The author's declared reason, so a reader can tell a declared finding from one the run tripped over without reading the evidence |
| `severity` | `informational` |

Severity stays `informational` and impact stays empty because nothing in this pipeline has judged
what a fuzzer crash is worth. Inventing `high` would be worse than leaving it blank.

---

## 5. Authoring and review

Authoring is the **shared session** of [`composer/authoring/`](../composer/authoring/) — the same
workflow the CVL and foundry backends run, described in
[formalization-abstraction.md §4.3.1](./formalization-abstraction.md) — assembled for a wheel in
[`session.py`](../composer/rustapp/session.py). One stateful agent per spec: it owns a `curr_spec`
buffer, edits it, calls the gate, asks for review, and publishes. The wheel supplies the prompts and
answers the callouts; it is still a passive service. What follows is only what is Rust-specific.

**The prompt is two halves.** The host owns the protocol half — the tools, what the publish gate
requires, what a skip and a give-up mean — rendered from
[`authoring_protocol.j2`](../composer/templates/authoring_protocol.j2). `Prompt.system` is the
*domain* half and is prepended to nothing else.

**The wheel supplies its own noun.** Every prompt and tool description says what `check_noun`
declares — Crucible's author reads about *harness functions*, another wheel's about *invariants* —
because an author writes better when the prompt speaks the language its own generated code uses. The
tool *names* stay generic (`validate_spec`, `expect_check_failure`) so the protocol can name them
literally; only prose moves. The host renders this through `CheckVocab` plus graphcore
`tool_family`: the session tools declare `{check}` / `{checks}` placeholders and
`with_template` instantiates them per wheel.

> `@tool_family_display` applies the UI label *after* `with_template`, so the generated
> subclass is what `as_tool`/`bind` close over. Putting `@tool_display` on the untemplated
> family class would rebind those methods over the *base* schema and the nouns would
> vanish with no error. `test_rust_llm_agent.py` guards it. A wheel that spelled the protocol itself could drift
from what the host enforces, so it is never asked to. The instruction is likewise augmented: the host
appends the obligation the gate enforces — declare the mapping before validating, cover every
property, and name only checks that are really in the spec — because that rule is the framework's
and identical for every backend, while the names themselves are the author's.

**The gate is a tool, not a loop.** A component session gets `validate_spec` (the wheel's `validate`,
per target); a setup session gets `compile_spec` (the wheel's `compile`). A run that passes *stamps a
digest of the buffer it saw* into the session's validations, so any later edit invalidates it
without anything having to remember to clear it. A partial run — `validate_spec(checks=[…])`, for
iterating on one problem — never stamps.

**A failure blocks unless it is the finding.** A check that did not come back `GOOD` keeps the gate
unstamped, unless the author marked it with `expect_check_failure(check, reason)`. That is how a real
counterexample reaches the report *as a finding with a justification* rather than as a row nobody
examined. It is the same mechanism as CVL's `expect_rule_failure` and foundry's
`expect_test_failure`.

**Every verdict says what the run behind it cost.** A `GOOD` from a campaign that explored to a
ten-minute budget is a real claim and one from a twelve-second campaign is nearly none, and a report
row carries only its check, its outcome and its `message`. So Crucible's `validate` ends every
verdict — green ones included — with the component it came from and what its campaign spent against
what it was allowed (`crucible-app/src/campaign.rs`). The note goes *last*: a `BAD`'s first line is
the only one the live console shows, so accounting must never displace a counterexample.

The marking is the *author's*, and the verdict is the *wheel's*; they meet on `RustFormalResult`,
whose `reported_verdicts()` is what both the report and the console rollup read. A declared check
reports `BAD` whatever its run said, because the alternative is the failure this exists to prevent:
the gate accepts a declared check without ever asking the run to reproduce it, so a documented
finding whose campaign did not happen to hit it would otherwise reach `report.html` as a clean row.
The two cases are distinguished in the detail rather than in the outcome — a reproduced finding
carries its counterexample, an unreproduced one says `NOT REPRODUCED` and names what the run did
say — because an unreproduced finding rests on the author's reading alone and a reader has to be
able to tell. `verdicts` itself stays verbatim: attribution remains the wheel's, and the declaration
is applied on the way out.

**The judge is structured.** When `judge` names a reviewer for an input, the session binds
`feedback_tool`; a wheel with no judge gets no review machinery and no feedback stamp among its
required validations. That question is asked once, when the session is built, and takes no spec —
whether an input is reviewed and who reviews it are both fixed before anything is authored, so only
`judge_instruction` is given a draft, once per round. The judge is a sub-agent that must read the draft
back through `get_spec` (`did_read`) and must call `result` with a `PropertyFeedback` — `good` is a
field it had to set, so there is no unparseable reply to interpret and no fail-open default. Its
acceptance is a stamp like any other. The author may answer a prior round with a `rebuttal`, typed
by the wheel's declared `evidence_kinds`.

**Publishing** requires every stamp to match the current buffer and the property→checks mapping to
account for every property that was not skipped, checked against the checks the wheel declared.
`give_up(reason)` is the honest exit and is reported as a real outcome.

---

## 6. Checks, targets and verdicts

A **check** is a named, runnable verification in the authored artifact — a CVL rule, a foundry test,
a tagged fuzz assertion. It is what the report keys a row by, and it is the concept the whole seam is
organized around: a check yields a `Verdict`. (A *component* is the thing system analysis produced
and the pipeline fans out over; one component's session authors many checks.)

**The author decides the check set.** Which checks express a component's properties — one rule
discharging three related invariants, two rules for one invariant — is authoring work, so it cannot
be computed before authoring starts. `map_checks` declares the property→checks mapping, and the
distinct names in it are exactly what a gate run executes. The relation is **many-to-many** and both
directions are ordinary: several checks under one property title, or one check named under several.

A `Check` is `{ name, properties, target? }`. `properties` is the author's own claim, carried
verbatim from the mapping rather than guessed by the wheel — it is there because some checkers speak
in properties rather than in check names (Crucible tags each assertion message with its property
title, and that is what lets it place a counterexample), while a backend whose checker reports per
check can ignore it. A **target** is **one invocation of the checker** — one
build + one run — answered per name by the wheel's `target_for`. Several checks may share one, so a
wheel can put a component's whole property set in a single target. That is the run-vs-report split:
targets group what *runs*, checks group what is *reported*, and a target sits inside one component's
session, so the three nest (`component ⊇ target ⊇ check`). `target_for` returning `None` makes the
check its own target — one invocation per check, the default.

A check's parts therefore come from the two parties that can know them: the **name** and **what it
verifies** from the author (the artifact is the author's, and so is the claim) and the **grouping**
from the wheel (a backend convention). The host puts them together, runs each *distinct* target once
(`target_or_name()`) and passes it as a `Target { name, checks }` carrying the checks it covers, so
the grouping is not something the wheel has to reconstruct; the wheel returns a verdict **per check
in it**. Attribution is the wheel's: it owns its result format, so it decides which check a
counterexample belongs to; the host records the verdicts verbatim and does no verdict logic of its
own, never parsing a tool's output.

### The verdict contract

Because the names are the author's, `validate` is also where a name is held to the artifact. A
backend must not answer `GOOD` for a check it found no evidence ran:

| Situation | Verdict |
| --- | --- |
| The checker has no such check | `ERROR`, with a detail naming it |
| It exists but the run never exercised it | `UNKNOWN`, with a detail saying so |
| It ran and held | `GOOD` |
| It ran and was refuted | `BAD` + the counterexample |

Both non-`GOOD` cases block the publish gate, which is the point: a declared check with nothing
behind it must not stamp a property as verified. What corroborates a check is the backend's own
business — a per-rule result from the Prover, a runtime tally of which tagged assertions a campaign
evaluated — but *some* evidence is required, and a scan of the source text is not it: a name in a
comment or in dead code reads exactly like a check. `Target::all(GOOD, …)` is therefore not a
legitimate answer to a clean run; "nothing was refuted" is one fact about the target, while `GOOD`
per check is a claim about each check individually. The residue no mechanism reaches — whether a
check that demonstrably ran genuinely verifies the property it claims — is the judge's.

### Verdicts

A verdict is keyed by **check name**, not by a restated `Check`. The wheel picks from the checks it
was just handed rather than echoing them back, and the host resolves each name to the `Check` it sent
before anything upstream sees it. Both ends hold that key to the target's own checks. On the Rust
side `ValidateOutcome::Verdicts` wraps a private `CheckVerdicts`, so the only ways to build one are
`Target::all` and `Target::verdicts`, which take the names from `self.checks` — a backend attributes
a run instead of spelling names. On the host side `ValidateVerdicts.resolve(target)` requires the
answer to be *exactly* the target's check set: a name no check has, a covered check left unanswered,
or the same check twice raises `ValidateCoverageError`. The unanswered case is the one worth the
machinery — a missing verdict is not a failing verdict, so it gives the publish gate nothing to
object to, and a wheel that answered for nothing would stamp a component nothing had checked.

A check the author marked with `expect_check_failure` is the one place the wheel's attribution is
not the last word. The declaration is the author's — "the failure here IS the finding" — and the
publish gate accepts such a check as clean without ever requiring the run to reproduce it, so
nothing else stands between a documented finding and a green row. `reported_verdicts()` folds the
two together on the host side: a declared check reports `BAD` whatever the run said, and the detail
says which case it is — a reproduced finding carries its counterexample, an unreproduced one says
`NOT REPRODUCED` and names the outcome the run did reach. Both the report and the console rollup
read the fold, so they cannot disagree about whether the run found something.

A stamping run records what it covered as `ran` — the targets, each with its checks — and that is
what the publish gate validates the declared mapping against, in both directions: every claimed name
must have run, and every name that ran must be claimed. Ground truth is the stamping run rather than
the current declaration, so a name added since is one that did not run and a name removed is one
that ran unclaimed; both are errors, which is why editing the mapping needs no stamp of its own. The
same `ran` reaches the result as `RustFormalResult.targets`, so a component's coverage stays
answerable even where a whole target erred.

Verdicts are grouped by property, not appended as singletons — two checks verifying the same
property are two check names under one report row, and as singletons they would be two rows with the
same key that the store's `dict()` would silently collapse.

`Outcome` is a closed enum on both sides (`GOOD` / `BAD` / `ERROR` / `TIMEOUT` / `UNKNOWN`) — the
report's backend-agnostic vocabulary, whose human wording ("No counterexample" vs "Verified") is
picked at render time from `backend_tag`, so a wheel never spells it out. A typo that used to reach
a report row as an unexplained `UNKNOWN` now doesn't compile. A label the *host* has never heard of
is refused rather than downgraded: with both sides shipping together it can only mean a variant added
to one `Outcome` and not the other, and rendering that as `UNKNOWN` would hide the drift behind a row
that looks merely inconclusive. `Verdict.detail` carries the counterexample or error text, so a bare
`BAD` is never unexplained.

[`results.py`](../composer/rustapp/results.py) rolls these up for the console/TUI: one row per
*check*, with the tally in the report's own display order and wording. A row is named by
`RustFormalResult.display_name` — the property title when the check verifies exactly one, and
otherwise the check's own name, the only thing that names it unambiguously once one check can carry
several properties. `findings` titles its findings by the same method, so a finding and its verdict
row are never called different things. A delivered component that bakes no verdicts contributes one
`UNKNOWN` row so the listing accounts for every component.

---

## 7. The project seam

Two things the host needs to know about a project it does not itself understand — where its code lives
as a unit of that project's build system, and how to prepare/build its workspace. Both are knowledge
about the **ecosystem under analysis**, not about implementing a backend in Rust: an application is
written in Rust, but the project it analyzes need not be. So
[toolchain.py](../composer/rustapp/toolchain.py) declares one seam, `ProjectToolchain`, and the
application that needs it registers an implementation per chain (shared by every wheel targeting it,
exactly like the ecosystem and RAG registries).

**Everything project-shaped crosses that seam opaquely.** A Cargo package name, an Anchor IDL, a Move
package's named addresses are one ecosystem's vocabulary; a framework that declared fields for them
would make the next ecosystem an edit to [wire.py](../composer/rustapp/wire.py). So three payloads are
carried without a schema — `source_unit`, `prep_facts` and `WorkspacePrep.toolchain_request` (Rust:
`autoprover_sdk::chain::ChainData`, a JSON object and nothing more). They are typed at both *ends* and
nowhere in between: the chain's registered implementation and the wheels targeting that chain share
those types through the chain's own support crate ([autoprover-solana](../rust/autoprover-solana),
which is where `{dir, package, lib, anchor}` and the `programs/<program>` fallback live). Which type is
inside follows from the wheel's declared `ecosystem`, not from inspecting keys. It is the same
treatment `AuthorInput`'s `model` and `unit` already get, for the same reason.

**`workspace_prep` is a pure plan the host executes.** The wheel returns
`WorkspacePrep { files, toolchain_request }` — file *contents* and declarative intent, never a command
line — and [`run_workspace_prep`](../composer/rustapp/adapter.py) writes the files itself
(path-confined) and hands the request to the chain's `ProjectToolchain`. The split is load-bearing for
the network posture: the sandbox **never** gives a confined process network access, so dependency
fetches run *unconfined* (a fetch executes no untrusted code) and anything that compiles runs
*confined + offline*. Handing the wheel a confined-with-network policy would be a brand-new security
capability; declaring a plan is not.

What the toolchain establishes comes back as `prep_facts` on every later callout, so a fact there means
*the thing it describes is in place* — Solana's `{idl: <path>}` is what routes a harness to generated
types instead of a dependency on a program crate it may not be able to link. A request that cannot be
carried out is a hard error: a wheel only asks when it cannot proceed without the result.

`AuthorInput.program` is not part of any of this. It is only the *analysis* identifier (the `Name` in
`path:Name`) — a label and a namespace — and nothing about a build-system unit follows from it (a real
lending program: directory `programs/lend`, package `example-lending`, lib `example_lending`). That is
exactly why `source_unit` is resolved once and carried rather than derived per callout.

**No chain has an entry today.** The seam's two halves are therefore reached in deliberately different
ways:

- `source_unit` **degrades**. An empty answer is already a documented state — it is what Solidity
  yields, and what an unreadable layout yields — and the wheel fills the gaps from its own convention
  (`SolanaSourceUnit::resolved`). "No toolchain" is indistinguishable from "nothing to resolve", which
  is the honest answer.
- `project_toolchain` **raises**. A plan that only places files never reaches it
  (`WorkspacePrep.needs_toolchain`), so getting there means the wheel asked for preparation nothing can
  perform. Skipping it silently would resurface much later as a mystifying compile error in the first
  authored draft — read as the authoring agent's fault.

---

## 8. Confinement and the security invariant

> The LLM controls file **contents** only. The trusted wheel authors every argv. **Python** authors
> every sandbox policy.

The wheel gets a `Workspace { dir, sandbox }` — the workdir and its `Sandbox { argv_prefix,
timeout_s }`, bundled because every command needs both — and launches
`[*argv_prefix, program, *args]` through the one shared helper,
[`Workspace::run`](../rust/autoprover-sdk/src/sandbox.rs). The prefix is **opaque**:
Python owns the confinement *intent* and lowers it to an argv (`SandboxConfig.backend_spec` →
`LauncherProvider.argv_prefix`), which names no sandbox mechanism, so swapping the mechanism never
changes this shape. An empty prefix is the trusted/`none` path — the command runs directly. A
non-empty one is a full `run-confined <flags…> --` wrapper whose launcher confines *itself*
(Landlock + seccomp + rlimits + env allowlist) and then `execve`s the tool, fail-closed:

```text
run-confined --rw <workdir> --rw <cargo home> --ro <toolchain> --allow-env PATH …
             --rlimit-as … -- <program> <args…>
             ^──────────── Python-authored ───────────^  ^─ wheel-authored ─^
```

`Workspace::run` materializes the (possibly LLM-derived) `files` map into the workdir first, joining
each relative path through `confined_join` — rejecting absolute paths and `..` — and Landlock grants
only the `--rw` workdir, so even a bad path can't escape. The host's own writes (the prep plan's
files, `finalize`'s deliverables, an IDL a toolchain places) go through the mirror-image
[`confined_target`](../composer/rustapp/adapter.py). Timeouts are enforced in the helper (reader
threads avoid a pipe-buffer deadlock; a kill reports the captured output plus the timeout).

A wheel that sets `confine_by_default` gets the fail-closed `launcher` provider by default, still
overridable by `COMPOSER_SANDBOX_PROVIDER`, with its declared `sandbox_grants` (`extra_ro` paths,
`extra_env` names) unioned into the Python-authored policy.

Per seam:

| Seam | New capability | Who authors argv | Who authors policy |
| --- | --- | --- | --- |
| `compile` / `validate` | runs the toolchain | the **wheel** (after `--`) | **Python** (before `--`) |
| `workspace_prep` | warm dirs, build a program, place an IDL | **Python** (the registered toolchain; the wheel supplies contents + which dirs/program) | **Python** |
| `finalize` / prep `files` | writes files under the project root | — (host writes, path-confined) | n/a |
| `sandbox_grants` | adds `extra_ro` / `extra_env` | n/a (data) | **Python** unions them in |

No seam gives the LLM argv control, and none lets the wheel invent a policy. Full details in
[command-sandbox.md](./command-sandbox.md).

---

## 9. Deliverables and the store

[`RustArtifactStore`](../composer/rustapp/store.py) is a thin `ArtifactStore` subclass; the base
already writes everything identical across backends (`properties.json`, `commentary.md`, the
property→checks map, `token_usage.json`). All the subclass supplies is the descriptor's layout — and
the choice of how the *source* deliverable lands:

- **`per_component`** (default) — one `{prefix}_{slug}.{ext}` file per component, written from its
  `artifact_text` by the base writer.
- **`callout`** — the store writes **no** per-component source; it writes the shared metadata and
  returns the mode's `primary` (`{program}`-templated) as the component's report link. The whole
  deliverable comes from `finalize`, which is handed every component's `artifact_text`,
  `property_checks` and `targets` plus the shared setup spec, and can therefore assemble one
  artifact (a single crate with a section per property) as the single source of truth for its layout.

**`crate_root` is what keeps a callout deliverable from being assembled twice.** Scaffolding for a
multi-unit build — a manifest's feature list, a crate root's module declarations — is a function of
the *whole unit set*, which no per-unit callout can see. Without the hook a wheel must re-render it
on every gated build from the one unit it happens to hold, and the real artifact is only ever
assembled at the end, where nothing has built it ([crucible.md](./crucible.md) §4 is what that
cost). The host calls `crate_root` once, in `StagedFormalizer.begin` — the first point both the
shared setup spec and the unit set exist — writes the result, and never rewrites it. A wheel that
implements it should emit only per-unit files from `compile`/`validate`, so a gated build *is* the
deliverable with one target selected.

A wheel that declares a `setup` phase is sent the unit set on that turn too (§4.3), so its setup gate
can render the same scaffolding and build against it. The hook still runs, and still matters: the
host serves a cached setup spec **without** calling `compile`, so on that path the gate never runs and
this is the only thing that puts the crate on disk. Render both from the same inputs and the second
write is byte-identical — one assembly, repeated, rather than two shapes to keep in agreement.

Because the scaffolding is written before any outcome exists, it necessarily declares a target for
every unit — including ones formalization later gives up on. `ComponentOutcome::GaveUp` therefore
carries the unit and the author's reason, so `finalize` can put something honest behind such a
target rather than leaving it dangling.

The remaining tradeoff of `callout` mode is that the *sections* land on disk only at finalize: the
assembled artifact is only fully runnable once complete, and `validate` already materializes a
transient copy per run via the `files` map. Streaming partial deliverables would be a deliberate
follow-up.

---

## 10. The Python shell: entry point, frontend, CLI

All of this stays Python — it is service lifecycle and UI, not data — but it is
**descriptor-driven**, so an application supplies none of it.

**Entry point** ([entry.py](../composer/rustapp/entry.py)). Irreducibly imperative async: nested
`async with` over `standard_connections` (checkpointer, store, pgvector indexed store, memory
backend), the async tool context and the thread logger; the `ServiceHost` env and
`WorkflowContext`; the design doc read through the async uploader; `import composer.bind` for its
import-time DI/tape bootstrap. What Rust contributes here is purely declarative: the arg schema and
the `validate_preconditions` hook. The env is descriptor-driven too — `build_default_env` binds the
standard source-navigation tools plus, when `rag_db_default` is set, that corpus's search tools
(none by default; the embedding stack is imported lazily so a wheel without a corpus never pays for
it). A backend wanting a different tool surface entirely passes `env_builder=`, which owns the whole
surface and takes no `rag_db`.

**Frontend** ([frontend.py](../composer/rustapp/frontend.py)). Two thin subclasses of the shared
bases — a `MultiJobApp` TUI and a stdout handler — whose phase labels and section order come from the
descriptor. Domain-event rendering is data-driven: the gate tools emit with
[`emit_event`](../composer/rustapp/adapter.py), which puts `{"type": kind, …}` on the graph run's
custom stream, and the handler writes any *declared* kind to that task's
collapsible log, or, for a `notice` kind, posts a persistent callout with the report's own outcome
glyph. Rust controls the event *content*; Python does the emission (the wheel cannot call
`get_stream_writer()`), and no per-application handler subclass is needed.

**CLI** ([cli.py](../composer/rustapp/cli.py)). Two `main()` shapes differing only in who owns the
event loop — `tui_main` (pipeline as a background worker inside Textual) and `console_main`
(pipeline directly, printing on completion). Both print the run summary, the counts block using
`unit_noun`, and the verdict tally + listing when the results carry verdicts (empty otherwise, so a
wheel that bakes none prints nothing). `import composer.bind` runs first.

---

## 11. Packaging and writing a new application

The wheel is its own maturin project, so `ai-composer` stays on setuptools and gains one dependency.
`requires-python = ">=3.12"` is a hard floor (the seam uses PEP 695 generics), so wheels are abi3
for cp312+.

1. **New crate** — `cdylib`, depending on `autoprover-sdk` and `pyo3`
   (`features = ["extension-module", "abi3-py312"]`). The `[lib] name` MUST match the `export_app!`
   module ident and the maturin module name. Copy
   [example-app/Cargo.toml](../rust/example-app/Cargo.toml).
2. **Implement `Backend`** — `descriptor` + `author_prompt` + `compile` + `validate` are
   required; `validate_preconditions`, `judge`, `judge_instruction`, `workspace_prep`,
   `sandbox_grants`, `crate_root` and `finalize` have defaults. Every callout is directly unit-testable in Rust with no Python.
3. **Export it** — `autoprover_sdk::export_app!(my_app, MyApp::new());`
4. **Wire the build** — a maturin `pyproject.toml` (`module-name = "my_app"`) with a `[tool.uv]
   cache-keys` block over its `.rs` sources, then one line each in the root `pyproject.toml`'s
   `apps` group and `[tool.uv.sources]`. `uv sync` builds it; there is no `maturin develop` step.
5. **Ship a CLI** — two lines, and register them under `[project.scripts]`:

   ```python
   from composer.rustapp.cli import console_main, tui_main
   def main() -> int: return tui_main("my_app")
   def main_console() -> int: return console_main("my_app")
   ```

   For headless/programmatic use: `await run_rust_pipeline("my_app", source, ctx, handler, env)`.

Two escape hatches exist for an app the descriptor cannot express, and neither is needed by an app
that fits it: `build_application(store_factory=…, backend_cls=…)` for a specialized store or
prepared-system path, and `env_builder=` for a bespoke tool surface.

---

## 12. Current limits

Facts about the seam as it stands, not open design questions:

- **Validation is serial.** `validate` is per-target and the host owns scheduling, so fanning out
  with `asyncio.gather` is a Python-side change with no API impact — but a wheel sharing one crate
  hits binary-name collisions, which is what `serialize_toolchain` exists for. Real parallelism
  needs the wheel to separate build from run first.
- **One registered chain implementation.** `PROJECT_TOOLCHAINS` holds Solana's (§7); for any other
  chain a files-only prep plan works and a plan asking for more raises.
- **Self-contained backends only.** This shape fits a checker that is a **local tool**. A backend
  whose "validate" is a remote/Python service cannot spawn it under `run-confined`; there is no
  `run_prover`-style host effect, so such a backend would need one added (or stays a Python
  backend).
- **No HITL.** The generic task handler raises on an interrupt prompt; an interactive Rust
  application would need a new mechanism.
- **No build-only tool in the component belt.** `validate` fuses build and run, so a separate
  `check_build` would only save the checker's runtime on a draft that does not compile. Worth adding
  if a real run shows an author burning its fuzz budget on build errors — the callout is already
  wrapped for the setup session's `compile_spec`.
- **Undeclared work is invisible.** The check set is the author's declaration, so a check written
  but left out of the mapping simply never runs. The gate catches the reverse (a name that did not
  run) but has nothing to compare against for work never declared.
- **Prompt changes don't invalidate caches** — neither the setup spec's nor the driver's. Clear
  the namespace.

---

## 13. Key files and tests

| Concern | File |
| --- | --- |
| The SDK: the `Backend` trait, descriptor, wire types, `Workspace::run`, `export_app!` | [rust/autoprover-sdk/src/](../rust/autoprover-sdk/src/) |
| A complete minimal application | [rust/example-app/src/lib.rs](../rust/example-app/src/lib.rs) |
| The sandbox launcher | [rust/run-confined/src/main.rs](../rust/run-confined/src/main.rs) |
| Declarative ABI mirror | [composer/rustapp/descriptor.py](../composer/rustapp/descriptor.py) |
| Runtime ABI mirror + parsers | [composer/rustapp/wire.py](../composer/rustapp/wire.py) |
| The backend, preflight, prep, report | [composer/rustapp/adapter.py](../composer/rustapp/adapter.py) |
| Report rows → audit-issue findings | [composer/rustapp/findings.py](../composer/rustapp/findings.py) |
| The authoring session (buffer, gate, review, publish) | [composer/rustapp/session.py](../composer/rustapp/session.py) |
| The shared authoring workflow | [composer/authoring/](../composer/authoring/) |
| Application assembly (enum, phases, store, backend) | [composer/rustapp/host.py](../composer/rustapp/host.py) |
| Entry point / argparse / env | [composer/rustapp/entry.py](../composer/rustapp/entry.py) |
| Frontend, CLI, verdict rollup, store, result | [frontend.py](../composer/rustapp/frontend.py) · [cli.py](../composer/rustapp/cli.py) · [results.py](../composer/rustapp/results.py) · [store.py](../composer/rustapp/store.py) · [result.py](../composer/rustapp/result.py) |
| Chain seams (crate resolution, workspace toolchain) | [composer/rustapp/toolchain.py](../composer/rustapp/toolchain.py) |
| The driver this plugs into | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| Sandbox policy authoring | [composer/sandbox/](../composer/sandbox/) |

Tests: `tests/test_rustapp.py` (the wheel round-trip, end to end through the host),
`test_rustapp_wire.py` (ABI), `test_wire_roundtrip.py` (both directions of the ABI under
Hypothesis, against the real serde types), `test_rustapp_preflight.py`, `test_rustapp_workspace_prep.py`,
`test_rustapp_setup_cache.py`, `test_rustapp_verdicts.py`, `test_rustapp_toolchain_sem.py`,
`test_rustapp_gate.py`, `test_rustapp_validate_target.py`, `test_rustapp_discovery_phase.py`,
`test_rustapp_findings.py` (the declaration fold and the findings it produces),
`test_rust_llm_agent.py`,
`test_rust_frontend.py`, plus `test_sandbox_run_confined.py` / `test_sandbox_escape.py` for the
launcher contract.
