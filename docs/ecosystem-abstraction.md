# The Ecosystem Abstraction

AutoProver's shared pipeline is parametric over an **ecosystem** — the blockchain/source
domain being analyzed. The ecosystem supplies the domain-specific *front half* of a run: the
system-model type the analysis phase produces, the analysis and property-extraction prompts,
the source-reading conventions, connectivity validation, how the target's "main" is located,
and how it is split into units. Everything downstream of properties (how a property becomes a
verified artifact) belongs to the **backend**, a separate axis.

Today three ecosystems are implemented: `EVM` (Solidity, wired to the CVL/prover and Foundry
backends), `SOLANA` (Rust front half, gated by a null backend), and `SOROBAN` (Rust front half,
with no backend yet).

---

## 1. Two orthogonal axes

The pipeline has a front half and a back half, joined by *properties*:

```
     ┌─────────── ECOSYSTEM owns ───────────┐   ┌──────── BACKEND owns ────────┐
source ─analyze─▶ SystemModel ─extract─▶ properties ─formalize─▶ artifact ─verdicts─▶
     (how we MODEL and REASON about          (how a property becomes a
      the domain: contracts vs programs,      checkable, verified artifact:
      storage vs accounts, reentrancy         CVL + prover / foundry test / …)
      vs missing-signer)
                                        └──────── SHARED: report ────────┘
```

- **Ecosystem** = the *front half*: the system-model type, the analysis + property-extraction
  prompts, source conventions, connectivity validation, main-unit location, and unit split.
- **Backend** = the *back half*: `prepare_system` → `Formalizer` (`formalize` / `fetch_verdicts`).
- **Report** is shared and domain-neutral.

The axes meet at the analyzed model: the backend's `prepare_system(analyzed: App)` consumes the
ecosystem's `App` type, so a **backend is written against an ecosystem's model** — the CVL prover
backend needs `SourceApplication`; a Solana backend needs `SolanaApplication`. `run_pipeline`
ties a `PipelineBackend[..., U, Main]` to an `Ecosystem[App, Main, U]`, so the analyzed model,
main-unit, and per-unit values flow through without casts.

---

## 2. The seam

