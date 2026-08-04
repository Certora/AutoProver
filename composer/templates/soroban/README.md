# Soroban Prompt Templates

Templates for the Soroban/Stellar Rust facet. They mirror
`composer/templates/solana/`, but use Soroban nouns: contracts, entry points,
`Address` auth, storage kind, and cross-contract calls.

| File | Role |
|---|---|
| `analysis_system.j2` | System prompt for model extraction |
| `analysis_prompt.j2` | User prompt for extracting the application model |
| `component_context.j2` | Component context rendered into property prompts |
| `property_system.j2` | System prompt for property inference |
| `property_prompt.j2` | User prompt for property inference |
| `_platform_model.j2` | Soroban execution model facts shared by prompts |
| `_vulnerability_patterns.j2` | Soroban-specific bug patterns |

Rust-level issues stay in `rust/_vulnerability_patterns.j2`; Soroban-specific
issues stay here.

## Expected Model

These templates assume a future `SorobanApplication` model similar to
`composer/spec/solana/model.py`. `ChainTag` already reserves `"soroban"`; the
remaining wiring is the model, validators, unit locator, and `ECOSYSTEMS` entry.

Required fields:

```text
SorobanApplication:
  components: list[SorobanContract | SorobanAuthority]
  contracts, authorities

SorobanContract:
  name, contract_identifier, contract_id, description
  storage_entries: list[StorageEntry]
  functions: list[SorobanFunction]
  components: list[ContractComponent]

StorageEntry:
  key
  durability: "instance" | "persistent" | "temporary"
  value_type
  description

SorobanFunction:
  name, description, args, returns
  auth: list[AuthRequirement]        # empty is meaningful
  storage: list[StorageAccess]
  calls: list[ContractCall]
  events, errors, requirements

AuthRequirement:
  address
  kind: "require_auth" | "require_auth_for_args"
  description

StorageAccess:
  key
  durability
  access: "read" | "write" | "remove" | "extend_ttl"

ContractCall:
  target_contract
  description

ContractComponent:
  name, description, functions, storage_keys, interactions, requirements

SorobanAuthority:
  name, description, assumptions
```

`component_context.j2` expects a resolved context with `app`, `contract`,
`component`, `functions`, `storage_entries`, `sibling_components`, and
`sibling_contracts`.

## Backend Boundary

`property_prompt.j2` includes `{{ backend_guidance }}`. For Certora Sunbeam,
that guidance should cover Rust `#[rule]` specs using Cavalier/CVLR macros
(`cvlr_assert!`, `cvlr_assume!`, `cvlr_satisfy!`), nondeterministic inputs, and
`certoraSorobanProver` config. The Soroban templates should identify
security properties; backend guidance decides how to express them.

## Sources

- Soroban auth and `Address::require_auth`
- Soroban storage kind, TTL, archive, and restore rules
- SEP-41 token interface and Stellar Asset Contract behavior
- Certora Sunbeam documentation
- Public Soroban audit checklists and detector catalogs
