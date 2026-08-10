# Solana units as abstract *components* (EVM parity)

> **Status: accepted; implemented — stages 1–5 of §13 landed.** Solana units are now per-component
> end to end: the model, prompt and validator (1), a backend-neutral property prompt (2), the
> `StagedFormalizer` seam for shared setup (3), `SolanaComponentInstance` + per-component extraction
> context (4), and per-component harness fns in the Crucible wheel (5). **Verified end to end by the
> Crucible e2e gate — §16.** Outstanding: stage 6 (measure). **§15 has the
> stage-1 measurement on a real ~60-instruction program** — the grouping is good, and K came in at 12
> rather than the 3–5 §11 guessed, which makes Crucible's aggregation choice (§8.3) a live decision.
>
> Response to review feedback that "instruction" is the wrong unit for Solana. Gives the **Solana
> ecosystem** the same **component** abstraction the EVM ecosystem already has, and works through
> what that means separately for each Solana *backend* — Crucible today, the Certora Solana Prover
> (CVLR) tomorrow. Supersedes [crucible-unit-granularity.md](./crucible-unit-granularity.md) (which
> chose the whole-program unit) and answers its §8 "EVM symmetry" open sub-question.
>
> **Naming discipline used throughout:** *Solana* = the ecosystem (the front half: system model,
> analysis + property prompts, unit split). *Crucible* = one Solana backend (a coverage-guided
> fuzzer). *CVLR / the Solana Prover* = a second, future Solana backend (symbolic). The unit
> choice is an **ecosystem** decision that both backends inherit; that is exactly why it needs to
> be made on backend-neutral grounds.
>
> **Decision rule:** every design question this note raises is settled by **mirroring current EVM
> behavior**, unless there is a specific reason not to. Exactly one deliberate divergence survives
> (the coverage validation, §7.3 rule 4), kept because it is cheap. §14 tabulates each resolution
> against what EVM actually does today.

## 1. The feedback, and what it is actually asking for

The objection is that an **instruction is a syntactic artifact, not a unit of behavior**. It is
the program's ABI surface, in the same way an external Solidity function is a contract's ABI
surface — and we do not fan out per Solidity function. A meaningful chunk of a Solana program is
a *capability*: "deposits and share accounting", "admin/config", "liquidation", "reward
accrual". Such a capability spans several instructions and owns some account types, and it is
the level at which an auditor states a property.

The ask is therefore: define the Solana unit as an **abstract component** — a named, semantic
cluster of the program's behavior produced by system analysis — exactly as EVM does with
`ContractComponent`.

One correction to the framing before going further: **Solana was not per-instruction when the
feedback landed.** That was true when the review was formed, but it changed in commit `fd51700`
and again in the collapse that followed, to **one whole-program unit** (`_solana_units` returned
`[main]`). So this change was not "instruction → component"; it was **"whole-program →
component"** — moving *coarser-to-finer*, back one notch on the spectrum
crucible-unit-granularity.md §3 laid out:

```
per-instruction   →   per-component (EVM-style)   →   whole-program
   rejected             shipped                        superseded
```

## 2. Where the unit choice lives: the ecosystem axis

[ecosystem-abstraction.md §1](./ecosystem-abstraction.md) draws the two axes:

```
     ┌─────────── ECOSYSTEM owns ───────────┐   ┌──────── BACKEND owns ────────┐
source ─analyze─▶ SystemModel ─extract─▶ properties ─formalize─▶ artifact ─verdicts─▶
```

`units: Callable[[Main], list[Unit]]` is on the **ecosystem** side. There is one `SOLANA`
ecosystem and there will be *n* Solana backends; every one of them fans out over whatever
`SOLANA.units` returns. Knowledge rides the backend axis (`rag_db_default`: `crucible_kb` vs
`cvlr_manual` — crucible-application.md §7.5), prompts and the unit split ride the ecosystem axis.

**This is the real problem the feedback was pointing at, stated precisely:** the old
`_solana_units → [main]` was a *Crucible* decision — chosen because Crucible's cost is K serialized
local builds and fuzz campaigns over the same global action space (crucible-unit-granularity.md
§7) — baked into the **ecosystem** seam, where a future CVLR backend would inherit it. A symbolic
prover has the opposite cost structure and would be actively harmed by a whole-program singleton
(§5.1). So even setting the property-quality argument aside, it was a backend-specific choice
sitting on a backend-neutral seam.

