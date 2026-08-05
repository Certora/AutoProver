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

Ten callouts, all **synchronous**, all speaking JSON strings.
[`export_app!`](../rust/autoprover-sdk/src/lib.rs) generates every one of them from a `Backend`
impl.

| Callout | Kind | Role |
|---|---|---|
| `descriptor() -> str` | pure | the declarative spine (§3), read once at load |
| `validate_preconditions(args_json) -> str \| None` | pure | fail before any service opens; `None` = ok |
| `units(input_json) -> str` | pure | the report rows this input formalizes, pre-authoring (§6) |
| `author_prompt(input_json, failure_json \| None) -> str` | pure | the instruction for one authoring turn; `failure` = revise context |
| `judge_prompt(input_json, spec) -> str \| None` | pure | optional LLM review; `None` = this wheel has no judge |
| `compile(input_json, spec, workdir, sandbox_json) -> str` | **blocking** | build the whole spec once — the setup and preflight gate |
| `validate(input_json, spec, target_json, workdir, sandbox_json) -> str` | **blocking** | build + check one target — which arrives with the rows it covers — returning a verdict per row (§6) |
| `workspace_prep(input_json) -> str` | pure | a *plan* the host executes (§7) |
| `sandbox_grants(args_json) -> str` | pure | extra grants to union into the host's policy (§8) |
| `finalize(outcomes_json) -> str \| None` | pure | run-level artifact files, `{relpath: contents}` (§9) |

`compile` and `validate` run the real toolchain: each spawns `run-confined` and waits, for minutes.
They stay off the event loop without a bridge, by the pair that makes this whole design work — the
`#[pyfunction]` wraps its work in `py.allow_threads(…)`, and Python calls it with
`await asyncio.to_thread(...)` (via [`_run_blocking`](../composer/rustapp/adapter.py)). The wheel
just spawns and waits; Python moves the wait to a thread. That is also why the wheel spawns the
sandbox launcher *directly* rather than awaiting a Python runner: `run-confined` is a standalone
binary, so the wheel needs nothing from Python at run time except the policy data.

### Where the ABI lives

Two files, one per half, and every string crossing the boundary is a model in one of them:

- [descriptor.py](../composer/rustapp/descriptor.py) — the declarative half (`AppDescriptor` and
  friends), mirroring the Rust structs.
- [wire.py](../composer/rustapp/wire.py) — the runtime half: `AuthorInput`, `Prompt`, `Failure`,
  `Unit`, `Verdict`, `CompileResult`, `ValidateOutcome`, `WorkspacePrep`, `SandboxGrants`,
  `FinalizeInput`. Every `json.loads` of a wheel's answer happens in one of its `parse_*`
  functions, so a renamed field fails at the boundary naming the field, not three frames later as
  an empty string.

Every payload that comes in more than one shape is a **tagged union** on both sides
(`#[serde(tag = …)]` ↔ pydantic discriminated union), so `CompileFailed` carries `errors` and no
verdicts, `ValidateVerdicts` the reverse, a `preflight` `AuthorInput` has neither a unit nor a
model, and a component that gave up carries nothing past its name. None of them can be asked for a
field another owns.

Direction sets the defaults: **inbound** models (what a wheel returns) default every optional field
so an older wheel still parses, and pydantic ignores unknown fields so a newer one does too;
**outbound** models require what the wheel requires, because an omitted `kind` is a host bug.

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
[Rust](../rust/autoprover-sdk/src/lib.rs) ↔ [Python](../composer/rustapp/descriptor.py).

**Identity and vocabulary**

| Field | Drives |
|---|---|
| `name` | the app name, task ids (`{name}-{step}`), the synthesized enum's class name |
| `header_text` | the TUI header |
| `ecosystem` | which `Ecosystem` the shared front half uses (default `evm`) |
| `backend_tag` | the report's backend vocabulary. Typed as `ReportBackend`, so a wheel declaring a tag the report doesn't know fails at descriptor load, before the run starts |
| `backend_guidance` | prose injected into the property-extraction prompt: what this verifier can check |
| `analysis_key` | the system-analysis cache key |
| `component_noun` | the human noun for one formalized unit in the console/TUI ("instruction"); `None` → "component", read through `unit_noun()` |

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
falls back to the first declared phase), `preflight` declares the workspace gate (§4.2), and `setup`
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
| a phase with `role: preflight` | run the workspace gate as its own visible task (§4.2). No such phase: the prep still runs, silently, with no gate |
| a phase with `role: setup` | author one shared artifact before the per-unit fan-out and hand it to every component as `AuthorInput.setup` (§4.3) |
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
load ─ entry point ─┬─ preflight  (workspace_prep → toolchain → compile gate)  ─┐
                    └─ system analysis                                          ├─ prepare_system
                                                                                ┘
   ─┬─ prepare_formalization ─────────────────┐
    └─ property extraction ───────────────────┴─ setup (author+compile, cached) ─ fan out
                                                                                    │
                          per unit: author ⇄ validate(target) → verdicts ───────────┘
                                                                    │
                                            report ─ store ─ finalize
