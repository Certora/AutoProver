# Proposal — The Ecosystem Abstraction (EVM, Solana, Soroban)

> A proposal to make AutoProver's shared pipeline parametric over an **ecosystem** —
> the blockchain/source domain being analyzed — selected by an application-level parameter
> that picks the right system model, prompts, source conventions, and validation. Today the
> "generic" pipeline is generic over the *backend* (how a property becomes a verified
> artifact) but silently hardwired to Solidity for everything else. This introduces a second,
> orthogonal axis — and factors it further into a **language** facet (Solidity, Rust) and a
> **chain** facet (EVM, Solana, Soroban), so the Rust-specific prompts and source conventions
> are written once and shared between Solana and Soroban while their blockchain-specific
> models and failure modes stay separate.
>
> Companion to [formalization-abstraction.md](./formalization-abstraction.md) (the backend
> seam), [application-abstraction.md](./application-abstraction.md) (the five pieces of an
> application), and [rust-applications.md](./rust-applications.md) (the Rust app framework
> the first Solana/Soroban backends will likely use). Status: **proposal / for review.**

---

## 1. Problem & motivation

We want to author properties for **Solana** programs (Rust/Anchor) and **Soroban** contracts
(Rust/soroban-sdk), not just EVM/Solidity. The pipeline advertises itself as backend-agnostic,
and it is — `run_pipeline` is generic over the result type `FormT`
([formalization-abstraction.md](./formalization-abstraction.md)). But "backend-agnostic" is
not "domain-agnostic." An audit of the shared spine (see §3) shows Solidity/EVM assumptions
baked into three shared places the driver owns:

1. the **system model** — the pydantic types the analysis phase produces;
2. the **prompts** — the analysis and property-extraction templates;
3. a few **source conventions** — the fs-exclusion default, the "main contract" locator.

None of this is tooling (no `solc`/`slither` in the shared steps — it's pure LLM + generic
file reading), so the work is types + prompts + a small driver generalization, not new
analysis engines.

We propose making this a first-class **ecosystem** parameter, so `evm` reproduces today's
behavior exactly and `solana` / `soroban` slot in beside it without forking the pipeline —
and, because two of those three are Rust, factoring the ecosystem so the Rust-specific parts
are shared rather than copied (§2.1).

---

## 2. Two orthogonal axes

The pipeline has a front half and a back half joined by *properties*:

```
     ┌─────────── ECOSYSTEM owns ───────────┐   ┌──────── BACKEND owns ────────┐
source ─analyze─▶ SystemModel ─extract─▶ properties ─formalize─▶ artifact ─verdicts─▶
     (how we MODEL and REASON about          (how a property becomes a
      the domain: contracts vs programs,      checkable, verified artifact:
      storage vs accounts, reentrancy         CVL+prover / foundry / a Rust
      vs missing-signer)                      Solana verifier)
                                        └──────── SHARED: report ────────┘
```

- **Ecosystem** = the *front half*: the system-model types, the analysis + property-extraction
  prompts, source conventions, and connectivity validation.
- **Backend** = the *back half*: `prepare_system` → `Formalizer` (`formalize` / `fetch_verdicts`),
  documented in [formalization-abstraction.md](./formalization-abstraction.md).
- **Report** stays shared and neutral.

The axes are conceptually independent — but they meet at the analyzed model: the backend's
`prepare_system(analyzed: App)` consumes the ecosystem's `App` type. So a **backend is written
against an ecosystem's model** (the CVL prover backend needs `SourceApplication`; a Solana
verifier needs `SolanaApplication`). An application picks a **(ecosystem, backend) pair that
agree on `App`.** That pairing is the one coupling to make explicit (§6).

| | EVM | Solana | Soroban |
| --- | --- | --- | --- |
| Compatible backends | `prover` (CVL), `foundry` | a Rust verifier (via `rustapp`) | a Rust verifier (via `rustapp`) |
| System model | `SourceApplication` (contracts) | `SolanaApplication` (programs) — new | `SorobanApplication` (contracts) — new |
| Unit of extraction | contract component | instruction / account-validation group | contract function |

### 2.1 Ecosystems factor into a *language* facet and a *chain* facet

