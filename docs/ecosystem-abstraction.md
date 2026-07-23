# The Ecosystem Abstraction

AutoProver's shared pipeline is parametric over an **ecosystem** — the blockchain/source
domain being analyzed. The ecosystem supplies the domain-specific *front half* of a run: the
system-model type the analysis phase produces, the analysis and property-extraction prompts,
the source-reading conventions, connectivity validation, how the target's "main" is located,
and how it is split into units. Everything downstream of properties (how a property becomes a
verified artifact) belongs to the **backend**, a separate axis.

Today two ecosystems are implemented: `EVM` (Solidity, fully wired to the CVL/prover and
Foundry backends) and `SOLANA` (Rust, front half only — analysis + property extraction, gated
by a null backend). Companion: [application-abstraction.md](./application-abstraction.md) (the
pieces of an analyzed application).

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
ChainTag    = Literal["evm", "solana", "soroban"]   # "soroban" is reserved; not yet wired

@dataclass(frozen=True)
class Language:
    name: LanguageTag
    default_forbidden_read: str          # fs-exclusion regex (Cargo layout vs Foundry layout)
    code_explorer_prompt: str            # source-navigation framing ("Rust source" vs "Solidity")
    failure_modes_partial: str | None = None   # j2 partial of language-level failure modes

@dataclass(frozen=True)
class Ecosystem[App: BaseApplication, Main, Unit: FeatureUnit]:
    name: ChainTag
    language: Language
    system_model: type[App]                          # the pydantic type analysis produces
    analysis_prompts: PromptPair                      # (system, initial) template names
    property_prompts: PromptPair
    validate_analysis: Callable[[BaseApplication, SolidityIdentifier | None], str | None]
    locate_main: Callable[[App, SourceCode], Main]    # find the "main" contract/program
    units: Callable[[Main], list[Unit]]               # split into per-unit extraction items
    analysis_extra_input: Callable[[SourceCode], list[str | dict]]
```

`Main` and `Unit` generalize what were EVM's `ContractInstance` / `ContractComponentInstance` —
thin index wrappers over `App` that the driver hands to the backend and to property inference.
`Unit` is any [`FeatureUnit`](../composer/spec/system_model.py) — the ecosystem-agnostic
interface (`display_name` / `slug` / `unit_index` / `cache_material` / `context_tag` /
`feature_json`) the driver uses for per-unit cache keys, task ids, and labels.

A registry exposes the ecosystems by chain tag; it is heterogeneous in `App`/`Main`/`Unit`
(each chain has its own model), hence `Ecosystem[Any, Any, Any]`:

```python
ECOSYSTEMS: dict[ChainTag, Ecosystem[Any, Any, Any]] = {"evm": EVM, "solana": SOLANA}
```

---

## 3. The EVM ecosystem (`SOLIDITY ⊕ evm`)

`EVM` is the `SOLIDITY` language facet composed with the `evm` chain. Its members are the
pre-existing EVM types, prompts, and functions bound into the seam — the CVL prover and Foundry
backends run against it unchanged.

```python
SOLIDITY = Language(
    name="solidity",
    default_forbidden_read=FS_FORBIDDEN_READ,          # Foundry layout: lib/, test/, .sol carve-out
    code_explorer_prompt=CODE_EXPLORER_SYS_PROMPT,
)

EVM: Ecosystem[SourceApplication, ContractInstance, ContractComponentInstance] = Ecosystem(
    name="evm",
    language=SOLIDITY,
    system_model=SourceApplication,
    analysis_prompts=PromptPair("application_analysis_system.j2", "application_analysis_prompt.j2"),
    property_prompts=PromptPair("property_analysis_system_prompt.j2", "property_analysis_prompt.j2"),
    validate_analysis=_validate_connectivity,
    locate_main=main_instance,                          # match by solidity_identifier
    units=_evm_units,                                   # one unit per contract component
    analysis_extra_input=_evm_analysis_extra_input,
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
    code_explorer_prompt=RUST_CODE_EXPLORER_PROMPT,     # "Rust source … instruction handlers, Accounts, PDAs"
    failure_modes_partial="rust/_failure_modes.j2",     # overflow/underflow, panic!/unwrap/expect, ownership
)

