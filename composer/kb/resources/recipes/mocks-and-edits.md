> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

# 6. Mocks and source edits `[EDIT]`

The governing axis: **CVL-expressibility.** Behavior expressible as a CVL
model — summaries, ghosts, hooks, axioms — is modeled in CVL. A mock or
source edit is reserved for behavior that must exist at EVM level for the
Prover to use it:

- **Storage layout declarations** — the R2 harness (declarative fields only).
- **Dispatch targets** — a `DISPATCH` list or `use_fallback` treatment needs a
  concrete implementation among the provided contracts; when a property
  genuinely hinges on `fallback`/`receive` or plain-ETH-transfer behavior, a
  minimal mock provides the body to dispatch to.
- **Rerouting harnesses** — an internal function whose storage-pointer
  arguments the summary must read is rerouted to an external library harness
  function; the harness is Solidity by necessity.
- **Reference implementations** — a minimal concrete implementation of an
  interface placed among the provided contracts, when dispatching to real code
  is preferable to a symbolic model for the property at hand.

Obligations on every mock or edit: the diff is minimal, and it carries a
stated argument that the change preserves the semantics relevant to the
properties under verification. A mock is itself an assumption — its body is a
claim about how the real counterparty behaves — and is justified the same way
a summary is.
