# Soroban Prompt Templates

Templates for Soroban/Stellar smart contracts. The structure is similar to 
`composer/templates/solana/`, but we use Soroban terminology: contracts, entry points,
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

Rust-level issues are in `rust/_vulnerability_patterns.j2`; Soroban-specific
issues are here.

## Model

These templates render against `SorobanApplication`, defined in
[composer/spec/soroban/model.py](../../spec/soroban/model.py) and bound into the seam as `SOROBAN`
in [composer/pipeline/ecosystem.py](../../pipeline/ecosystem.py). That module is authoritative; the
sketch below is a reading aid. Because the four templates are registered in
`template_manifest.json`, [tests/test_fuzzed_templates.py](../../../tests/test_fuzzed_templates.py)
renders each of them against hundreds of generated models — an undefined variable or a field rename
that breaks a template fails CI.

Fields:

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
  auth: list[AuthRequirement]
  storage: list[StorageAccess]
  calls: list[ContractCall]
  events, errors, requirements

AuthRequirement:
  address
  kind: "require_auth" | "require_auth_for_args"
  description

StorageAccessSite:
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
`sibling_contracts` — that is `SorobanComponentInstance`, the ecosystem's `Unit`. It resolves the
component's `functions` and `storage_keys` name lists into objects, because a bare storage key
without its durability is not interpretable. `_soroban_validate` rejects a name that would not
resolve, so the templates never have to render a hole.

## Backend Boundary

`property_system.j2` includes `{{ backend_guidance }}` — the system prompt, not the initial one,
because the guidance is fixed for a whole run and so belongs inside the cached prefix. For Certora Sunbeam,
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