SOLANA: Ecosystem[SolanaApplication, SolanaProgramInstance, SolanaProgramInstance] = Ecosystem(
    name="solana",
    language=RUST,
    system_model=SolanaApplication,
    analysis_prompts=PromptPair("solana/analysis_system.j2", "solana/analysis_prompt.j2"),
    property_prompts=PromptPair("solana/property_system.j2", "solana/property_prompt.j2"),
    validate_analysis=_solana_validate,
    locate_main=_solana_locate_main,                    # match by program_identifier
    units=_solana_units,                                # whole-program: a singleton [main]
    analysis_extra_input=_solana_analysis_extra_input,
)
```

- **System model** ([composer/spec/solana/model.py](../composer/spec/solana/model.py)) —
  `SolanaApplication` is the standalone analog of `SourceApplication`: `SolanaProgram`s with
  their instructions and account constraints (Solana accounts are **passed in**, not owned
  storage), CPI targets, and signers in place of EOA actors. `SolanaProgramInstance` /
  `SolanaInstructionInstance` are the index-wrapper instances (the latter satisfies
  `FeatureUnit`).
- **Whole-program units.** `units` returns a singleton `[main]` — the `Unit` type is
  `SolanaProgramInstance`, the same as `Main`. All of a program's invariants are inferred over
  the whole program in a single extraction rather than fanned out per instruction.
- **Validation** — `_solana_validate` mirrors `_validate_connectivity`'s structure over
  `SolanaApplication`: unique program identifiers, unique instruction slugs within a program,
  the expected main program present.

### Prompt composition — the shared Rust fragment

The `RUST` language facet is chain-independent, so its source conventions and failure-mode
fragment are authored once and pulled into the chain's prompts by Jinja `{% include %}`. The
Solana property template composes the shared Rust fragment with its own platform fragment:

```jinja
{# composer/templates/solana/property_prompt.j2 #}
{% include "rust/_failure_modes.j2"   %}   {# shared: overflow, panics, unwrap, lossy casts #}
{% include "solana/_failure_modes.j2" %}   {# chain-specific: signer/owner/PDA/CPI checks #}
```

`rust/_failure_modes.j2` (the language facet) states language-level failure modes — integer
overflow/underflow, `panic!`/`unwrap`/`expect` aborts, lossy conversions, unchecked results —
independent of any chain; `solana/_failure_modes.j2` adds the Solana-native ones. Because the
Rust facet is factored out this way, it is reusable by any future Rust chain without copying.

---

## 5. Driver integration

`run_pipeline` ([composer/pipeline/core.py](../composer/pipeline/core.py)) takes an
`ecosystem` and never hardcodes a domain. It defaults to `EVM`, so Solidity backends pass
nothing; a non-EVM backend passes its ecosystem (e.g. `ecosystem=SOLANA`).

```python
async def run_pipeline[..., U, Main](
    backend: PipelineBackend[P, FormT, H, A, U, Main],
    run: PipelineRun[P, H],
    *, ...,
    ecosystem: Ecosystem[Any, Any, Any] = EVM,
):
    analyzed = await run_component_analysis(
        ty=ecosystem.system_model,
        prompts=ecosystem.analysis_prompts,
        validate=ecosystem.validate_analysis,
        extra_input=[*ecosystem.analysis_extra_input(source), *spec.extra_input], ...)
    prepared = await backend.prepare_system(analyzed, run)
    ...
    batches = await _extract_all(..., ecosystem=ecosystem)   # iterates ecosystem.units(prepared.main)
```

- **`run_component_analysis`** ([system_analysis.py](../composer/spec/system_analysis.py)) is
  generic over the analyzed type and accepts the prompt pair + validation function; the ecosystem
  supplies all three.
- **`run_property_inference`** ([prop_inference.py](../composer/spec/prop_inference.py)) takes the
  ecosystem's property prompt pair and a generic `FeatureUnit`. The "expressible downstream" axis
  stays backend-owned (`backend_guidance`); the "failure modes in this domain" axis is the
  ecosystem's prompt.
- **`_extract_all`** iterates `ecosystem.units(main)`, running one property-inference agent per
  unit — one per component for EVM, one for the whole program for Solana.

---

## 6. What is shared and domain-neutral

- Source tools (`fs_tools`, `code_explorer`, `code_document_ref`) — language-neutral; they read
  Rust as well as Solidity. The only ecosystem inputs are the `forbidden_read` default and the
  explorer prompt string.
- The report (`collect` / `Verdict` / schema) and `ReportBackend`.
- Caching, the multi-round property loop, interactive refinement, and the agent plumbing.
- The backend seam itself — a verification backend is "just another backend," paired to an
  ecosystem by its `App` model.

---

## 7. Key files

| Concern | File |
|---|---|
| The ecosystem seam | [composer/pipeline/ecosystem.py](../composer/pipeline/ecosystem.py) |
| Driver integration | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| System analysis (ecosystem-driven) | [composer/spec/system_analysis.py](../composer/spec/system_analysis.py) |
| Property inference (ecosystem-driven) | [composer/spec/prop_inference.py](../composer/spec/prop_inference.py) |
| `FeatureUnit` protocol | [composer/spec/system_model.py](../composer/spec/system_model.py) |
| EVM system model + prompts | [composer/spec/system_model.py](../composer/spec/system_model.py) · `composer/templates/application_analysis_*.j2` · `property_analysis_*.j2` |
| Solana system model | [composer/spec/solana/model.py](../composer/spec/solana/model.py) |
| Solana prompts + shared Rust fragment | `composer/templates/solana/*.j2` · `composer/templates/rust/_failure_modes.j2` |
| fs-exclusion default (EVM) | [composer/spec/util.py](../composer/spec/util.py) |