The seam lives in [composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py) as two
frozen dataclasses. An ecosystem factors into a **language** facet (conventions for reading the
analyzed program's *source* — shared by every chain written in that language) and the **chain**
itself (the platform model + prompts). The language here is that of the *code under analysis*,
not the language a backend is implemented in.

```python
LanguageTag = Literal["solidity", "rust"]
ChainTag    = Literal["evm", "solana", "soroban"]

@dataclass(frozen=True)
class Language:
    name: LanguageTag
    default_forbidden_read: str          # fs-exclusion regex (Cargo layout vs Foundry layout)
    vulnerability_patterns_fragment: str | None = None   # j2 fragment of language-level vulnerability patterns

@dataclass(frozen=True)
class Ecosystem[App: BaseApplication, Main, Unit: FeatureUnit]:
    name: ChainTag
    language: Language
    system_model: type[App]                          # the pydantic type analysis produces
    analysis_prompts: PromptPair                      # (system, initial) template names
    property_prompts: PromptPair
    validate_analysis: Callable[[BaseApplication, SourceIdentifier | None], str | None]
    locate_main: Callable[[App, SourceCode], Main]    # find the "main" contract/program
    units: Callable[[Main], list[Unit]]               # split into per-unit extraction items
    analysis_extra_input: Callable[[SourceCode], list[str | dict]]
    code_explorer_prompt: TypedTemplate              # shared protocol + this chain's look-fors
```

`Main` and `Unit` generalize what were EVM's `ContractInstance` / `ContractComponentInstance` —
thin index wrappers over `App` that the driver hands to the backend and to property inference.
`Unit` is any [`FeatureUnit`](../composer/spec/system_model.py) — the ecosystem-agnostic
interface (`display_name` / `slug` / `unit_index` / `cache_material` / `context_tag` /
`feature_json`) the driver uses for per-unit cache keys, task ids, and labels.

A registry exposes the ecosystems by chain tag. Each chain has its own `App`/`Main`/`Unit` types,
so the registry is a `TypedDict` rather than a plain `dict`.

```python
class Ecosystems(TypedDict):
    evm: EvmEcosystem
    solana: SolanaEcosystem
    soroban: SorobanEcosystem

ECOSYSTEMS: Ecosystems = {"evm": EVM, "solana": SOLANA, "soroban": SOROBAN}
```

---

## 3. The EVM ecosystem (`SOLIDITY ⊕ evm`)

`EVM` is the `SOLIDITY` language facet composed with the `evm` chain. Its members are the
pre-existing EVM types, prompts, and functions bound into the seam — the CVL prover and Foundry
backends run against it unchanged.

```python
SOLIDITY = Language(
    name="solidity",
    default_forbidden_read=fs_forbidden_read,          # Foundry layout: lib/, test/, .sol carve-out
)

EVM: Ecosystem[SourceApplication, ContractInstance, ContractComponentInstance] = Ecosystem(
    name="evm",
    language=SOLIDITY,
    system_model=SourceApplication,
    analysis_prompts=PromptPair("application_analysis_system.j2", "application_analysis_prompt.j2"),
    property_prompts=PromptPair("property_analysis_system_prompt.j2", "property_analysis_prompt.j2"),
    validate_analysis=validate_solidity_connectivity,
    locate_main=main_instance,                          # match by solidity_identifier
    units=_evm_units,                                   # one unit per contract component
    analysis_extra_input=_evm_analysis_extra_input,
    code_explorer_prompt=EVM_CODE_EXPLORER_TEMPLATE,    # code_explorer/solidity.j2
)
```

EVM's unit split is one `ContractComponentInstance` per component of the located contract, so
property extraction fans out one agent per component — the historical per-component behavior.

---

## 4. The Solana ecosystem (`RUST ⊕ solana`)

`SOLANA` is the `RUST` language facet composed with the `solana` chain. The front half is
implemented and exercised by a null (report-only) backend; the verification backend is a
separate effort and not part of this seam.

```python
RUST = Language(
    name="rust",
    # Cargo/Anchor layout: hide build output, VCS, lockfiles, and the JS side; keep crate sources + tests/.
    default_forbidden_read=r"(^target/.*)|(^\.git.*)|(^node_modules/.*)|(.*\.lock$)",
    vulnerability_patterns_fragment="rust/vulnerability_patterns_fragment.j2",     # overflow/underflow, panic!/unwrap/expect, ownership
)

SOLANA: Ecosystem[SolanaApplication, SolanaProgramInstance, SolanaComponentInstance] = Ecosystem(
    name="solana",
    language=RUST,
    system_model=SolanaApplication,
    analysis_prompts=PromptPair("solana/analysis_system.j2", "solana/analysis_prompt.j2"),
    property_prompts=PromptPair("solana/property_system.j2", "solana/property_prompt.j2"),
    validate_analysis=_solana_validate,
    locate_main=_solana_locate_main,                    # match by program_identifier
    units=_solana_units,                                # one per ProgramComponent of the main program
    analysis_extra_input=_solana_analysis_extra_input,
    code_explorer_prompt=SOLANA_CODE_EXPLORER_TEMPLATE, # rust/_common + PDAs / signers / CPI identity
)
```

- **System model** ([composer/spec/solana/model.py](../composer/spec/solana/model.py)) —
  `SolanaApplication` is the standalone analog of `SourceApplication`: `SolanaProgram`s with
  their instructions and account constraints (Solana accounts are **passed in**, not owned
  storage), CPI targets, and signers in place of EOA actors. `SolanaProgramInstance` /
  `SolanaComponentInstance` are the index-wrapper instances (the latter satisfies
  `FeatureUnit`), mirroring EVM's `ContractInstance` / `ContractComponentInstance`.
- **Per-component units.** `units` returns one `SolanaComponentInstance` per `ProgramComponent`
  of the main program — the same shape as `_evm_units`. A component is a *capability* (a named
  cluster of instructions plus the account state they maintain), produced by system analysis. It
  is an authoring and attribution scope, not an execution scope: Crucible still fuzzes the whole
  program in one action sequence, and a symbolic backend would not execute sequences at all.
  Note `units` is on the **ecosystem** axis, so *every* Solana backend inherits this split —
  Crucible today, a Certora Solana Prover (CVLR) backend later — which is why it is chosen on
  backend-neutral grounds rather than to suit a fuzzer's cost model. How Crucible uses the
  split is [crucible.md](./crucible.md) §2.
- **Validation** — `_solana_validate` mirrors `validate_solidity_connectivity`'s structure over
  `SolanaApplication`: unique program identifiers and names, unique instruction slugs within a
  program, unique component names/slugs, component interactions resolving to a declared
  program+component or authority, and the expected main program present. It adds one rule EVM
  has no peer for: the component→instruction mapping must be **valid and total**. EVM's
  `external_entry_points` are prose the prompt renders, but a `ProgramComponent`'s `instructions`
  are *references* the unit wrapper resolves, so a name that doesn't resolve silently drops an
  instruction's account detail from the extraction prompt — and an instruction no component
  claims is an entry point no property will ever cover.

### Prompt composition — the shared Rust fragment

The `RUST` language facet is chain-independent, so its source conventions and vulnerability-pattern
fragment are authored once and pulled into the chain's prompts by Jinja `{% include %}`. The
code-explorer system prompt is the same split, but on the *ecosystem*: a shared protocol
(`code_explorer/common_fragment.j2`) plus a Rust crate-navigation fragment
(`code_explorer/rust/common_fragment.j2`) plus the chain's look-fors (`code_explorer/solana.j2` /
`code_explorer/soroban.j2`). A single Rust explorer prompt cannot name PDAs without lying to
Soroban, or `require_auth` without lying to Solana.