Solana and Soroban are both **Rust**; EVM is **Solidity**. Much of what the front half needs
is fixed by the *source language* — how to read/navigate it, the project layout to exclude
(Cargo vs Foundry), the language-level failure modes (Rust: integer overflow, `panic!` /
`unwrap` / `expect` aborts, ownership) — and is identical across every chain that uses that
language. The rest is fixed by the *chain/platform*: the system model, the storage and
authorization semantics, the platform failure modes. So an ecosystem is a **composition**:

```text
   ecosystem  =  language facet  ⊕  chain facet
   evm        =  solidity        ⊕  evm
   solana     =  rust            ⊕  solana     ┐ share the SAME rust language facet
   soroban    =  rust            ⊕  soroban    ┘ (prompts, fs conventions, overflow/panic)
```

| Facet | Owns | `solidity` | `rust` (shared by `solana` + `soroban`) |
| --- | --- | --- | --- |
| **Language** | fs-exclusion default, `code_explorer` prompt, source-navigation framing, the language-level failure-mode prompt fragment | assembly / delegatecall, checked-arith caveat | integer overflow/underflow, `panic!`/`unwrap`/`expect` aborts, ownership/borrow |
| **Chain** | system-model type, connectivity validation, main-unit locator + units, the platform failure-mode prompt fragment, SDK conventions | contracts, storage, ERC standards | *differs per chain* — see §8 |

The payoff (the Soroban question directly): **the Rust language facet is authored once and
shared by both Solana and Soroban** — the Rust source conventions, the `code_explorer` prompt,
and the Rust failure-mode prompt fragment. Only the chain facet differs between them.

One honest caveat: the sharing is **not strictly hierarchical.** Solana and Soroban share the
Rust *language*, but Soroban's *model* — a contract that owns typed storage, with explicit
authorization and cross-contract calls — is closer to EVM's than to Solana's (programs
operating on externally-passed accounts). So facets are best expressed as **composable prompt
fragments keyed by concern** (§4.1), not a rigid two-level inheritance: `soroban` pulls the
`rust` language fragments *and* may reuse EVM-flavored "contract-owns-storage / authorization"
analysis fragments, while `solana` does not.

---

## 3. What is Solidity-specific today (audit)

Condensed from the audit; all in the *shared* pipeline, not the backends.

| Concern | Where | Ecosystem-specific? |
|---|---|---|
| System-model types (`ExplicitContract`, `solidity_identifier` + regex, `ContractComponent.state_variables`/`external_entry_points`, `ContractSort`, EOA `ExternalActor`) | [system_model.py](../composer/spec/system_model.py) | **Yes** — EVM-shaped |
| Driver pins the model: `run_component_analysis(ty=SourceApplication)`, `prepare_system(analyzed: SourceApplication)`, `main_instance` matching `solidity_identifier`, `_extract_all` → `ContractComponentInstance`, the "explicit contract instance with this solidity identifier" `extra_input` | [core.py](../composer/pipeline/core.py) | **Yes** — hardcoded |
| Analysis prompts ("Smart Contracts", `solidity_identifier` block, `ContractSort` deploy semantics, ERC20/4626/721) | `application_analysis_system.j2` / `application_analysis_prompt.j2` | **Yes** — heavy rewrite |
| Property-extraction prompts (reentrancy, oracle manipulation, MEV, storage layout, checked arithmetic/`uint256`) | `property_analysis_system_prompt.j2` / `property_analysis_prompt.j2` | **Yes** — failure-mode vocabulary |
| Connectivity validation (contract/component/actor shape) | `_validate_connectivity` in [system_analysis.py](../composer/spec/system_analysis.py) | **Yes** — structure reusable, types EVM |
| fs-exclusion default (`lib/`, `test/`, `.sol` carve-out) | `FS_FORBIDDEN_READ` [util.py:59](../composer/spec/util.py) | **Yes** — but already a per-input param |
| `code_explorer` prompt ("smart contract source code") | [code_explorer.py](../composer/spec/code_explorer.py) | Cosmetic |
| Source tools (`fs_tools`, `code_explorer`, `code_document_ref`) | [source_env.py](../composer/spec/source/source_env.py) | **No** — language-neutral, read Rust fine |
| `backend_guidance` ("what's expressible downstream") | [prop_inference.py](../composer/spec/prop_inference.py) | **No** — already backend-supplied |
| Report (`Verdict`, `RuleName`, `unit_file`, `Outcome`) | [report/collect.py](../composer/spec/source/report/collect.py) | **No** — neutral |

