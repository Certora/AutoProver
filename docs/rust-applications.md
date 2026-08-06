# Rust applications: the wheel API and the generic host

How an AutoProver application whose backend is written in **Rust** is defined, and how the generic
Python host runs it. One Rust wheel plus the `AppDescriptor` it exports *is* the application: the
host synthesizes the phase enum, the CLI, the entry point, the frontend, the artifact store and
`main()` from that declaration, and drives the wheel's callouts through the shared pipeline.

Reference for the seam as built. The driver it plugs into is
[formalization-abstraction.md](./formalization-abstraction.md) (`PipelineBackend` →
`PreparedSystem` → `Formalizer`); the confinement it runs its toolchain under is
[command-sandbox.md](./command-sandbox.md); the RAG corpus a wheel can declare is
[rag-import-format.md](./rag-import-format.md).

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
|---|---|---|
| `descriptor() -> str` | pure | the declarative spine (§3), read once at load |
| `validate_preconditions(args_json) -> str \| None` | pure | fail before any service opens; `None` = ok |
| `checks(input_json) -> str` | pure | the checks this input formalizes, pre-authoring (§6) |
| `author_prompt(input_json) -> str` | pure | the instruction (+ domain system prompt) for one authoring *session* (§5) |
| `check_syntax(input_json, spec) -> str \| None` | pure | reject a spec at write time; `None` = accept. Cheap — it runs on every put/edit |
| `judge_prompt(input_json, spec) -> str \| None` | pure | optional LLM review; `None` = this wheel has no judge |
| `compile(input_json, spec, workdir, sandbox_json) -> str` | **blocking** | build the whole spec once — how setup and preflight build |
| `validate(input_json, spec, target_json, workdir, sandbox_json) -> str` | **blocking** | build + check one target — which arrives with the rows it covers — returning a verdict per row (§6) |
| `workspace_prep(input_json) -> str` | pure | a *plan* the host executes (§7) |
| `sandbox_grants(args_json) -> str` | pure | extra grants to union into the host's policy (§8) |
| `finalize(outcomes_json) -> str \| None` | pure | run-level artifact files, `{relpath: contents}` (§9) |

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
  `FinalizeInput`. Every `json.loads` of a wheel's answer happens in one of its `parse_*`
  functions, so a renamed field fails at the boundary naming the field, not three frames later as
  an empty string.
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
|---|---|
| `name` | the app name, task ids (`{name}-{step}`), the synthesized enum's class name |
| `header_text` | the TUI header |
| `ecosystem` | which `Ecosystem` the shared front half uses (default `evm`) |
| `backend_tag` | the report's backend vocabulary. Typed as `ReportBackend`, so a wheel declaring a tag the report doesn't know fails at descriptor load, before the run starts |
| `backend_guidance` | prose injected into the property-extraction prompt: what this verifier can check |
| `analysis_key` | the system-analysis cache key |
| `component_noun` | the human noun for one formalized component in the console/TUI ("instruction"); `None` → "component", read through `unit_noun()` |
| `check_noun` | what this backend calls one check **to the model** ("rule", "harness function"); `None` → "check", read through `check_label()` |
| `evidence_kinds` | the closed set an author may cite when rebutting the judge (§5) |

**Phases.** `phases: [PhaseSpec { key, label, order, role }]`. The host synthesizes
`enum.Enum(f"{Name}Phase", …)` from the keys ([`build_phase_enum`](../composer/rustapp/host.py)).
This is safe because phase members are only ever used for `.name` and as dict keys — there are no
`isinstance` or identity checks against a static class — and the *one* rule (`phase_labels` must be
keyed by the same synthesized members) holds by construction: the frontend's labels and the
backend's `TaskInfo`s come from one `RustApplication`.

`role` says **which step of the run the phase groups**, and for the steps the host runs as their own
visible task it is also the declaration *of* that step: the task is the phase's own label, under the
phase itself, with id `{app}-{role}`. `grouping` (the default) declares no step — a phase that only
organizes the UI, like autoprove's harness/autosetup.

The four the driver itself tags — `analysis`, `extraction`, `formalization`, `report` — are
`PhaseRole.required()` and every descriptor must claim them (`build_core_phases` raises otherwise).
The rest are optional, and **a role no phase claims is a step the application does not have**:
`discovery` groups the design-doc task the *entry point* runs before the pipeline (unclaimed, it
falls back to the first declared phase), `preflight` declares the toolchain check (§4.2), and `setup`
the shared artifact authored before the fan-out (§4.3). Claiming a step by role rather than by a
side struct naming a `phase_key` means there is no key for one half to spell and the other to
match — and no way to point a step at a phase that doesn't exist.

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
|---|---|
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
2. **The toolchain check**, when the descriptor declares `preflight` — an `Authored::Preflight`
   `compile` whose `spec` is **empty**: nothing has been authored yet, so the wheel renders its own
   throwaway skeleton, the smallest artifact that still exercises what an authored one will depend
   on. This step is optional; a wheel whose toolchain has nothing worth checking early omits the
   phase and keeps only step 1.