```

The two build-shaped steps are overlapped with the LLM steps that don't need them, and the shared
setup artifact is authored at the one point in the run where it can be (§4.3).

### 4.1 Load and entry

[`build_application`](../composer/rustapp/host.py) imports the module, validates the callouts,
parses the descriptor, resolves the ecosystem, validates the declared RAG corpus, synthesizes the
phase enum + core-phase map + labels + section order, and returns a `RustApplication`. Both
registry references are resolved up front: an unknown ecosystem or an unregistered corpus is a
wheel bug, not something to discover mid-run.

[`rust_entry_point`](../composer/rustapp/entry.py) then does the irreducibly imperative half —
parse args, call `validate_preconditions` (with the run's inputs as `AppArgs` — the resolved
`program_crate`, so a wheel can check up-front that the code it will depend on is where the host
says it is, and the program/source path already split out of `path:Name`), open the four
Postgres-backed pools + RAG + async tool context + thread logger, build the `ServiceHost` env and
`WorkflowContext`, resolve or discover the design doc, apply `confine_by_default`, and yield the
Executor a frontend drives.

### 4.2 Preflight: prepare the workspace, then gate it

[`RustBackend.preflight`](../composer/rustapp/adapter.py) runs **concurrently with system
analysis** — neither step reads the analyzed model, which is what makes the overlap safe — and the
driver cancels the analysis if it raises. It is created with `run.unmetered_runner`, not
`run.runner`: the run's semaphore budgets concurrent *agents*, and a multi-minute cargo build
charged to it would quietly take a quarter of the default concurrency away from the analysis it
overlaps.

Two steps, both *declared* by the wheel and executed here:

1. **`workspace_prep`** (§7) — write the plan's files, then warm dependencies / build the program /
   place its IDL through the chain's registered toolchain. Already the run's slowest non-LLM step.
2. **The gate**, when the descriptor declares `preflight` — an `Authored::Preflight` `compile` whose
   `spec` is **empty**: nothing has been authored yet, so the wheel renders its own minimal
   skeleton, the smallest artifact that still exercises what an authored one will depend on.

Why the gate and not just the prep: `cargo fetch` resolves a dependency graph and **compiles
nothing**, and a failed warm is deliberately non-fatal. Without the gate, the first thing that
actually builds is the compile of the first LLM-authored draft — at the far end of extraction — and
everything that can go wrong there is invisible to an authoring agent's revise loop, because the
agent does not own the manifest:

| Failure | Where it would otherwise appear |
|---|---|
| Dependency graph won't co-resolve | compiler errors in draft #1 |
| The harness crate won't link (program on another Anchor major) | compiler errors in draft #1 |
| IDL codegen rejects the program | compiler errors in draft #1 |
| The built program isn't where the fixture expects, or won't load | a mystery panic at setup |

So a failure is **terminal**: `run_preflight_gate` raises
[`PreflightFailed`](../composer/rustapp/adapter.py) with the diagnostics the wheel extracted, and
there is no re-author. Two side benefits: the gate proves the built artifact actually runs, and it
leaves the target dir warm, so the first *authored* compile builds one crate instead of a graph.

What the step establishes is carried forward as `RustPreflight { program_crate, idl }` — the driver
holds it opaquely and hands it to `prepare_system` — so the gated build, every authoring turn and
the delivered artifact all name the same dependency.

### 4.3 Setup: the shared artifact

A wheel that declares `setup` has one artifact every unit builds on (a fixture, a shared module).
It must be authored from the **union of every unit's properties** — that union is what makes them
checkable — which pins it to exactly one point in the run, and
[`prepare_formalization`](../composer/rustapp/adapter.py) is not it: that overlaps extraction, so no
properties exist there yet. Nor can it be lazy on first `formalize`: whichever unit won the race
would decide the artifact all the others are then told to work within.

So `prepare_formalization` returns a `StagedFormalizer` instead of a `Formalizer`, and the driver
calls [`RustStagedFormalizer.begin`](../composer/rustapp/adapter.py) between extraction and the
fan-out. `begin` de-duplicates properties by title (units are disjoint, but two components can
surface the same property), runs `author_and_compile` under the declared step's task, and hands back
the `RustFormalizer` built around the result — the artifact is threaded through the constructor
rather than assigned onto a live formalizer, so the formalizer is constructed once and never
mutated.

The artifact is **cached** like a formalization result (`RustSetupArtifact`), keyed by
[`_setup_identity`](../composer/rustapp/adapter.py): the program and its crate, the analyzed model,
the property set, and whether types come from the crate or a generated IDL. Deliberately not the
whole input — `args` also carries run knobs (a fuzz budget) that don't change what gets authored.
Authoring + compiling this is a full LLM loop and on a large program the longest single step of a
run, so a re-run after a downstream failure must not pay for it twice. As with the driver's other
caches, changing the *prompt* does not invalidate; clear the namespace for that.

A wheel with no `setup` gets its `RustFormalizer` directly from `prepare_formalization` and never
mentions staging.

### 4.4 Formalization: author → validate

Per unit, in [`RustFormalizer.formalize`](../composer/rustapp/adapter.py):

```python
units = parse_units(module.units(input_json))        # pure: the report rows, before authoring
for _ in range(max_attempts):                        # DEFAULT_MAX_ATTEMPTS = 7
    spec = await author_turn(module, input_json, failure)          # LLM turn, Python
    if spec is None:                                 # the agent never called `result`
        failure = Failure(errors=_NO_ARTIFACT); continue
    for target in distinct_targets(units):           # host owns enumeration + scheduling
        res = await run_blocking(module.validate(input_json, spec, target, workdir, sandbox))
                                                     # `target` = {name, units it covers}
        if isinstance(res, ValidateBuildFailed):     # the build is shared ⇒ re-author everything
            failure = Failure(draft=spec, errors=res.errors); break
        record(res.verdicts); emit("verdict", …)      # live, per target
    else:
        return RustFormalResult(artifact_text=spec, units=…, verdicts=…, targets=…)