Two useful facts fell out: `run_component_analysis` is *already* generic (`[T: BaseApplication]`)
— only the driver pins `SourceApplication`; and `SolidityIdentifier`'s regex already accepts
Rust identifiers, so it's not a hard blocker (just misnamed).

---

## 4. The seam: a `Language` facet and a `Chain` facet

Two small protocols, composed. The **chain** carries its **language**; the driver consumes the
composed ecosystem (which is just a resolved chain). Sketch (illustrative, not final signatures):

```python
# composer/pipeline/ecosystem.py
type LanguageTag = Literal["solidity", "rust"]
type ChainTag = Literal["evm", "solana", "soroban"]

@dataclass(frozen=True)
class PromptPair:
    system: str        # j2 template name
    initial: str

class Language(Protocol):
    """Shared by every chain that uses this source language."""
    name: LanguageTag
    default_forbidden_read: str          # Cargo layout vs Foundry layout
    code_explorer_prompt: str            # "Rust source" vs "Solidity source"
    failure_modes_partial: str           # j2 partial: language-level failure modes (overflow, panics …)

class Chain[App: BaseApplication, Main, Unit](Protocol):
    name: ChainTag
    language: Language                   # <-- the shared facet (RUST for solana AND soroban)

    # --- domain model ---
    system_model: type[App]              # the analyzed pydantic type
    def validate_analysis(self, app: App, expected_main: str | None) -> list[str]: ...
    def locate_main(self, app: App, source: SourceCode) -> Main: ...
    def units(self, main: Main) -> list[Unit]: ...

    # --- prompts (chain templates that compose in the language partials, §4.1) ---
    analysis_prompts: PromptPair
    property_prompts: PromptPair

type Ecosystem = Chain                   # an ecosystem is a chain that carries its language
```

`Main`/`Unit` generalize today's `ContractInstance` / `ContractComponentInstance` — thin index
wrappers over `App` that the driver hands to the backend (`to_artifact_id`, `prepare_system`) and
to property inference. For EVM they *are* those types unchanged.

A registry selects by chain tag; `RUST` and `SOLIDITY` are the shared language singletons:

```python
RUST     = _RustLanguage(...)            # authored ONCE
SOLIDITY = _SolidityLanguage(...)
ECOSYSTEMS: dict[ChainTag, Ecosystem] = {"evm": EVM, "solana": SOLANA, "soroban": SOROBAN}
# where SOLANA.language is RUST and SOROBAN.language is RUST — the same object.
```

### 4.1 Prompt composition — how the Rust prompts are shared

Prompts are **assembled from fragments with Jinja2 includes/inheritance**, not duplicated. A
chain's property template pulls the shared language fragment, then adds its own:

```jinja
{# composer/templates/solana/property_prompt.j2  (soroban/ is identical but for the last include) #}
{% extends "property_prompt_base.j2" %}
{% block failure_modes %}
  {% include "rust/_failure_modes.j2" %}      {# SHARED: overflow, panics, unwrap — solana AND soroban #}
  {% include "solana/_failure_modes.j2" %}    {# chain-specific: signer/owner/PDA/CPI checks #}
{% endblock %}
```

So `rust/_failure_modes.j2`, the Rust `code_explorer` prompt, and the Cargo `forbidden_read` are
authored once as the `RUST` language and referenced by both `solana` and `soroban`; Soroban swaps
only the final `{% include "soroban/_failure_modes.j2" %}` and its chain facet. The base template
(`property_prompt_base.j2`) holds the ecosystem-neutral skeleton — the invariant / safety /
attack-vector framing, the output contract — so *that* is shared across all three.

---

## 5. Selection: an application parameter

The ecosystem is chosen per application, threaded to `run_pipeline` alongside the backend.

- **Built-in apps.** `run_autoprove_pipeline` / `run_foundry_pipeline` pass `ecosystem=EVM`
  explicitly (one line; both are EVM).
- **Rust apps.** The [`AppDescriptor`](./rust-applications.md) gains an `ecosystem` field
  (default `"evm"`); the host resolves `ECOSYSTEMS[descriptor.ecosystem]` and passes it into
  `run_application`. This adds one string field to `rust/autoprover-sdk` and one lookup in
  `composer/rustapp/host.py`.

```python
# composer/rustapp/descriptor.py
class AppDescriptor(BaseModel):
    ...
    ecosystem: ChainTag = "evm"      # "evm" | "solana" | "soroban"
```