A second instance of the same leak, worth fixing alongside: `solana/property_prompt.j2` is
fuzzer-framed *throughout* ("You are performing a security review of a Solana program as a
whole, to produce program-level invariants **for a coverage-guided fuzzer**. The fuzzer drives
*random sequences* … and checks each property after every action"). The EVM peer
(`property_analysis_prompt.j2`) says nothing about symbolic execution; all backend-specific
framing goes through the `{{ backend_guidance }}` hole that each backend fills
(`CERTORA_BACKEND_GUIDANCE` at [prop_inference.py:82](../composer/spec/prop_inference.py#L82),
Crucible's `backend_guidance.j2`). Fixed in stage 2: the Solana prompt is now neutral and the
action-sequence framing lives in Crucible's guidance, so CVLR will not inherit a prompt telling it
to write fuzzer properties.

## 3. How the component abstraction works on EVM, end to end

Seven layers participate. This is the template to mirror. All seven are on the ecosystem axis —
neither the CVL/prover backend nor the Foundry backend contributes any of them.

| # | Layer | EVM | File |
|---|---|---|---|
| 1 | **Model type** | `ContractComponent` — `name`, `description`, `external_entry_points`, `state_variables`, `interactions`, `requirements` | [system_model.py:60](../composer/spec/system_model.py#L60) |
| 2 | **Owner** | `ExplicitContract.components: list[ContractComponent]` | [system_model.py:100](../composer/spec/system_model.py#L100) |
| 3 | **Analysis prompt** | "### Contract Components" — group related functionality, name it, attach entry points, state variables, requirements, interactions | [application_analysis_prompt.j2:114](../composer/templates/application_analysis_prompt.j2#L114) |
| 4 | **Validation** | `_validate_connectivity` — unique component names per contract, unique *slugs*, interactions resolve | [system_analysis.py:17](../composer/spec/system_analysis.py#L17) |
| 5 | **Unit wrapper** | `ContractComponentInstance` — index pair `(contract, component)` implementing `FeatureUnit` | [system_model.py:244](../composer/spec/system_model.py#L244) |
| 6 | **Enumeration** | `_evm_units(main) -> [ContractComponentInstance …]` | [ecosystem.py:133](../composer/pipeline/ecosystem.py#L133) |
| 7 | **Prompt context** | `application_context_new.j2` — this component + its sibling contracts | [application_context_new.j2](../composer/templates/application_context_new.j2) |

Everything between the ecosystem and the backend is generic: `_extract_all` fans out one
extraction agent per unit ([core.py:325](../composer/pipeline/core.py#L325)), `run_pipeline` fans
out one `formalize` per unit, the report builds one `ReportComponentInput` per unit keyed by
`display_name`, and per-unit cache keys come off `cache_material()`. The driver never learns what
a component *is*.

Two properties of the EVM design make the port cheap:

- **A component references, it does not own.** `external_entry_points` and `state_variables` are
  *lists of strings*. The component is a view over the contract, not a container for it. No
  partition invariant, no relocation of data.
- **A component is an authoring/attribution scope, not an execution scope.** This is what lets
  *two* EVM backends with completely different execution models share one unit split — see §5.

## 4. Where the Solana ecosystem stands today

> **This section describes the state this proposal replaced.** It is kept because §5's argument
> is about the *difference*; for what the code does now, see §7–§8 and the ✅ items in §13.

| Layer | Solana before (superseded) |
|---|---|
| Model type | none — `SolanaProgram.instructions` is a flat list ([solana/model.py:83](../composer/spec/solana/model.py#L83)) |
| Analysis prompt | "#### Instructions" — flat enumeration, no grouping ([solana/analysis_prompt.j2](../composer/templates/solana/analysis_prompt.j2)) |
| Validation | `_solana_validate` — unique program ids, unique instruction slugs, main present ([ecosystem.py:259](../composer/pipeline/ecosystem.py#L259)) |
| Unit wrapper | `SolanaProgramInstance` doubles as `Main` **and** the single unit ([solana/model.py:136](../composer/spec/solana/model.py#L136)) |
| Enumeration | `_solana_units(main) -> [main]` ([ecosystem.py:304](../composer/pipeline/ecosystem.py#L304)) |
| Prompt context | `solana/program_context.j2` — the whole program's instruction list |

Two dead-ish types also remained from earlier iterations — `SolanaInstructionInstance` (only an
unused import plus docstrings) and `SolanaInvariantUnit` (referenced only by tests). Both are now
deleted, along with the `program_context.j2` / `instruction_context.j2` templates they served.

## 5. What the unit choice means for each backend

This is the section the ecosystem/backend distinction buys us. The two Solana backends have
**opposite cost structures and opposite attribution mechanics**, so the same unit split lands very
differently on each. The EVM precedent is directly informative, because EVM already runs two
backends (CVL/prover and Foundry) over the *same* `ContractComponentInstance` split.

### 5.1 The Prover (CVL today, CVLR for Solana tomorrow)

What the EVM prover backend actually does with a component unit, per unit:

- authors one spec file, `autospec_<slug>.spec`
  ([artifacts.py:29](../composer/spec/source/artifacts.py#L29)), containing one or more CVL
  **rules per property**;
- that spec `import`s the shared, run-level `invariants.spec` (the staged structural invariants
  authored once in `prepare_formalization`);
- submits **one prover run** keyed by the component slug
  (`ComponentSpec.run_key`; the run map is written to `components_to_prover_runs.json`);
- verdicts come back **per rule** — i.e. per property, exactly attributed, with the rule name
  naming the violated property ([report_prover.py](../composer/spec/source/report_prover.py)).

The economics: prover runs execute **in the cloud, concurrently**. `run_pipeline` gathers all
units' `formalize` calls with `asyncio.gather` ([core.py:269](../composer/pipeline/core.py#L269))
and nothing serializes them. So for the Prover, **more units is close to free in wall-clock and
strictly better in every other dimension**:

- **Proof modularity.** A spec's rules are checked against the contract's methods; one giant spec
  covering every property of a whole program is a large, slow, timeout-prone run in which one
  intractable rule can starve the rest. Splitting is the standard mitigation. This is precisely
  what crucible-unit-granularity.md §5 meant by "its per-component split is about *proof
  modularity*, not scenario construction."
- **Failure isolation.** One spec that fails to typecheck loses one component, not the program.
- **Exact attribution already.** No string tagging is needed — the rule name *is* the property.

Now apply today's Solana unit to a hypothetical CVLR backend: `units → [main]` means **one CVLR
spec module for the entire program, one prover run, every property in it**. That is the
configuration the EVM prover backend has never used and would not choose. It converts the
Prover's cheapest axis (parallel cloud runs) into its most expensive one (a single monolithic
run), for no benefit.

> **Confidence note.** CVLR does not exist in this repo yet — the grounding is
> [analyzer/analysis.py:161](../analyzer/analysis.py#L161) ("CVLR — Certora Verification Language
> for Rust — a DSL embedded into Rust", Solana, language Rust) and crucible-application.md §7.5's
> roadmap statements. The claims above are about *shape* — Rust-embedded rule functions, symbolic
> evaluation, one prover job per spec unit, per-rule verdicts — which follow from it being the
> Certora Prover, not from any specific API. The exact per-unit artifact (a `certora/` rule module,
> a Cargo feature per rule set, …) is for the CVLR backend design to settle.

**The one thing that genuinely differs for CVLR, and it is not the unit:** a symbolic prover does
not execute action sequences. A CVLR rule sets up nondeterministic state, invokes an entry point,
and asserts — it does not drive a random instruction sequence. So the *kind* of property that
suits CVLR differs from what suits Crucible (inductive state invariants and per-entry-point
pre/post conditions, versus "no sequence of actions drains the escrow"). That is a
`backend_guidance` concern (§2), not a unit concern — exactly as it is on EVM, where CVL and
Foundry share `ContractComponentInstance` but receive different guidance.

### 5.2 Crucible

What Crucible does with a unit today, and would do per component:

- authors one `#[invariant_test] fn` into the **shared** harness crate `certora/crucible/fuzz/<program>/`
  (the analogue of the prover's shared `invariants.spec` is the shared `Fixture`);
- builds that crate selecting the fn's Cargo feature, then runs **one fuzz campaign**
  (`crucible run <program> <fn> --mode explore`);
- verdicts are per **target**, not per property: every property in the unit shares the one fuzz
  run, and a counterexample is attributed back to a property by parsing the `[<property title>]`
  prefix the author is instructed to put on each assertion message
  ([crucible-app/src/lib.rs:997](../rust/crucible-app/src/lib.rs#L997)).

The economics are the mirror image of the Prover's. Builds and campaigns are **local and
serialized** — a single-permit semaphore, because all units share one crate and one `target/`
([adapter.py:850](../composer/rustapp/adapter.py#L850), `serialize_toolchain: true`; the reasons
per-crate isolation does not work are crucible-unit-granularity.md §7). So for Crucible, **more
units costs real wall-clock, linearly** (§9), which is exactly why the collapse to one unit
happened.

And the *execution* scope cannot be narrowed to match the unit even if we wanted it to: the
fixture is one `#[fuzz_fixture] impl` with `action_*` methods driving every instruction, and
`explore` mode drives random sequences across all of them. You cannot fuzz "just the deposit
component" — nor should you want to, since the interesting violations are cross-instruction.

So for Crucible a component is an **authoring and attribution scope over a whole-program
execution**. That is not a compromise; it is the same shape Foundry already has on EVM — Foundry's
stateful `invariant_*` runner calls *all* the contract's functions in random sequences while its
authoring stays per component. It is also why the per-instruction design was wrong and this one is
not: per-instruction framed the *properties* instruction-locally; components do not — a
component's properties are still stated over any reachable action sequence, just *about* one
capability's state.

### 5.3 Side by side

| | EVM Prover (CVL) | Solana Prover (CVLR, sketch) | Crucible |
|---|---|---|---|
| Execution model | symbolic, all states, no sequences | symbolic, all states, no sequences | concrete, random action sequences |
| Per-unit artifact | `autospec_<slug>.spec` | a CVLR rule module per unit | one `#[invariant_test] fn c_<slug>` in the shared crate |
| Shared prelude | `invariants.spec`, imported | presumably a shared setup/mock module | the shared `Fixture` |
| Per-unit run | one prover job | one prover job | one build + one fuzz campaign |
| Where runs execute | cloud, **concurrent** | cloud, **concurrent** | local, **serialized** |
| Marginal cost of +1 unit | ≈ 0 wall-clock | ≈ 0 wall-clock | ≈ 170 s (§9) |
| Verdict granularity | per **rule** = per property, exact | per rule, exact | per **target**; property attributed by string tag |
| Harm from too few units | monolithic slow run, timeouts, no isolation | same | none — this is today's optimum |
| Harm from too many units | none material | none material | linear wall-clock |

The table makes the trade explicit: **the Prover pays nothing for finer units and gains
modularity; Crucible pays linearly and gains isolation plus property quality.** A component-level
split — a handful of units, not one and not one-per-instruction — is the point that serves both.
It is not a coincidence that this is where EVM landed with two backends of its own.

### 5.4 The unit is shared; the aggregation is not

The ecosystem fixes *what a unit is*. It does **not** fix how a backend aggregates properties
within or across units — that is `Formalizer.formalize`'s business, and the two backends should
differ freely:

- **Prover / CVLR:** one artifact per unit, one rule per property, one run per unit. Maximum
  fan-out, because fan-out is free and modularity is valuable.
- **Crucible:** one harness fn per unit holding *all* that unit's properties (already how it works
  within the single unit today) — and, if wall-clock demands it, the freedom to aggregate
  *further*, collapsing several units back into one build + campaign. §8.3 sketches that knob.

Making this explicit matters because it defuses the strongest objection to component units — "it
will make Crucible 12 minutes slower." Crucible retains the option to re-collapse; what it cannot
recover, if the ecosystem hands it a single whole-program unit, is per-component extraction,
failure isolation, and a grouped report. Coarse aggregation is a backend's prerogative; coarse
*units* take the choice away from every backend.

## 6. Can the EVM approach be used for the Solana ecosystem? Yes

Layers 1–7 of §3 have exact Solana analogues:

| EVM concept | Solana analogue |
|---|---|
| `ExplicitContract` | `SolanaProgram` |
| `external_entry_points: list[str]` (function signatures) | `instructions: list[str]` (instruction names) |
| `state_variables: list[str]` | `account_types: list[str]` (already a field on `SolanaProgram`) |
| `interactions` (component / external actor) | CPI targets + `SolanaAuthority` |
| `requirements` | `requirements` (already per-instruction; lifts to the component) |
| slug-uniqueness validation | same, over component names within a program |

The one caveat — that Crucible's execution scope stays whole-program — is discharged in §5.2: it is
the Foundry situation, not a new problem, and it does not touch CVLR at all.

The second caveat, smaller and Crucible-only: **attribution still needs the string tag.** With K
components, a component's properties still share one fuzz target, so the `[<title>]` prefix
mechanism stays. Components narrow the blast radius (a build failure or an unattributable crash
marks one component's rows, not the whole program's); they do not remove the tag. CVLR has no such
problem — its rule names carry the attribution natively.

## 7. The ecosystem-level design

### 7.1 Model: `ProgramComponent`, by reference

```python
class ProgramComponent(BaseModel):
    """A named capability of a program — a semantic cluster of its instructions and the
    account state they maintain. The Solana analogue of ContractComponent."""
    name: str                        # short, unique within the program
    description: str                 # what it does, not how
    instructions: list[str]          # names of SolanaProgram.instructions in this component
    account_types: list[str]         # names of SolanaProgram.account_types it maintains
    interactions: list[Interaction]  # other components / CPI targets / authorities
    requirements: list[str]          # the component's behavioral specification


class SolanaProgram(BaseModel):
    ...
    instructions: list[SolanaInstruction]     # unchanged — still flat and authoritative
    account_types: list[str]                  # unchanged
    components: list[ProgramComponent]        # NEW
```

**Reference, not nesting** — components name instructions, they do not contain them. Considered
and rejected: moving `SolanaInstruction` objects *inside* components. Reference wins because:

- `SolanaProgram.instructions` stays the single source of truth for the rich per-instruction data
  (accounts, constraints, CPIs, signers). Nesting would force a strict partition and duplicate or
  scatter that data.
- Backends that read the flat list keep working untouched — Crucible's `api_facts` block
  ([crucible-app/src/lib.rs:501](../rust/crucible-app/src/lib.rs#L501)) builds the "PROGRAM API
  FACTS" the fixture author depends on straight off `prog.instructions`, and the shared fixture is
  whole-program by construction. A CVLR backend enumerating entry points would likewise be
  unaffected.
- It matches EVM exactly (`external_entry_points` is a list of strings).
- An instruction may legitimately belong to two capabilities (`close_position` is both "positions"
  and "liquidation"), which a partition forbids.

Resolution belongs on the *instance wrapper*, not at the call sites — a `component.instructions`
property returning the `SolanaInstruction` objects, so no backend re-does name lookup.

> Naming note: `SolanaApplication.components` (programs + authorities, from `BaseApplication`) and
> `SolanaProgram.components` (capabilities) collide in name. EVM has exactly this collision already
> (`Application.components` vs `ExplicitContract.components`), so the precedent is consistent even
> if the word is overloaded.

### 7.2 Unit wrapper: `SolanaComponentInstance`

Mirrors `ContractComponentInstance` — an index pair over `(program, component)` implementing
`FeatureUnit`:

| `FeatureUnit` member | Value |
|---|---|
| `display_name` | `component.name` |
| `slug` | `slugify_filename(component.name)` |
| `unit_index` | component index |
| `cache_material` | `app JSON \| program ind \| component ind` |
| `context_tag` | `{"component": component.model_dump()}` |
| `feature_json` | `component.model_dump(mode="json")`, with `instructions` resolved to the full `SolanaInstruction` objects and a `slug` field added |

**Scope: component-only, mirroring EVM** (`ContractComponentInstance.feature_json` is
`self.component.model_dump(mode="json")` — the component, and nothing else; `context_tag` likewise,
[system_model.py:294](../composer/spec/system_model.py#L294)). An earlier draft of this note had
`feature_json` also carry the whole program's instruction list; that is dropped in favour of EVM
parity. It costs nothing, because the whole-program surface already reaches each backend by its own
route:

- **Crucible** — the shared `Fixture` source is on every `AuthorInput.setup` and
  rendered verbatim in the author prompt, and it contains every `action_*` method. The author
  therefore still sees the full action surface; it just arrives as the fixture (a backend artifact)
  rather than as the unit (an ecosystem value). That is the correct axis for it.
- **CVLR** — the analyzed model is available to `prepare_system` as it is on EVM.

Two deviations from a bare `model_dump` are mechanical rather than semantic: `instructions` is
resolved from names to the `SolanaInstruction` objects (the §7.1 wrapper property — EVM's
`external_entry_points` are already self-contained strings, so it has nothing to resolve), and
`slug` is carried because it becomes Crucible's harness function name (§8.1) and must not be
re-derived backend-side.

### 7.3 Analysis prompt + validation

**Prompt** ([solana/analysis_prompt.j2](../composer/templates/solana/analysis_prompt.j2)): add a
"#### Components" subsection under Programs, modeled on the EVM "### Contract Components" text.
Solana-specific guidance to include:

- Group by *capability and shared account state*, not by call order or file layout. Instructions
  that read/write the same PDA or state account almost always belong together. (The Solana
  analogue of EVM's "a well-known interface like ERC20 is a natural fit for a component".)
- Admin/configuration instructions usually form their own component — access-control properties
  cluster there.
- Every instruction must appear in at least one component; an instruction may appear in more than
  one when it genuinely serves two capabilities.

**No target count** — mirroring EVM, whose prompt gives none and lets the grouping fall out of the
contract. Adding a numeric range for Solana would be an unforced divergence; if real programs come
back badly grouped (staging step 1), that is the evidence for adding one, to *both* ecosystems.

**Validation** (`_solana_validate`, [ecosystem.py](../composer/pipeline/ecosystem.py)) — add, with
the same retry-feedback style as the EVM validator:

1. Component names unique within a program; component **slugs** unique within a program (they name
   files, Cargo features and Rust fns). *EVM peer: direct.*
2. Program **names** unique (the existing validator only checked `program_identifier`). Needed
   because an interaction names a program by `name`. *EVM peer: `Duplicate contract names`.*
3. Component interactions resolve — to a declared program, to a declared component of it, or to a
   declared external authority. Resolved in a **second pass**, so a component may name one declared
   after it. *EVM peer: direct.* Note this is stricter than the existing `CpiCall.target_program`
   leniency, deliberately: the analysis prompt requires every external actor to be declared,
   including SPL Token and the System program.
4. The **component↔instruction mapping is valid and total**: every name in `component.instructions`
   resolves to a declared instruction, and every instruction is named by ≥ 1 component. Overlap is
   allowed and not flagged.
5. On any failure, append the **declared-names reference block** ("For reference, the names you
   declared in your submission: …") — every rule above fails on a name, so the retry needs the
   vocabulary. *EVM peer: direct.*

Rule 4 is the **one deliberate divergence** from EVM (§14 Q5), and it is one rule rather than the
two an earlier draft listed: reference-resolution and coverage are two halves of the same claim,
and coverage alone would be meaningless if a typo'd name silently resolved to nothing. Cheap — a
dict lookup and a set difference in a validator that already walks both lists. Not backported to
EVM.

Two rules the earlier draft listed are **dropped**, both for want of an EVM peer:

- *Every `component.account_types` entry resolves.* EVM validates `state_variables` against
  nothing, and `SolanaProgram.account_types` is documented as free text ("name & purpose"), so a
  component saying `"Vault"` against a program saying `"Vault — the per-user PDA"` would fail an
  exact match. High false-positive rate, no precedent; dropped.
- *At least one component per program.* Subsumed: rule 4 already forces a component to exist
  whenever the program has an instruction, and a program with neither is legitimately empty. EVM
  has no such rule either.

### 7.4 Property extraction

New `solana/component_context.j2`, included by `solana/property_prompt.j2` in place of
`program_context.j2` (now deleted). It mirrors `application_context_new.j2` **section for
section**:

| `application_context_new.j2` renders | `solana/component_context.j2` renders |
|---|---|
| the component's name, description, requirements | the same |
| the parent contract's name + the application type | the parent program's name + the application type |
| `ommer_contracts` — the *other contracts*, name + description + sort | the *other programs*, name + description |
| `component.interactions` — other components / external actors | the same, plus CPI targets and authorities |

Plus one Solana-specific addition with no EVM peer: full account + constraint detail for *this
component's* instructions — the detail `instruction_context.j2` used to render (that template is
currently orphaned and can be harvested). EVM needs no equivalent because a Solidity function's
signature carries its interface; a Solana instruction's does not, and the account constraints are
where the properties come from.

Note what this *drops* relative to the earlier draft, again for EVM parity: no summary list of the
program's other instructions. EVM's component context does not enumerate sibling components'
entry points, so neither does this. The interleaving concern that motivated it is Crucible's alone
and is handled backend-side (§8.1).

**Neutralize the execution-model framing while here** (§2). The prompt's task text currently
hard-codes the fuzzer; it should describe the component and the property categories, and leave
"must hold across any reachable action sequence" (Crucible) versus "must hold inductively /
per-entry-point" (CVLR) to `{{ backend_guidance }}`. Without this, CVLR inherits a prompt that
asks for fuzzer properties, and the ecosystem seam stays leaky in the same way `units` is today.

## 8. Backend impact: Crucible

### 8.1 The wheel changes

| Concern | Today | With components |
|---|---|---|
| Harness fn | one `const SINGLE_HARNESS_FN = "c_invariants"` ([:37](../rust/crucible-app/src/lib.rs#L37)) | `c_<component_slug>`, read from `input.component`; one fn per component. **Spelled as a Rust ident** — see below |
| `units()` ([:810](../rust/crucible-app/src/lib.rs#L810)) | row `c_<prop_slug>`, `target = c_invariants` | row `c_<prop_slug>`, `target = c_<component_slug>` — **no structural change, just a different target string** |
| `author_prompt` ([:835](../rust/crucible-app/src/lib.rs#L835)) | `harness_fn = SINGLE_HARNESS_FN`, `component` = whole-program API | the component's fn; `component` = the component only (§7.2). Prompt gains "this suite covers the *X* capability; **the fuzzer also drives the other components' actions in the same sequence**, so the invariant must survive them" |
| `compile` / `validate` ([:914](../rust/crucible-app/src/lib.rs#L914), [:951](../rust/crucible-app/src/lib.rs#L951)) | feature = `SINGLE_HARNESS_FN` | feature = the component's fn. Both already take it as a variable — one-line changes |
| `finalize` ([:1058](../rust/crucible-app/src/lib.rs#L1058)) | dedupes N identical sections, declares `features = [SINGLE_HARNESS_FN]` | one section per component, `features` = the delivered components' fns. The dedupe added in `574bf3f` becomes a no-op rather than being removed |
| `component_noun` ([:740](../rust/crucible-app/src/lib.rs#L740)) | `"instruction"` (already stale) | drop the override, taking the SDK's `"component"` default |

`finalize` needs the target names and today gets only `entry["name"]` (the display name) and
`property_units` (row names). Re-deriving `c_<slug>` on the Rust side by re-slugifying the display
name would put the same slug rule in two languages and smuggle a semantic value through a string —
don't. Thread the real value instead: carry the distinct targets on `RustFormalResult` (the adapter
already computes them at [adapter.py:740](../composer/rustapp/adapter.py#L740)) and mirror them
into the outcome entry at [adapter.py:804](../composer/rustapp/adapter.py#L804).

**The slug is filesystem-safe, not identifier-safe — the wheel must bridge that.** `slugify_filename`
permits `A-Za-z0-9_-`, which is the right guarantee for its original job (EVM spec *filenames*) but
weaker than what a Rust identifier needs, and the harness fn name is simultaneously a Rust fn, a
Cargo feature, and the `crucible run <program> <fn>` selector a human types. So `harness_fn` folds
the slug through `ident_of`: lowercased, non-`[a-z0-9_]` → `_`. Two concrete failures that avoids —
both observed, the first in a real run:

- **Capitals.** A component named "Vault Initialization" slugs to `Vault_Initialization`, giving
  `fn c_Vault_Initialization` — a `non_snake_case` warning (so it *compiles*, which is why the first
  green e2e shipped it) and a case-sensitive string the user has to type exactly.
- **Hyphens.** "Admin-Config" slugs to `Admin-Config`, and `fn c_Admin-Config` is a **syntax error**.
  A Cargo feature may contain `-`, so the manifest would look correct while `src/main.rs` failed to
  compile — and only a component whose name happens to contain a hyphen would trigger it.

The fix belongs here, not in `slugify_filename`: changing that would rename every EVM spec artifact
as a side effect, and the per-property report rows should keep the component's own casing.

**Where the whole-program action surface now comes from.** With `component` narrowed to the
component (§7.2), the author prompt's "Program API (drive instructions via the fixture's
`action_*` methods)" block covers only this component's instructions. Nothing is lost: the prompt
already renders the **entire fixture source** below it
([author_component.j2](../rust/crucible-app/templates/author_component.j2) — "Fixture source for
reference"), and that fixture is whole-program, so every `action_*` the fuzzer can drive is in
front of the model either way. The change is which axis carries it — a backend artifact rather than
an ecosystem value — which is the right one, and the reason the ecosystem can mirror EVM here
without Crucible paying for it.

### 8.2 The shared fixture — the one genuine blocker

`_ensure_setup` ([adapter.py:653](../composer/rustapp/adapter.py#L653)) authors the shared fixture
**once**, lazily, from whichever unit calls `formalize` first, under a lock. Its docstring is
explicit: *"the first component's properties inform the artifact the rest share."*

With one unit that is harmless — the single batch *is* every property, so the fixture is authored
knowing everything it must support. **With K components it silently regresses**: the fixture — the
thing that decides which `action_*` methods exist, and therefore which properties are checkable at
all — would be authored from one arbitrary component's properties, and the other K−1 components
would then be told "if a property cannot be checked with the actions the fixture provides, do not
fake it" ([author_component.j2](../rust/crucible-app/templates/author_component.j2)). The likely
outcome is a run full of honest `// UNCOVERABLE` comments.

This must be fixed as part of the change, not after. The driver already holds every batch before it
fans out (`batches` at [core.py:233](../composer/pipeline/core.py#L233)), so the fix is a
pre-fan-out step receiving all batches, with the Rust adapter authoring the setup artifact there
from the union of all properties. That replaces the lazy lock with an explicit phase, removes the
first-caller-wins nondeterminism, and is a strict improvement even at K = 1. (As landed, that step
is `StagedFormalizer.begin`, which *returns* the `Formalizer` — see §13.3.)

Note this is a **generic setup-step concern, not a Crucible one**: any backend with a shared setup
artifact — plausibly including CVLR, whose rules will need shared mocks/setup — hits it the moment
there is more than one unit. The EVM prover's peer (`invariants.spec`, staged in
`prepare_formalization`) is already authored before fan-out and does not have the bug.

### 8.3 Crucible's aggregation options (its choice, not the ecosystem's)

Given K component units, Crucible may pick where it sits on cost-vs-isolation:

- **C1 — one harness fn per component** (the §8.1 proposal). K builds, K campaigns, serialized.
  Full failure isolation and per-component report rows. Cost: §9. **Resolved: this is what we
  ship.** It is the EVM Prover's shape — one artifact per unit, one run per unit, the unit's
  properties aggregated into it — and each campaign gets the *full* fuzz budget, mirroring the way
  each EVM component gets its own prover run at the full timeout rather than a split of one.
- **C2 — per-component fns, one campaign each, but split build from fuzz.** Build the K binaries
  serially (cheap incremental on the shared crate), then fuzz them in parallel via
  `crucible run --binary-in`. This is the deferred project from crucible-unit-granularity.md §7;
  component units make it materially more valuable, since K parallel campaigns of a few minutes is
  exactly the workload it was designed for. Not required for this change.
- **C3 — re-collapse.** Author one fn holding every unit's properties, as today: one build, one
  campaign. Needs the same pre-fan-out hook §8.2 introduces (the backend must see all batches).
  Keeps today's wall-clock and keeps per-component *extraction* and report grouping, but gives up
  failure isolation. A config lever if K turns out to hurt.

C2 and C3 stay documented but unbuilt. They remain available precisely because aggregation is a
Crucible-internal decision that changes no ecosystem code and does not affect CVLR — the point of
§5.4 — so reaching for one later costs nothing now.

## 9. Backend impact: the Solana Prover (CVLR) — sketch

No CVLR backend exists, so this is a forward-looking sketch of what component units set it up for.
Marked speculative where it is.

- **Per-unit artifact.** One CVLR rule module per component — the direct analogue of
  `autospec_<slug>.spec` — holding one `#[rule]`-style function per property, named after the
  property. *(Shape follows from the EVM prover backend; the concrete layout is the CVLR backend's
  to design.)*
- **Shared prelude.** Whatever shared setup/mock/summary module CVLR needs, authored once in
  `prepare_formalization` and imported by each unit's module — the peer of `invariants.spec` and of
  Crucible's `Fixture`. §8.2's pre-fan-out hook covers it.
- **Runs.** One prover job per component, submitted concurrently; verdicts per rule, exactly
  attributed. No string-tag attribution needed.
- **Why component units specifically.** Per-instruction would be wrong for the same reason it is
  wrong on EVM (we do not fan out per Solidity function, and a rule about "deposits and share
  accounting" is not about one entry point). Whole-program would be wrong for the reason §5.1
  gives — one monolithic run, no modularity, no isolation, wasting the free parallelism. Component
  is the point that has already survived contact with two EVM backends.
- **What CVLR needs that Crucible does not.** Different `backend_guidance` (§7.4): inductive
  invariants and per-entry-point pre/post conditions rather than sequence-violation framing; and
  guidance on what a symbolic tool cannot do, modeled on `CERTORA_BACKEND_GUIDANCE`
  ([prop_inference.py:82](../composer/spec/prop_inference.py#L82)) but Solana-flavored. Neither is
  a unit concern.
- **What it inherits for free** if this proposal lands: the component model, the grouping prompt,
  the validation, the unit wrapper, the per-component extraction context, and the pre-fan-out
  setup hook. The CVLR backend then contributes only its `Formalizer` and its guidance — which is
  what the two-axis design promises.

## 10. Change list

| Axis | Area | File | Change |
|---|---|---|---|
| Ecosystem | Model | [composer/spec/solana/model.py](../composer/spec/solana/model.py) | add `ProgramComponent`, `SolanaProgram.components`, `SolanaComponentInstance`; retire `SolanaInvariantUnit` / reconcile `SolanaInstructionInstance` |
| Ecosystem | Seam | [composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py) | `_solana_units` → one instance per component; extend `_solana_validate` (§7.3); retype `SOLANA` to `Ecosystem[SolanaApplication, SolanaProgramInstance, SolanaComponentInstance]` |
| Ecosystem | Prompts | `templates/solana/analysis_prompt.j2` | add the "#### Components" section |
| Ecosystem | Prompts | `templates/solana/component_context.j2` (new) | per-component context; harvest the orphaned `instruction_context.j2` |
| Ecosystem | Prompts | `templates/solana/property_prompt.j2`, `property_system.j2` | component framing; `unit_noun = "component"`; **move fuzzer framing out to `backend_guidance`** (§7.4) |
| Shared | Driver | [composer/pipeline/core.py](../composer/pipeline/core.py) | add the pre-fan-out `StagedFormalizer` step (§8.2) — generic, benefits any backend with a shared setup artifact |
| Shared | Rust host | [composer/rustapp/adapter.py](../composer/rustapp/adapter.py) | author setup from the union of batches; carry targets on `RustFormalResult` into the outcome entry |
| Backend | Crucible wheel | [rust/crucible-app/src/lib.rs](../rust/crucible-app/src/lib.rs) | per-component harness fn (§8.1): `units`, `author_prompt`, `compile`, `validate`, `finalize`, `component_noun` |
| Backend | Crucible wheel | `rust/crucible-app/templates/author_component.j2`, `judge_instruction.j2`, `backend_guidance.j2` | component framing; absorb the action-sequence guidance moved out of the ecosystem prompt |
| Docs | — | [crucible-unit-granularity.md](./crucible-unit-granularity.md), [crucible-application.md §10 Q1](./crucible-application.md), [ecosystem-abstraction.md §4](./ecosystem-abstraction.md) | all three describe the whole-program split and hooks (`collapse_units`, `global_extraction`/`extraction_unit`/`property_unit`) that no longer exist; correct while here |
| Tests | — | `tests/test_crucible_granularity.py` | rewrite around component units |
| Tests | — | `tests/test_solana_gate.py`, `test_null_solana_backend.py`, `test_crucible_*_gate.py` | fixtures gain `components`; the e2e gate asserts the delivered crate compiles with K harness fns |

## 11. Cost, per backend

**Prover / CVLR: ≈ 0.** Runs are cloud-side and concurrent; K units means K concurrent jobs
instead of one monolithic one. Net expected *improvement* in wall-clock and in timeout risk.

**Crucible: linear in K.** Formalization is the dominant cost and it serializes — one build
semaphore over a shared crate ([adapter.py:850](../composer/rustapp/adapter.py#L850)), for the
reasons crucible-unit-granularity.md §7 documents (per-crate `target/` recompiles the heavy deps; a
shared `CARGO_TARGET_DIR` collides on the hardcoded `invariant_test` binary name). From the two
measured e2e runs on the same 13-property vault:

| Configuration | Units | Measured |
|---|---|---|
| Per-invariant | 13 | 1:33:20 |
| Whole-program (today) | 1 | 0:59:22 |

≈ 170 s of marginal wall-clock per extra unit. Extrapolating linearly — caveats being that
component authoring turns are longer than single-property ones, and that the e2e's fuzz budget is
small:

| K components | Estimated Crucible e2e | |
|---|---|---|
| 1 (today) | 0:59 | |
| 3 | ≈ 1:08 | |
| 5 | ≈ 1:11 | |
| **12** | **≈ 1:31** | **measured on a real ~60-instruction program — §15** |

> **Read the small-K rows as a floor.** This table was written against a guessed K = 3–5. §15
> measures **K = 12** on a real 62-instruction program, i.e. ≈ +31 min and essentially the 1:33 the
> per-invariant collapse was meant to escape. K scales with program size, so Crucible's cost scales
> with program size *twice* — a bigger program means both longer campaigns and more of them. C3
> (§8.3) buys it back while keeping everything §15 shows is valuable, which is an argument for
> making C1/C3 a **per-program** choice rather than a global one.

Extraction fans out K ways but runs concurrently, so it is free for both backends. At **production
fuzz budgets** (60–300 s vs. the e2e's seconds) Crucible's fuzz portion scales K× and becomes the
bottleneck, which is the trigger for C2 / `--binary-in`.

**What the cost buys, by backend:**

| | Prover / CVLR | Crucible |
|---|---|---|
| Property quality (K focused extraction agents vs. one capped pass) | ✔ | ✔ |
| Failure isolation | ✔ (one spec fails, not all) | ✔ (one harness fails, not all) |
| Proof/run modularity | ✔ (avoids one monolithic job) | — (execution is global regardless) |
| Sharper attribution | — (already exact per rule) | ✔ (narrows the tag's blast radius) |
| Grouped, readable report | ✔ | ✔ |
| Less special-casing | ✔ (Solana stops being the singleton ecosystem) | ✔ (`component_noun` stops lying) |

The property-quality row is worth a sentence: today one extraction agent produces the *entire*
program's property set in one pass — a hard cap on depth for anything larger than the vault. K
focused agents each go deeper on one capability, and the "quality over quantity" prompt guidance
stops fighting the breadth of the context. That benefit accrues to **both** backends and is
independent of how either aggregates afterwards.

## 12. Alternatives considered

- **B. Cluster the invariants, not the instructions.** Keep whole-program extraction, then group
  the resulting properties into K clusters (LLM or heuristic) and author per cluster. No
  analysis-model change; recovers failure isolation and the grouped report. But it buys *none* of
  the property-quality win — the grouping is post-hoc, so the single extraction agent is still the
  cap — and the clusters aren't grounded in the program's structure. Note it is also a *backend*
  technique, not an ecosystem one: Crucible could do it unilaterally today, and CVLR would not
  inherit it. Cheap; strictly weaker. A reasonable fallback if the analysis-model change proves
  unreliable.
- **C. Derive components mechanically** from module layout / IDL namespaces. No LLM grouping risk,
  but source layout is a poor proxy for capability and cannot express a capability spanning
  modules. Rejected.
- **D. Do nothing.** Defensible for small programs — the vault has 13 properties and one component
  would be an honest grouping. Weakest point of this option: it is a bet that Crucible stays the
  only Solana backend, since a CVLR backend inherits the singleton unit and the fuzzer-framed
  property prompt on day one.

## 13. Staging

Each step is independently shippable and revertable.

1. ✅ **Model + analysis + validation** (§7.1, §7.3) — **done, and the de-risking gate passed.**
   Ecosystem-only: the model gains `components` and the analysis prompt fills them in;
   `_solana_units` still returns `[main]`, so no backend changes and no wall-clock change.
   Validated on a real ~60-instruction lending protocol — 12 components, total and
   non-overlapping, covering five capabilities the whole-program unit was blanking on (**§15**).
   Repeat the probe on a second program of a different shape before treating the grouping question
   as closed.
2. ✅ **Neutralize the property prompt** (§7.4, §2) — **done.** `solana/property_prompt.j2` and
   `property_system.j2` no longer mention fuzzing, sequences or "after every action"; that framing
   moved into Crucible's `backend_guidance.j2`, where a CVLR backend will supply its own instead.
3. ✅ **Fix the shared-setup ordering** (§8.2) — **done.** `StagedFormalizer.begin(jobs, run)` on the
   driver, called once between extraction and the fan-out and *returning* the `Formalizer`; the Rust
   adapter's `RustStagedFormalizer` authors the setup artifact there from the de-duplicated union of
   every unit's properties, and the lazy first-caller-wins lock is gone. The ordering is carried by
   the types rather than by the driver's call order: a backend that needs no shared artifact returns
   a `Formalizer` directly, and one that does cannot produce a formalizer without the artifact.
4. ✅ **Switch `units` + the per-component prompt context** (§7.2, §7.4) — **done.**
   `SolanaComponentInstance` is the unit, `_solana_units` enumerates the main program's components
   (mirroring `_evm_units`), and `solana/component_context.j2` replaces the whole-program context.
   `SolanaProgramInstance` is no longer a `FeatureUnit` (main and unit are different axes, as on
   EVM); `SolanaInvariantUnit` and `SolanaInstructionInstance` are retired, and
   `program_context.j2` / `instruction_context.j2` are deleted.
5. ✅ **Per-component harness fn in the Crucible wheel** (§8.1) — **done.** `harness_fn(input)`
   reads the unit's slug (`c_<slug>`), `units()` targets it, `compile`/`author_prompt`/
   `judge_instruction` follow, and `finalize` emits one section and one declared feature per delivered
   component off the host's mirrored `targets`. `component_noun` drops to the SDK default.
   The e2e gate asserts the delivered crate compiles, **one build per feature** rather than one
   build with all K: each `#[invariant_test]` expands to its own `#[cfg(feature = …)] fn main()` and
   `#[global_allocator]`, so enabling two at once is a duplicate-`main` error by construction, and a
   feature selects a fuzz target. Run and passing — §16. Each section is additionally **sealed
   behind its own feature** so components cannot collide in the fold — §17, which is also where the
   limits of that e2e assertion are recorded.
6. **Measure** against the 0:59 baseline. C1 (§8.3) ships regardless; the measurement decides only
   whether C2/C3 ever become worth building.

Steps 1–3 are pure ecosystem/framework work that a CVLR backend would want regardless of what
step 6 measures.

## 14. Resolved questions — default to EVM behavior

**Decision rule: where a question has an EVM answer, Solana takes it.** These were open questions
in an earlier draft; each is resolved by mirroring what the EVM ecosystem does today. The point of
the exercise is parity, so a Solana-specific answer needs a reason, and "we thought about it harder
this time" is not one — if the EVM behavior is wrong, it is wrong on both and should be fixed on
both. Exactly one deliberate divergence survives (Q5), kept because it is cheap.

| # | Question | Resolution (EVM behavior) | Where it lands |
|---|---|---|---|
| 1 | How many components should the model produce? | **No target count.** The EVM analysis prompt gives no range and lets the grouping fall out of the contract. Solana does the same. | §7.3 |
| 2 | Per-component fuzz budget, or one budget split K ways? | **Full budget per component.** Each EVM component gets its own prover run at the full timeout, not a share of one; a Crucible campaign per component at the full budget is the same shape. | §8.3 (C1) |
| 3 | Should a unit's artifact see only its own component? | **Component-scoped context, no restriction on what the artifact reads.** EVM's `feature_json` is the component alone and its context template renders the component + sibling *contracts* — it does not enumerate sibling components' entry points. Solana matches; Crucible's whole-program action surface arrives via the shared fixture instead. | §7.2, §7.4, §8.1 |
| 4 | Multi-program applications — are a second program's components units? | **No — main program only.** `_evm_units` enumerates the *main contract's* components; siblings are context, not units. | §7.2 |
| 5 | Coverage validation — require every instruction in ≥ 1 component? | **Keep it on Solana; do not backport to EVM.** The one divergence. It is a set difference in a validator that already walks both lists, so it is cheap, and an unassigned instruction is an entry point no property will ever cover. EVM's `_validate_connectivity` has no such rule and is left alone. | §7.3 |
| 6 | Is `units` on the right axis? | **Yes — one split per ecosystem.** EVM demonstrates it with two backends (CVL + Foundry) over one `ContractComponentInstance` split. No per-backend override; §5.4's aggregation freedom covers the divergence backends actually need. | §2, §5.4 |

Two of these tighten the proposal relative to the earlier draft rather than merely settling it —
Q3 removes the extra whole-program payload from `feature_json` and the sibling-instruction summary
from the context template, and Q1 removes the invented "2–6" range. Both were Solana-only inventions
with no EVM counterpart, and neither survives the parity test.

### What stays genuinely open

Not questions this rule can answer — they are about things EVM has no position on:

- ~~**Whether the grouping is any good on real Solana programs.**~~ **Answered on one large
  program — see §15.** It groups cleanly and totally. Worth repeating on a second program of a
  different shape before treating it as settled.
- **Whether Crucible's K× wall-clock is acceptable at production fuzz budgets** (§11), which decides
  whether C2 / `--binary-in` gets built. **Sharper than it was**: §15 measures K = 12 on a real
  program, not the 3–5 §11 guessed, which puts C1 on a large program back at the wall-clock the
  collapse was
  meant to escape. Still does not block stages 2–4 (which are ecosystem work), but it now bears
  directly on stage 5.
- **CVLR's per-unit artifact layout** (§9) — a rule module per component is the shape, but the
  concrete form belongs to that backend's design, not this note.

## 15. Evidence: measured on a large real-world program

Stage 1's de-risking gate, run against a **customer lending protocol** — ~60 instructions, ~27k
lines of Rust across ~50 handler modules — roughly the largest thing a Solana backend will meet.
(Deliberately unnamed, and the findings below are paraphrased: it is not our code. Reproduce
against any workspace with `tests/test_solana_component_grouping.py`, which runs the analysis
phase only — no extraction, no backend:)

```sh
SOLANA_PROBE_ROOT=<workspace> SOLANA_PROBE_DOC=<design doc> \
SOLANA_PROBE_SRC=<program>/src/lib.rs SOLANA_PROBE_MAIN=<program identifier> \
env -u CERTORA .venv/bin/python -m pytest tests/test_solana_component_grouping.py -m expensive -q -s
```

### The grouping (gate: passed)

**12 components, every instruction assigned, zero overlap, validator clean.** The split fell out
along the lines an auditor would draw — reserve/market administration, price-and-interest refresh,
position lifecycle and health, liquidity provision, position management (the largest, at 14
instructions), liquidation, flash loans, conditional orders, ownership transfer, a queued-withdrawal
subsystem, referrals and fees, and an external-staking integration.

The requirements attached to each were grounded in the code rather than restating the design doc:
several named a specific asymmetry between two code paths, of the kind that is worth an auditor's
attention. The point for this note is only that the grouping is *substantive* — the model had read
the program, not skimmed the document.

### Why it matters: what the whole-program unit was missing

The same program had two property sets from earlier whole-program runs: **35 and 37 properties for
~60 instructions**. Both are strong on the headline surface (reserve accounting, exchange rate,
collateral health, liquidation, flash loans). Neither covers the tail, and — the actual finding —
**they miss *different* tails**:

| Capability (now its own component) | instructions | run A | run B |
|---|---|---|---|
| External-staking integration | 3 | 0 | 0 |
| Referrals & fees | 7 | 0 | 1 |
| Queued withdrawals | 4 | 0 | 2 |
| Ownership transfer | 4 | 2 | 0 |
| Administration (global config part) | 3 | 3 | 1 |

(Counting instructions *named* in a property's text — a proxy that understates real coverage, so
treat the absolute numbers as soft. The staking integration is not mentioned in either run at all.)

If the program genuinely had ~36 properties, two runs would find roughly the same 36. Instead they
agree at the top and diverge randomly below it — the signature of one agent with a fixed budget
sampling a surface too large for one pass. That is §11's "hard cap on depth", no longer
hypothetical. The five capabilities above are **21 instructions** that two whole-program runs
between them barely touched, and each is now a component that gets its own extraction agent.

### The cost surprise: K = 12, not 3–5

§11 guessed K = 3–5 and estimated +10–12 min for Crucible. This program yields **K = 12**, and at
the measured ≈170 s marginal per unit that is **≈ +31 min** — which lands C1 (§8.3) back at the
1:33 the per-invariant collapse was meant to escape. Two consequences:

- **K scales with program size, so Crucible's cost scales with program size twice** (a bigger
  program means both a longer campaign and more of them). The §11 table is right for a small
  program and wrong for a large one; read it as a floor.
- **C2/C3 are no longer hypothetical levers.** C3 (re-collapse: per-component extraction and report
  rows, one build + campaign) preserves everything this section shows is valuable — the
  property-quality win is entirely in *extraction*, which is free and parallel — while giving up
  only failure isolation. At this scale that trade looks clearly right, which suggests C1/C3 should
  be a **per-program decision, not a global one**.

None of this touches the Prover/CVLR side: 12 concurrent cloud jobs instead of 1 monolithic one is
a straight improvement, and it is the case §5.1 predicted.

### Caveat on the measurement

Analysis took **31.5 min**. There is no before-baseline for the same program *without* the
component section, so that number is not attributable to this change — it is mostly the cost of
reading ~27k lines. Worth capturing a baseline if analysis wall-clock becomes a concern.

## 16. Verified: the Crucible e2e gate

`tests/test_crucible_e2e_gate.py` on `test_scenarios/solana_vault` — the whole vertical with real
models, through the generic host exactly as `console-crucible` does. **Passed in 44:39.**

```
Crucible E2E: 2 invariant(s), 19 properties
  == Vault Initialization == delivered=True    8/8  GOOD
  == Lamport Custody ==      delivered=True   11/11 GOOD
```

What each stage's claim looks like when it is actually run:

| Stage | Observed |
|---|---|
| 4 — per-component units | K = 2 on a 3-instruction program: *Vault Initialization*, *Lamport Custody* |
| 3 — `begin` hook | **one** shared fixture with 11 `action_*` spanning *both* capabilities (init + custody) — a fixture authored from one component's properties would have covered one and starved the other |
| 5 — per-component fns | two fns + two features in one delivered crate; fixture present once; no leftover `c_probe` (the gates' check leaked into the deliverable as a *component* feature — it now ships deliberately, as `preflight`, outside the `c_` namespace) |
| 5 — per-feature build | `cargo build --features=<fn>` passed for each |
| attribution | 19 per-property report rows, each pinned to its component's target |

Two things this caught that unit tests had not:

- **The harness-fn naming bug** (§8.1) — the run shipped `fn c_Vault_Initialization`, which compiles
  (only a `non_snake_case` warning) and so slipped past a green gate. Fixed via `ident_of`; the
  hyphen case it also uncovered would have been a hard syntax error.
- **A stale `finalize` test** in `tests/test_crucible_harness.py`, which asserted the old single
  `c_invariants` feature and sent no `targets`. It had been *erroring at import* the whole time
  because the `crucible_app` wheel was not built in the venv, so it never ran — installing the wheel
  is what surfaced it. Worth knowing: a green `pytest tests/` proves less than it appears to if the
  wheel is missing.

Environment prerequisites the gate needs, none of them obvious from the failure messages: the
`crucible_app` wheel (`maturin develop`), the `run-confined` launcher (`cargo build -p run-confined
--release`), and a `cargo-build-sbf` whose platform-tools version is **already cached** — the build
runs confined and offline, so a missing version cannot be downloaded, and the error that surfaces
names the failed cleanup rather than the cause. See crucible-demo.md §1e–1f.

## 17. Section isolation: why the delivered crate is gated per feature

Measured on klend (14 components, 283 properties, 2026-08-03): the run finished green — 281/283
held, every gate passed — and the delivered crate **compiled for no feature at all**. Two
components had each emitted an ungated `impl Fixture { fn read_token_balance }`, and rustc's
`E0592` fires on a duplicate inherent method *regardless of the module it is written in*
(verified). Building any single feature failed with that plus two `E0034` ambiguities, at call
sites belonging to components unrelated to the selected feature.

Nothing was wrong with any gate. Three facts compose into the gap:

1. **A gated build is single-component by construction.** `compile`/`validate` write a *fresh*
   crate — `main.rs` = fixture + that one section, `Cargo.toml` declaring exactly one feature — and
   `Workspace::run` materializes it with a plain overwrite. Fourteen sequential single-component
   crates, each honestly compiled and fuzzed. Rebuilding the two culprit sections in isolation
   confirms both: exit 0 each, 3 errors together.
2. **The union is assembled only at the end,** in `finalize` — you cannot know the component set
   before then. klend's `Cargo.toml` is stamped at the same second the last component finished, and
   declares 14 features; a gate can only ever write one.
3. **Nothing revalidated the union**, so any cross-section conflict shipped silently.

`finalize` already anticipated this — its dedupe comment names the mechanism exactly ("only this
render folds the whole outcome set into one crate") — but the guard compares **whole section
texts**, so two components emitting the same *item* inside two different sections are two distinct
texts and both pass through. It catches a duplicated `fn c_x`, not a duplicated helper.

**The fix removes the failure class rather than detecting it.** Each section is emitted as its own
module gated on its Cargo feature, with a generated `#[invariant_test]` entry delegating into it
(`Section`, `templates/section_entry.j2` + `section_file.j2`). Three properties make this work:

- The **`#[cfg]`, not the module, is what isolates.** A module gives inherent impls no namespace;
  it is simply the single item the gate can hang on, which is what avoids gating authored source
  item-by-item (that would mean parsing item boundaries out of LLM output).
- **Exactly one feature is ever enabled**, because each `#[invariant_test]` expands its own
  `fn main()` + `#[global_allocator]` — enabling two is a duplicate-`main` error by construction.
  So gated sections can never coexist in a build, and same-named helpers are free.
- **The entry must be generated by us.** The macro expands `main()` as a *sibling* of the fn it
  annotates, so it cannot come from inside the module. It **inlines the annotated body into its
  per-action loop** (verified in `crucible-invariant-macro/src/lib.rs`), which is precisely what
  makes a delegating wrapper right: the authored fn is then called once after every fuzzed action.
  We do not reimplement `main` — that is ~100 lines of libafl wiring we would then own across
  Crucible bumps.

  > An earlier revision of this section claimed the macro "reads only the signature, never the
  > body". That was wrong. The conclusion is unchanged, but for the opposite reason.

Verified by regenerating klend's real 14 sections through the wrapping: **14/14 features build,
against 0/14 as delivered.**

#### The crate root is now written once, up front

Section gating removed the collision; what remained was that the crate was still *assembled* N+1
times — once per gated build from the one section it held, and once more at the end. Each section
now lives in its own file (`<harness>/src/c_<slug>.rs`) and the crate root is rendered **once
per run** by the `Backend::crate_root` hook, which the host calls in `StagedFormalizer.begin`: the
first and only point where both the shared fixture and the whole unit set are known. `compile` and
`validate` write nothing but their own section file.

The **setup gate** reaches that point too: it is sent the unit set on `Authored::Setup`, so with the
fixture it has just authored it holds both halves and writes the real `src/main.rs` itself. The
`crate_root` hook then renders the same files from the same two inputs, byte-identically — not a
second assembly, but the same one repeated for the run whose setup spec came from cache and so never
reached that gate. Declaring every component's `mod` before a single section file exists costs the
setup build nothing: a `#[cfg]`-disabled `mod` is stripped before rustc resolves its file.

Only **preflight** runs earlier than that, before analysis, when there is no fixture and no unit set.
It builds a crate of exactly one file, `src/preflight.rs`, declared under the **same `[[bin]]` name**
as the deliverable — `crucible run` only ever executes `target/<profile>/invariant_test`
(`find_fuzz_binary` in the CLI), so the name is fixed while the path is ours. Writing `src/main.rs`
that early would leave a half-crate at the deliverable's path; from the setup gate on the manifest
points there instead, and `src/preflight.rs` stays behind as the record of what the preflight proved.

The check those gates build is a **target like any component's** (`preflight`), not a one-off. So it
survives into the delivered crate: `crucible run <program> preflight --dry-run` re-runs the setup
gate — fixture compiles, program loads, nothing asserted — through exactly the mechanism that runs a
suite. Its body is inline in the crate root rather than in a section file of its own, so there is
nothing that can drift from what the gates ran.

The scaffolding's names sit **outside** the `c_` prefix that every component target carries, since
`feature_of` is the only thing that names one. A component called "preflight" therefore slugs to
`c_preflight` and is simply a different target — where a shared namespace would have had it declare
the preflight's Cargo feature twice and land its section file on the preflight root, which is the §17
failure wearing a new hat. `the_scaffolding_and_component_namespaces_cannot_collide` pins it.

So **a gated build is the delivered crate with one feature selected** — not a differently-shaped
artifact that resembles it, and not two renders kept in step by argument. Two consequences:

- The authored fn has a constant name (`invariants`), because the varying `c_<slug>` now exists only
  in the generated entry. The prompt no longer carries a per-component fn name for the cheat sheet
  and the instruction to disagree about — the failure mode that produced `c_invariants` in a run
  that had asked for `c_withdraw_queue`.
- Every unit gets a feature at `crate_root` time, before any outcome exists. A component that later
  gives up therefore has a declared target with nothing behind it, so `finalize` puts a
  `compile_error!` there naming the gap. Deliberately **not** an always-failing test: `validate`
  reads a fuzz finding as a refuted property, so a failing test would be indistinguishable from a
  real counterexample against the user's program.

### Future direction: a per-feature build gate on every run (deliberately not built)

The obvious complement is to move the e2e's per-feature build loop (§13 stage 5,
`tests/test_crucible_e2e_gate.py`) out of the test and into the run — after `finalize` writes the
crate, ask the wheel to `cargo build --features=<f>` for each declared feature. Sketch, if it is
ever wanted: a new optional `Backend::check_deliverable(workdir, sandbox)` hook (defaulting to
`Ok`, so an app with no cross-unit assembly is unaffected) called from `RustBackend.finalize` right
after the write loop, returning the failing features with their diagnostics rather than a flag. Cost
is one incremental build per feature — **~6 s warm**, so ~84 s on klend — but only if it reuses the
run's `CARGO_HOME`/`RUSTUP_HOME` and `target/`; miss that and it is a cold ~4-minute build each.

**Decision: not now.** Section gating already removes the only failure this has ever caught, so its
remaining value is hypothetical (wrapper normalizations misfiring, future drift between what
`files()` and `finalize` assemble). More decisively, it cannot affect the run's pass/fail
determination: the verdicts are all obtained from real builds of real sections, so a broken
*package* would have to be reported-and-continued, which means new report schema
(`unbuildable_targets` or similar) and a new phase for a warning. That is real complexity for a
speculative catch. Revisit if a cross-section conflict recurs despite gating — that recurrence is
the evidence which would justify it.

Worth recording why this went unnoticed for two rounds of the same bug (an earlier `E0428
c_invariants defined multiple times`, then this `E0592`): the per-feature assertion exists, but it
lives in an `expensive`-marked test on the 2-component `solana_vault` scenario, which has too few
components to collide. The check was written where it cannot fire. klend at K = 14 is the first
target large enough to hit it.