Why check, and not just prep: `cargo fetch` resolves a dependency graph and **compiles
nothing**, and a failed warm is deliberately non-fatal. Without the check, the first thing that
actually builds is the compile of the first LLM-authored draft — at the far end of extraction — and
everything that can go wrong there is invisible to an authoring agent's revise loop, because the
agent does not own the manifest:

| Failure | Where it would otherwise appear |
|---|---|
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
fan-out. `begin` de-duplicates properties by title (components are disjoint, but two of them can
surface the same property), runs a `setup` session under the declared step's task, and hands back
the `RustFormalizer` built around the result — the artifact is threaded through the constructor
rather than assigned onto a live formalizer, so the formalizer is constructed once and never
mutated.

The artifact is **cached** like a formalization result (`RustSetupSpec`), keyed by
[`_setup_identity`](../composer/rustapp/adapter.py): the program and its crate, the analyzed model,
the property set, and whether types come from the crate or a generated IDL. Deliberately not the
whole input — `args` also carries run knobs (a fuzz budget) that don't change what gets authored.
Authoring + compiling this is a full LLM loop and on a large program the longest single step of a
run, so a re-run after a downstream failure must not pay for it twice. As with the driver's other
caches, changing the *prompt* does not invalidate; clear the namespace for that.

A wheel with no `setup` gets its `RustFormalizer` directly from `prepare_formalization` and never
mentions staging.

### 4.4 Formalization: the authoring session

Per component, [`RustFormalizer.formalize`](../composer/rustapp/adapter.py) asks the wheel for its
checks and hands them to one session (§5):