So a Solana application is `ecosystem="solana"` + a Solana backend wheel, and a Soroban
application is `ecosystem="soroban"` + a Soroban backend wheel. Nothing else in the app shell
changes — the generic entry point/frontend already synthesize from the descriptor, and both Rust
chains transparently pick up the shared `RUST` language facet.

---

## 6. Driver generalization

Localized changes; the phase chain and concurrency are untouched.

**`run_pipeline`** ([core.py](../composer/pipeline/core.py)) takes `ecosystem: Ecosystem` and
stops hardcoding EVM:

```python
async def run_pipeline[P, FormT, H, A, App](
    backend: PipelineBackend[P, FormT, H, A, App],
    run: PipelineRun[P, H],
    ecosystem: Ecosystem[App, ...],
    *, ...
):
    analyzed = await run.runner(..., lambda: run_component_analysis(
        ty=ecosystem.system_model,                       # was: SourceApplication
        prompts=ecosystem.analysis_prompts,              # was: hardcoded templates
        validate=ecosystem.validate_analysis,            # was: _validate_connectivity
        expected_main_id=source.contract_name, ...))
    prepared = await backend.prepare_system(analyzed, run)
    ...
    main = ecosystem.locate_main(analyzed, run.source)   # was: main_instance(...) by solidity_identifier
    batches = await _extract_all(ecosystem.units(main), ecosystem.property_prompts, ...)
```

**`run_component_analysis`** ([system_analysis.py](../composer/spec/system_analysis.py)) —
already generic over `T`; additionally accept the prompt pair + validation function instead of
importing `_validate_connectivity` and hardcoding template names.

**`run_property_inference`** ([prop_inference.py](../composer/spec/prop_inference.py)) — accept
the ecosystem's property prompt pair and a generic `Unit` (it already takes `backend_guidance`
as a param, so the "expressible downstream" axis stays backend-owned; the "failure modes in this
domain" axis moves into the ecosystem's prompt).

**`PipelineBackend` / `SystemAnalysisSpec`** — add the `App` type parameter so
`prepare_system(analyzed: App)` and `to_artifact_id(unit: Unit)` type-check against the paired
ecosystem. `SystemAnalysisSpec` keeps `analysis_key` + `extra_input` (backend/app-owned); the
analyzed *type* and templates move to the ecosystem.

---

## 7. The EVM ecosystem (= today, zero behavior change)

`EVM` = the `SOLIDITY` language facet ⊕ the `evm` chain facet, a faithful capture of current
behavior, so autoprove/foundry are byte-for-byte unchanged:

```python
SOLIDITY = _Language(
    name="solidity",
    default_forbidden_read=FS_FORBIDDEN_READ,
    code_explorer_prompt=CODE_EXPLORER_SYS_PROMPT,
    failure_modes_partial="solidity/_failure_modes.j2",
)

EVM = _Chain(
    name="evm",
    language=SOLIDITY,
    system_model=SourceApplication,
    analysis_prompts=PromptPair("application_analysis_system.j2", "application_analysis_prompt.j2"),
    property_prompts=PromptPair("property_analysis_system_prompt.j2", "property_analysis_prompt.j2"),
    validate_analysis=_validate_connectivity,          # moved, not rewritten
    locate_main=main_instance,                          # moved, not rewritten
    units=lambda main: [ContractComponentInstance(_contract=main, ind=i)
                        for i in range(len(main.contract.components))],
)
```

The migration is a *move*, not a rewrite: existing types, prompts, and functions become the EVM
ecosystem's members (the current monolithic prompts stay as-is at first; splitting out a
`solidity/_failure_modes.j2` partial can wait until Soroban wants to reuse EVM fragments, §8).
This is the safety property of the proposal — the refactor is provably behavior-preserving for
EVM before any Rust chain adds anything.

---

## 8. The Rust chains: Solana and Soroban (shared language facet)

Both are `RUST` ⊕ their chain facet. The **`RUST` language facet is authored once** and both
reuse it verbatim:

```python
RUST = _Language(
    name="rust",
    # Cargo layout: exclude target/ and .git; KEEP tests/ (unlike Foundry) and the crate sources.
    default_forbidden_read=r"(^target/.*)|(^\.git.*)|(.*\.lock$)",
    code_explorer_prompt=RUST_CODE_EXPLORER_PROMPT,        # "Rust source … modules/traits/impls"
    failure_modes_partial="rust/_failure_modes.j2",        # overflow/underflow, panic!/unwrap/expect, ownership
)
```

