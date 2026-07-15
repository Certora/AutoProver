# solidity_ast test fixtures

Fixtures for the `certora_autosetup/solidity_ast/` pydantic AST models.

- `contracts/` — feature-breadth Solidity sources. `breadth_06.sol` is shared by
  solc 0.6.x and 0.7.x; `breadth_08.sol` adds the 0.8-only constructs (custom
  errors, UDVTs, user-defined operators, unchecked, named mapping params, ...).
- `solc_<ver>.asts.json` — real `certoraRun --dump_asts` output (sanitized to be
  machine-independent). Regenerate with `generate_fixtures.py` (dev-only, not run
  in CI; see its docstring).
- `expected_parent_graph_0_8_30.json` — golden output of the frozen legacy
  parent-graph algorithm over `solc_0_8_30.asts.json` (`generate_fixtures.py --golden`).
- `vyper_mixed.asts.json` — **SYNTHETIC (hand-written), not a real certoraRun
  dump**: pins the passthrough shape for a mixed Solidity+Vyper dump. One level-1
  entry with two level-2 sources: a real solc-0.8.30-shaped Solidity `Counter`
  and a Vyper source whose nodes use the Vyper dialect (`ast_type`/`node_id`
  keys instead of `nodeType`/`id`).