```python
checks = parse_checks(module.checks(input_json))  # pure: the checks, before authoring
outcome = await run_session(module=…, input=…, kind="component", checks=checks, titles=…)
# inside the session, driven by the agent:
#   put_spec / edit_spec        → the buffer, gated by the wheel's check_syntax
#   validate_spec(checks=None)  → one run per DISTINCT target, each carrying the checks it covers;
#                                 stamps the buffer's digest when every live check is accounted for
#   expect_check_failure(c,why) → a failure that IS the finding stops blocking the gate
#   record_skip(p, why)         → drops that property's checks from the run and the mapping
#   feedback_tool(rebuttals=…)  → the wheel's judge, structured; stamps on acceptance
#   result(commentary, mapping) → refused unless every stamp matches the CURRENT buffer
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

`fetch_verdicts` maps the wire verdicts `validate` baked into the result onto the report's own
`Verdict` (its `message` is the wire `detail`), filling `unit_file` from the component when the
wheel didn't name one. The store writes the per-component artifacts and metadata (§9), and
`finalize` receives the whole outcome set as `FinalizeInput` — program, crate, IDL path, the shared
setup spec, and per component its `artifact_text`, `property_checks` and the `targets` its
checks ran under — and returns `{relpath: contents}` the host writes under the project root,
path-confined.

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
literally; only prose moves. The host renders this through `CheckVocab`, which re-describes each
schema per session.

> A schema that is re-described must not carry `@tool_display`: that decorator rebinds
> `as_tool`/`bind` closed over the class it decorated, so a *subclass* of a decorated schema hands
> back the base's fields with no error at all. The factories in
> [`session.py`](../composer/rustapp/session.py) apply the display themselves for exactly this
> reason, and `test_rust_llm_agent.py` guards it. A wheel that spelled the protocol itself could drift
from what the host enforces, so it is never asked to. The instruction is likewise augmented: the host
appends the exact property titles and check names from `checks()`, because the publish gate compares
the declared mapping against those strings literally.

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

**The judge is structured.** When `judge_prompt` returns a prompt for an input (probed once with an
empty spec), the session binds `feedback_tool`; a wheel with no judge gets no review machinery and
no feedback stamp among its required validations. The judge is a sub-agent that must read the draft
back through `get_spec` (`did_read`) and must call `result` with a `PropertyFeedback` — `good` is a
field it had to set, so there is no unparseable reply to interpret and no fail-open default. Its
acceptance is a stamp like any other. The author may answer a prior round with a `rebuttal`, typed
by the wheel's declared `evidence_kinds`.

**Publishing** requires every stamp to match the current buffer and the property→checks mapping to
account for every property that was not skipped, checked against the checks the wheel declared.
`give_up(reason)` is the honest exit and is reported as a real outcome.

---

## 6. Checks, targets and verdicts

A **check** is the backend's named, runnable verification of one property — a CVL rule, a foundry
test, a fuzz harness function. It is what the report keys a row by, and it is the concept the whole
seam is organized around: a check yields a `Verdict`. (A *component* is the thing system analysis
produced and the pipeline fans out over; one component's session authors many checks.)

`checks(input)` is **pure and pre-authoring**: it is the report's property→check map, the set of
names the prompt requires the author to produce, and the list the publish gate validates the
declared mapping against. It runs before the first turn, so the report's shape never depends on what
the model happened to write.

A `Check` is `{ property, name, target? }`. A **target** is **one invocation of the checker** — one
build + one run — and several checks may share one, so a wheel can put a component's whole property
set in a single target. That is the run-vs-report split: targets group what *runs*, checks group
what is *reported*, and a target sits inside one component's session, so the three nest
(`component ⊇ target ⊇ check`). `target: None` makes the check its own target — one invocation per
check, the default.

The host runs each *distinct* target once (`target_or_name()`) and passes it as a
`Target { name, checks }` carrying the checks it covers, so the grouping the host just computed is
not something the wheel has to reconstruct; the wheel returns a verdict **per check in it**.
Attribution is the wheel's: it owns its result format, so it decides which check a counterexample
belongs to; the host records the verdicts verbatim and does no verdict logic of its own, never
parsing a tool's output.

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
*check*, named by the property title it verifies, with the tally in the report's own display order and
wording. A delivered component that bakes no verdicts contributes one `UNKNOWN` row so the listing
accounts for every component.

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
those types through the chain's own support crate — where that chain's Cargo/Anchor vocabulary and
its layout conventions live. Which type is inside follows from the wheel's declared `ecosystem`, not
from inspecting keys. It is the same treatment `AuthorInput`'s `model` and `unit` already get, for
the same reason.

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
|---|---|---|---|
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

The tradeoff of `callout` mode is that the deliverable lands on disk only at finalize, not
incrementally: the assembled artifact is only *runnable* once complete, and `validate` already
materializes a transient copy per run via the `files` map. Streaming partial deliverables would be a
deliberate follow-up.

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
descriptor. Domain-event rendering is data-driven: the loop emits with
[`make_emitter`](../composer/rustapp/adapter.py), which pushes `{"type": kind, …}` onto the custom
stream keyed by the active task, and the handler writes any *declared* kind to that task's
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
2. **Implement `Backend`** — `descriptor` + `checks` + `author_prompt` + `compile` + `validate` are
   required; `validate_preconditions`, `judge_prompt`, `workspace_prep`, `sandbox_grants` and
   `finalize` have defaults. Every callout is directly unit-testable in Rust with no Python.
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

Three escape hatches exist for an app the descriptor cannot express, and none is needed by an app
that fits it: `build_application(store_factory=…, backend_cls=…)` for a specialized store or
prepared-system path, `env_builder=` for a bespoke tool surface, and `run_pipeline_fn=` for a
bespoke pipeline wrapper.

---

## 12. Current limits

Facts about the seam as it stands, not open design questions:

- **Validation is serial.** `validate` is per-target and the host owns scheduling, so fanning out
  with `asyncio.gather` is a Python-side change with no API impact — but a wheel sharing one crate
  hits binary-name collisions, which is what `serialize_toolchain` exists for. Real parallelism
  needs the wheel to separate build from run first.
- **No registered chain implementations.** `SOURCE_CRATES` and `WORKSPACE_TOOLCHAINS` are both
  empty here (§7): a files-only prep plan works, a plan asking for a warm/build/IDL raises.
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
- **A skipped property is not re-planned.** `checks()` is asked for once, before authoring; a skip
  removes its checks from the run and from the mapping, but nothing re-derives what the remaining
  checks should be.
- **Prompt changes don't invalidate caches** — neither the setup spec's nor the driver's. Clear
  the namespace.

---

## 13. Key files and tests

| Concern | File |
|---|---|
| The SDK: the `Backend` trait, descriptor, wire types, `Workspace::run`, `export_app!` | [rust/autoprover-sdk/src/](../rust/autoprover-sdk/src/) |
| A complete minimal application | [rust/example-app/src/lib.rs](../rust/example-app/src/lib.rs) |
| The sandbox launcher | [rust/run-confined/src/main.rs](../rust/run-confined/src/main.rs) |
| Declarative ABI mirror | [composer/rustapp/descriptor.py](../composer/rustapp/descriptor.py) |
| Runtime ABI mirror + parsers | [composer/rustapp/wire.py](../composer/rustapp/wire.py) |
| The backend, preflight, prep, report | [composer/rustapp/adapter.py](../composer/rustapp/adapter.py) |
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
`test_rust_llm_agent.py`,
`test_rust_frontend.py`, plus `test_sandbox_run_confined.py` / `test_sandbox_escape.py` for the
launcher contract.