`rust/_failure_modes.j2` is the concrete answer to "share Rust prompts between Solana and
Soroban": it is `{% include %}`d by both chains' property templates (§4.1). Each chain then
supplies only its own model + validation + platform fragment.

### 8.1 Solana chain (`RUST ⊕ solana`)

- **System model** (`composer/spec/solana/model.py`, new) — `SolanaApplication` with `Program`
  (program id / `crate::module`), `Instruction` (entry points), `AccountGroup` / account
  constraints (Solana accounts are **passed in**, not owned storage), and CPI targets / signers
  in place of EOA `ExternalActor`.
- **Platform failure fragment** (`solana/_failure_modes.j2`) — missing signer/owner checks,
  account substitution / confused-deputy, unvalidated PDA seeds, arbitrary CPI, lamport/rent
  draining, missing Anchor constraints (`has_one`, `constraint`, `seeds`/`bump`).
- **`locate_main` / `units`** — main = the target program; units = its instructions (or
  account-validation structs).

### 8.2 Soroban chain (`RUST ⊕ soroban`)

- **System model** (`composer/spec/soroban/model.py`, new) — `SorobanApplication` with `Contract`
  (`#[contract]`), `ContractFunction` (`#[contractimpl]` entry points), and **typed contract
  storage** (`instance` / `persistent` / `temporary`, each with TTL/archival) — i.e. the contract
  **owns** its state, closer to EVM than to Solana. Authorization is explicit (`require_auth` /
  `require_auth_for_args`); cross-contract calls go through generated clients; custom types are
  `#[contracttype]`.
- **Platform failure fragment** (`soroban/_failure_modes.j2`) — missing/incorrectly-scoped
  `require_auth`, storage-durability misuse (temporary vs persistent) and TTL/archival
  (entry-expiration) bugs, unchecked cross-contract results / reentrancy, replay. The Rust
  overflow/panic modes come from the shared `rust/_failure_modes.j2` — **not repeated here.**
- **`locate_main` / `units`** — main = the target contract; units = its contract functions.
- **Fragment reuse across the *chain* axis, too.** Because Soroban's model is
  contract-owns-typed-storage with explicit authorization, its *analysis* prompt can
  `{% include %}` EVM-flavored fragments about storage and authorization that Solana cannot —
  the non-hierarchical sharing noted in §2.1. This is exactly why fragments beat rigid inheritance.

The Solana/Soroban *backends* (formalization) are out of scope here — each plugs in via
[rust-applications.md](./rust-applications.md) and pairs with its chain's `App` model.

---

## 9. What stays shared and unchanged

- Source tools (`fs_tools`, `code_explorer`, `code_document_ref`) — already language-neutral; the
  only ecosystem input is the `forbidden_read` default and the explorer prompt string.
- The report (`collect` / `Verdict` / schema) — neutral; `ReportBackend` already widened.
- Caching, the multi-round property loop, interactive refinement, `run_to_completion`, the whole
  agent plumbing.
- The backend seam and the Rust app framework — a Solana or Soroban verifier is "just another
  backend."

---

## 10. Phased plan

1. **Extract `Language` + `Chain` + `EVM` (= `SOLIDITY ⊕ evm`), behavior-preserving.** Add
   `composer/pipeline/ecosystem.py`; move the `SourceApplication` reference, template names,
   `_validate_connectivity`, `main_instance`, and unit-enumeration into `EVM`, and the
   `forbidden_read`/`code_explorer` defaults into `SOLIDITY`. Thread `ecosystem` through
   `run_pipeline` / `run_component_analysis` / `run_property_inference`; default `EVM`. Prompts
   stay monolithic for now (no fragment split yet). **Gate:** autoprove + foundry suites
   unchanged; a golden-run diff is empty.
2. **Add the `App` type parameter** to `PipelineBackend` / `SystemAnalysisSpec` so the
   ecosystem↔backend pairing type-checks.
3. **Wire selection into `rustapp`.** `AppDescriptor.ecosystem: ChainTag` (SDK + Python mirror) +
   registry lookup in `host.py`.