The Solana property template composes the shared Rust fragment with its own platform fragment:

```jinja
{# composer/templates/solana/property_prompt.j2 #}
{% include "rust/vulnerability_patterns_fragment.j2"   %}   {# shared: overflow, panics, unwrap, lossy casts #}
{% include "solana/vulnerability_patterns_fragment.j2" %}   {# chain-specific: signer/owner/PDA/CPI checks #}
```

`rust/vulnerability_patterns_fragment.j2` (the language facet) states language-level vulnerability
patterns — integer overflow/underflow, `panic!`/`unwrap`/`expect` aborts, lossy conversions,
unchecked results — independent of any chain; `solana/vulnerability_patterns_fragment.j2` adds the
Solana-native ones. Because the Rust facet is factored out this way, it is reusable by any future
Rust chain without copying.

---

## 5. The Soroban ecosystem (`RUST ⊕ soroban`)

`SOROBAN` is the `RUST` language facet plus the Soroban/Stellar model and prompts. It is front half
only: there is no Soroban backend yet. Registering it still matters because the prompt templates
are typed, listed in `template_manifest.json`, and rendered by
[tests/test_fuzzed_templates.py](../tests/test_fuzzed_templates.py).

```python
SOROBAN: SorobanEcosystem = Ecosystem(
    name="soroban",
    language=RUST,
    system_model=SorobanApplication,
    analysis_prompts=PromptPair(SOROBAN_ANALYSIS_SYSTEM_TEMPLATE, SOROBAN_ANALYSIS_INITIAL_TEMPLATE),
    property_prompts=PropertyPrompts(SOROBAN_PROPERTY_SYSTEM_TEMPLATE, _render_soroban_property_prompt),
    validate_analysis=_soroban_validate,
    locate_main=_soroban_locate_main,
    supports_greenfield=False,
    units=_soroban_units,
    unit_type=SorobanComponentInstance,
    analysis_extra_input=_soroban_analysis_extra_input,
    code_explorer_prompt=SOROBAN_CODE_EXPLORER_TEMPLATE,  # rust/_common + require_auth / storage kind
)
```

- **System model** ([composer/spec/soroban/model.py](../composer/spec/soroban/model.py)) models
  contracts, entry-point functions, `Address` auth checks, storage entries, calls, components, and
  external actors.
- **Storage** carries its kind everywhere: `instance`, `persistent`, or `temporary`. A component's
  `storage_keys` resolve to full `StorageEntry` objects so the templates can show the storage kind.
- **Auth** is explicit. A function with no `require_auth` is represented as `auth == []`; the
  templates render that fact instead of hiding it.
- **Units** are one `SorobanComponentInstance` per component of the main contract.
- **Validation** checks duplicate contract identifiers/names, duplicate function slugs, duplicate
  component names/slugs, unknown component links, unknown storage keys, duplicate storage keys, and
  functions that belong to no component.
- **Templates** include `soroban/platform_model_fragment.j2` in both the analysis and property prompts, so
  Soroban execution facts live in one shared place. See
  [composer/templates/soroban/README.md](../composer/templates/soroban/README.md).
- **Backend guidance** still belongs to the backend. A future Certora Sunbeam backend should provide
  guidance for CVLR `#[rule]` specs and `certoraSorobanProver`; the Soroban ecosystem should only
  describe the app and propose properties.

---

## 6. Driver integration