return GaveUp(…)
```

The component path has **no separate `compile`**: `validate`'s build *is* the compile gate. Fusing
them is the efficiency win — a per-component dry-run before the first fuzz roughly doubled the e2e
— and it is why `ValidateOutcome` has a `BuildFailed` arm at all. Because the units share one
build, a `BuildFailed` from any target re-authors the whole spec.

`compile` is therefore called only for the two kinds that have no units to validate: `setup`
(via `author_and_compile`, which retries on compile failure and consults the judge post-compile) and
`preflight`.

An authoring turn that produces nothing costs an attempt like any other, but the toolchain never
sees it — there is nothing to build — so the next prompt is told exactly that (`_NO_ARTIFACT`).

### 4.5 Report and finalize

`fetch_verdicts` maps the wire verdicts `validate` baked into the result onto the report's own
`Verdict` (its `message` is the wire `detail`), filling `unit_file` from the component when the
wheel didn't name one. The store writes the per-component artifacts and metadata (§9), and
`finalize` receives the whole outcome set as `FinalizeInput` — program, crate, IDL path, the shared
setup artifact, and per component its `artifact_text`, `property_units` and the `targets` its rows
were validated by — and returns `{relpath: contents}` the host writes under the project root,
path-confined.

---

## 5. Authoring and review

The authoring turn is Python's ([`run_llm_agent`](../composer/rustapp/adapter.py)): it binds the
env's tool belt (source navigation + the declared corpus's RAG search) and a `result` tool, and runs
a bounded agent to completion. The wheel supplies only the prompt. `Prompt.system` is
backend-definable; `None` means the host's neutral default, which conveys the tool-using-agent and
result-tool contract and nothing domain-specific. The reply has any code fence stripped, because it
is written verbatim into a source file.

`Failure { draft, errors, kind }` is the revise context. The `draft` travels because each authoring
turn is fresh — the model has no memory of its prior attempt — and `kind` distinguishes
`Compile` from `Judge` so the wheel's revise prompt can frame review feedback as something other
than compiler errors (a judge rejection means the draft *did* compile).

**The judge runs in-loop.** When `judge_prompt` returns a prompt for an input (probed once with an
empty spec), the host binds a `request_review` tool into the author's own session and gates the
`result` tool on a draft the reviewer accepted — so the author self-revises against feedback within
one session instead of losing its context to a fresh retry. A wheel with no judge gets the plain
single-shot author. Three properties keep this from hanging:

- **A bounded budget** (`MAX_REVIEW_ROUNDS = 3`), because the loop can be *unwinnable*: when the
  objection is something the author has no power to change, revise-and-re-review never converges,
  it just re-spends a growing context per round.
- **Relenting is honest.** On the last round the hook returns a real `Accepted` carrying the open
  concerns labelled as unresolved — the author's gate opens, the draft goes forward, and the reason
  lands in the transcript. The compile gate and the checker still judge the result.
- **The verdict is a type, not a flag.** `Review = Accepted | Rejected`: the feedback string means
  different things on either side of an accept, and absence ("this wheel declares no judge") is
  `None` rather than a verdict standing in for it.

The reviewer is an **advisory** gate in front of the gates that actually decide, so it fails open:
an unparseable reply, or a judge turn that ends without stating a verdict, is read as acceptance
rather than burning a revise round on a verdict nobody stated. A JSON `{accept, feedback}` is
authoritative when present. Flipping that default is a policy decision, not a cleanup.

---

## 6. Units, targets and verdicts

`units(input)` is **pure and pre-authoring**: it is the report's property→unit map, the set of names
the prompt requires the author to produce, and the list the host validates against. It runs before
the first turn, so the report's shape never depends on what the model happened to write.

A `Unit` is `{ property, unit, target? }`. `target` is the **validation target the host runs**, and
several report rows may share one — a wheel can put a component's whole property set in a single
target. The host runs each *distinct* target once (`target_or_unit()`) and passes it as a
`Target { name, units }` carrying the rows it covers, so the grouping the host just computed is not
something the wheel has to reconstruct; the wheel returns a verdict **per unit in it**. Attribution is the wheel's: it owns its result
format, so it decides which unit a counterexample belongs to; the host records the verdicts
verbatim and does no verdict logic of its own, never parsing a tool's output.

Verdicts are grouped by property, not appended as singletons — two units checking the same property
are two unit names under one report row, and as singletons they would be two rows with the same key
that the store's `dict()` would silently collapse.

`Outcome` is a closed enum on both sides (`GOOD` / `BAD` / `ERROR` / `TIMEOUT` / `UNKNOWN`) — the
report's backend-agnostic vocabulary, whose human wording ("No counterexample" vs "Verified") is
picked at render time from `backend_tag`, so a wheel never spells it out. A typo that used to reach
a report row as an unexplained `UNKNOWN` now doesn't compile. From an *older or newer* wheel,
though, an outcome this host doesn't know is version skew rather than corruption, so the wire
validator downgrades it to `UNKNOWN` with a warning: losing one row's wording beats losing the run's
results. `Verdict.detail` carries the counterexample or error text, so a bare `BAD` is never
unexplained.

[`results.py`](../composer/rustapp/results.py) rolls these up for the console/TUI: one row per
*unit*, named by the property title it checks, with the tally in the report's own display order and
wording. A delivered component that bakes no verdicts contributes one `UNKNOWN` row so the listing
accounts for every component.

---

## 7. The toolchain seams

Two things the host needs to know about a project it does not itself understand — where its code
lives as a compilation unit, and how to prepare/build its workspace. Both are knowledge about the
**ecosystem under analysis**, not about implementing a backend in Rust, so
[toolchain.py](../composer/rustapp/toolchain.py) declares the seams and the application that needs
one registers an implementation per chain (shared by every wheel targeting it, exactly like the
ecosystem and RAG registries).

**`workspace_prep` is a pure plan the host executes.** The wheel returns
`WorkspacePrep { files, warm_dirs, build_program, idl_dest }` — file *contents* and declarative
intent, never a command line — and [`run_workspace_prep`](../composer/rustapp/adapter.py) writes the
files itself (path-confined) and hands the rest to the chain's `WorkspaceToolchain`. The split is
load-bearing for the network posture: the sandbox **never** gives a confined process network access,
so dependency fetches run *unconfined* (a fetch executes no untrusted code) and anything that
compiles runs *confined + offline*. Handing the wheel a confined-with-network policy would be a
brand-new security capability; declaring a plan is not.

`idl_dest` is the same shape for a derived *input* rather than an artifact: the toolchain resolves
the program's IDL (an operator-supplied file, else its own IDL build), writes it there, and the host
echoes the path back as `AuthorInput.idl` on every later callout — so a set `idl` means "the file is
in place". A hard error if it can't be produced: the wheel only asks when it
cannot proceed without one. This is what lets a harness target a program whose toolchain it cannot
link against — types generated from the IDL belong to the *wheel's* stack, so the program's own
dependency graph never enters the harness build.

**`ProgramCrate`** answers the other question. `AuthorInput.program` is only the *analysis*
identifier (the `Name` in `path:Name`); a crate's directory, package name and lib name are
independent of it and of each other (a real lending program: directory `programs/lend`, package
`example-lending`, lib `example_lending`). So the host resolves `{dir, package, lib, anchor}` from
the manifest that owns the main source file and carries it on every `AuthorInput`. `anchor` is the
crate's declared `anchor-lang` requirement, because a dependent wheel can only link the crate when
its Anchor *compatibility unit* matches (`anchor_compat`, where a `0.x` minor counts as a major):
Anchor's generated `InstructionData`/`ToAccountMetas` impls are tied to the exact `anchor-lang` that
generated them, so no amount of pinning makes a different major satisfy the trait bounds. That is
the fact that routes a wheel to the IDL path instead.

**Neither map has an entry today**, and they are unregistered in deliberately different ways:

- `source_crate` **degrades**. An all-empty `ProgramCrate` is already a documented state — it is
  what Solidity yields, and what an unreadable Rust layout yields — and the SDK's
  `ProgramCrate::resolved` fills the gaps from the `programs/<program>` convention. "No resolver" is
  indistinguishable from "nothing to resolve", which is the honest answer.
- `workspace_toolchain` **raises**. A plan that only places files never reaches it
  (`WorkspacePrep.needs_toolchain`), so getting there means the wheel asked for a warm/build/IDL
  nothing can perform. Skipping it silently would resurface much later as a mystifying compile error
  in the first authored draft — read as the authoring agent's fault.

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
property→units map, `token_usage.json`). All the subclass supplies is the descriptor's layout — and
the choice of how the *source* deliverable lands:

- **`per_component`** (default) — one `{prefix}_{slug}.{ext}` file per component, written from its
  `artifact_text` by the base writer.
- **`callout`** — the store writes **no** per-component source; it writes the shared metadata and
  returns the mode's `primary` (`{program}`-templated) as the component's report link. The whole
  deliverable comes from `finalize`, which is handed every component's `artifact_text`,
  `property_units` and `targets` plus the shared setup artifact, and can therefore assemble one
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
2. **Implement `Backend`** — `descriptor` + `units` + `author_prompt` + `compile` + `validate` are
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
- **The judge is advisory and fails open** (§5), and its budget relents rather than blocking
  finalization.
- **Prompt changes don't invalidate caches** — neither the setup artifact's nor the driver's. Clear
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
| The backend, loop, preflight, prep | [composer/rustapp/adapter.py](../composer/rustapp/adapter.py) |
| Application assembly (enum, phases, store, backend) | [composer/rustapp/host.py](../composer/rustapp/host.py) |
| Entry point / argparse / env | [composer/rustapp/entry.py](../composer/rustapp/entry.py) |
| Frontend, CLI, verdict rollup, store, result | [frontend.py](../composer/rustapp/frontend.py) · [cli.py](../composer/rustapp/cli.py) · [results.py](../composer/rustapp/results.py) · [store.py](../composer/rustapp/store.py) · [result.py](../composer/rustapp/result.py) |
| Chain seams (crate resolution, workspace toolchain) | [composer/rustapp/toolchain.py](../composer/rustapp/toolchain.py) |
| The driver this plugs into | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| Sandbox policy authoring | [composer/sandbox/](../composer/sandbox/) |

Tests: `tests/test_rustapp.py` (the wheel round-trip, end to end through the host),
`test_rustapp_wire.py` (ABI), `test_rustapp_preflight.py`, `test_rustapp_workspace_prep.py`,
`test_rustapp_setup_cache.py`, `test_rustapp_verdicts.py`, `test_rustapp_toolchain_sem.py`,
`test_rustapp_review_budget.py`, `test_rustapp_discovery_phase.py`, `test_rust_llm_agent.py`,
`test_rust_frontend.py`, plus `test_sandbox_run_confined.py` / `test_sandbox_escape.py` for the
launcher contract.