4. **Author the `RUST` language facet + the Solana chain.** `RUST` (Cargo `forbidden_read`, Rust
   `code_explorer` prompt, `rust/_failure_modes.j2`); introduce the fragment-composition
   convention (`property_prompt_base.j2` + `{% include %}`); `SolanaApplication` model, Solana
   prompts + `solana/_failure_modes.j2`, `locate_main`/`units`. **Gate:** analysis + extraction
   produce sane properties on a sample Anchor program (a null/echo backend suffices).
5. **Author the Soroban chain, reusing `RUST`.** `SorobanApplication` model + Soroban prompts;
   the *only* new prompt content is `soroban/_failure_modes.j2` and the Soroban analysis template
   — the Rust language fragments are inherited. **Gate:** the shared `rust/_failure_modes.j2` is
   referenced by both chains and appears in neither chain's own fragment (no duplication).
6. **Solana / Soroban backends** — separate efforts, via the Rust framework.

---

## 11. Open questions

1. **Is `Unit` uniform enough across ecosystems?** EVM's unit is a contract component; Solana's
   might be an instruction *or* an account-validation struct. If per-unit shape diverges too much,
   `run_property_inference` may need the ecosystem to own unit rendering (a `render_unit(unit) -> dict`
   hook) rather than a shared template variable. Decide when authoring the Solana property prompt.
2. **One backend, multiple ecosystems?** Could a single Rust backend serve both (unlikely given the
   `App` pairing)? Keep the pairing explicit for now; revisit if a genuinely cross-domain backend
   appears.
3. **Report labels by ecosystem.** Should the report say "program"/"instruction" vs
   "contract"/"rule"? Today `backend_tag` drives labels; an `ecosystem` tag on the report may be
   the cleaner source for domain nouns. Minor; defer.
4. **Prompt template packaging.** Templates live in a shared dir; the fragment convention needs
   per-facet subdirs (`rust/`, `solidity/`, `solana/`, `soroban/`, plus shared `*_base.j2`) and a
   Jinja loader that resolves includes across them. Confirm the loader supports `{% extends %}` /
   `{% include %}` across subdirs (it should — it's stock Jinja).
5. **Fragment granularity.** Is a single `_failure_modes.j2` partial per facet the right grain, or
   do we want finer per-concern fragments (storage / authorization / arithmetic) so Soroban can
   pull the EVM *storage/auth* fragments (§8.2) without the EVM *contract-deployment* ones? Start
   coarse (one partial per facet); split a fragment only when a second consumer wants part of it.
6. **`SolidityIdentifier` naming.** Rename the shared field/type to an ecosystem-neutral
   `SourceIdentifier`, or leave it (regex already fits Rust)? Cosmetic; a rename touches many
   annotations — sequence it after Phase 1.

---

## 12. Key files

| Concern | File |
|---|---|
| Driver to generalize | [composer/pipeline/core.py](../composer/pipeline/core.py) |
| Ecosystem seam (new) | `composer/pipeline/ecosystem.py` |
| System analysis (accept ecosystem) | [composer/spec/system_analysis.py](../composer/spec/system_analysis.py) |
| Property inference (accept ecosystem) | [composer/spec/prop_inference.py](../composer/spec/prop_inference.py) |
| EVM system model (→ EVM ecosystem) | [composer/spec/system_model.py](../composer/spec/system_model.py) |
| Analysis / property prompts (EVM) | `composer/templates/application_analysis_*.j2` · `property_analysis_*.j2` |
| fs-exclusion default | [composer/spec/util.py](../composer/spec/util.py) |
| Source tools (unchanged) | [composer/spec/source/source_env.py](../composer/spec/source/source_env.py) · [code_explorer.py](../composer/spec/code_explorer.py) |
| Language facets + shared prompt fragments (new) | `composer/templates/{rust,solidity}/_failure_modes.j2` · `*_base.j2` |
| Solana chain model / prompts (new) | `composer/spec/solana/…` · `composer/templates/solana/…` |
| Soroban chain model / prompts (new) | `composer/spec/soroban/…` · `composer/templates/soroban/…` |
| Ecosystem selection in Rust apps | [composer/rustapp/descriptor.py](../composer/rustapp/descriptor.py) · [host.py](../composer/rustapp/host.py) · [rust/autoprover-sdk/src/lib.rs](../rust/autoprover-sdk/src/lib.rs) |
| The backend seam (unchanged) | [docs/formalization-abstraction.md](./formalization-abstraction.md) |