`run_pipeline` ([composer/pipeline/core.py](../composer/pipeline/core.py)) takes an
`ecosystem` and never hardcodes a domain. It is a required keyword argument — every caller names
its ecosystem (`ecosystem=EVM`, `ecosystem=SOLANA`) rather than inheriting one by omission.

```python
async def run_pipeline[..., U, Main, App](
    backend: PipelineBackend[P, FormT, H, A, U, Main, App],
    run: PipelineRun[P, H],
    *, ...,
    ecosystem: Ecosystem[App, Main, U],
):
    analyzed = await run_component_analysis(
        ty=ecosystem.system_model,
        system_template=ecosystem.analysis_prompts.system,
        initial_template=ecosystem.analysis_prompts.initial,
        validate=ecosystem.validate_analysis,
        extra_input=[*ecosystem.analysis_extra_input(source), *spec.extra_input], ...)
    prepared = await backend.prepare_system(analyzed, run)
    ...
    batches = await _extract_all(..., ecosystem=ecosystem)   # iterates ecosystem.units(prepared.main)
```

- **`run_component_analysis`** ([system_analysis.py](../composer/spec/system_analysis.py)) is
  generic over the analyzed type and takes the prompt pair + validation function, all three
  required. Each is domain-specific, so none carries a default.
- **`run_property_inference`** ([prop_inference.py](../composer/spec/prop_inference.py)) takes the
  ecosystem's property prompt pair and a generic `FeatureUnit`. The "expressible downstream" axis
  stays backend-owned (`backend_guidance`, from `PipelineBackend`); the "failure modes in this
  domain" axis is the ecosystem's prompt. Both axes are required keyword arguments.
- **The natspec pipeline** ([natspec/pipeline.py](../composer/spec/natspec/pipeline.py)) is the
  other consumer of both primitives. It takes an `EvmEcosystem` for the prompt pairs and is
  Solidity-only otherwise (solc, CVL, interface/stub generation). Its analyzed model comes from
  its `MentalModel` — `Application` / `FromSourceApplication`, siblings of `EVM.system_model`
  under `BaseApplication` rather than subtypes — so `ecosystem.validate_analysis` does not
  typecheck there and it names `validate_solidity_connectivity` directly.
- **`_extract_all`** iterates `ecosystem.units(main)`, running one property-inference agent per
  unit — one per component for EVM, Solana, and Soroban.

---

## 7. What is shared and domain-neutral

- Source tools (`fs_tools`, `code_explorer`, `code_document_ref`) — language-neutral; they read
  Rust as well as Solidity. The language input is the `forbidden_read` default; the explorer
  system prompt is an ecosystem template (shared protocol + chain look-fors).
- The report (`collect` / `Verdict` / schema) and `ReportBackend`.
- Caching, the multi-round property loop, interactive refinement, and the agent plumbing.
- The backend seam itself — a verification backend is "just another backend," paired to an
  ecosystem by its `App` model.

---

## 8. Key files

| Concern | File |
|---|---|
| The ecosystem seam | [composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py) |
| Driver integration | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| System analysis (ecosystem-driven) | [composer/spec/system_analysis.py](../composer/spec/system_analysis.py) |
| Property inference (ecosystem-driven) | [composer/spec/prop_inference.py](../composer/spec/prop_inference.py) |
| `FeatureUnit` protocol | [composer/spec/system_model.py](../composer/spec/system_model.py) |
| EVM system model + prompts | [composer/spec/system_model.py](../composer/spec/system_model.py) · `composer/templates/application_analysis_*.j2` · `property_analysis_*.j2` |
| Solana system model | [composer/spec/solana/model.py](../composer/spec/solana/model.py) |
| Solana prompts + shared Rust fragment | `composer/templates/solana/*.j2` · `composer/templates/rust/vulnerability_patterns_fragment.j2` |
| Code-explorer prompts (per ecosystem) | `composer/templates/code_explorer/` · `Ecosystem.code_explorer_prompt` |
| Soroban system model | [composer/spec/soroban/model.py](../composer/spec/soroban/model.py) |
| Soroban prompts (+ platform primer) | `composer/templates/soroban/*.j2` · [its README](../composer/templates/soroban/README.md) |
| Template manifest (what the fuzzer renders) | [template_manifest.json](../template_manifest.json) · [composer/scripts/template_manifest.py](../composer/scripts/template_manifest.py) |
| fs-exclusion default (EVM) | [composer/spec/util.py](../composer/spec/util.py) |
