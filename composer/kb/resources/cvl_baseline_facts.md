# CVL Baseline Facts

> Where these facts and the CVL manual or other Certora materials diverge,
> these facts govern.

Language-wide rules. Summarization semantics and methods-block mechanics live
in the summarization knowledge base; invariant and quantifier knowledge lives
in the invariants & quantifiers knowledge base; situational formulas live
behind the recipes index.

## Types and arithmetic

- Numeric variables default to `mathint` — arbitrary precision; operations on
  `mathint` never overflow or underflow. The exceptions, which use the
  appropriate `(u)intK` type: variables passed *to* contract functions,
  variables holding the *result* of a contract call, and array indices.
- Every mathematical operation (bitvector operations excluded) produces
  `mathint`. Narrowing back to `(u)intK` uses a cast: `assert_uintK(x)`
  reports a violation when `x` is out of range; `require_uintK(x)` silently
  drops those executions — a vacuity risk. Default to `assert_` casts.
- Operands of comparisons and arithmetic are promoted to `mathint`
  automatically; explicit `to_mathint` casts are rarely needed.
- Bitwise operations are over-approximated by default; avoid them where
  possible.

## Values, variables, ghosts

- With the exception of ghosts, CVL values are immutable: `a[0] = 4;` and
  `s.f = x;` are illegal. Values are constrained instead:
  `require a[0] == 4;` — watching for vacuity from conflicting constraints.
- A variable declared but never bound or constrained takes a
  non-deterministic value.
- Out-of-bounds array access yields an unconstrained value rather than a
  revert; guard indices explicitly.
- User-defined type names are qualified by their defining contract:
  `MyContract.MyStruct s;`
- Meaningful numeric constants (fee caps, expiry periods) are named via
  `definition` rather than written inline.

## Rules

- The final statement of every rule is an `assert` or `satisfy` — not a
  conditional that contains one.
- `method` variables are declared as rule parameters, never inside the rule
  body.
- Parametric rules and invariants instantiate methods of every provided
  contract; restrict with `filtered { f -> f.contract == currentContract }`
  when only the verified contract is intended — and treat the restriction as
  a theorem edit to be stated.

## Functions and calls

- Every CVL call to a contract function takes an `env` as its first argument
  unless the methods-block entry declares `envfree`; `envfree` claims are
  verified by the Prover, not assumed.
- Every CVL function ends in a `return` statement, including functions with
  no return value. Void CVL functions omit the `returns` clause entirely
  (`returns void` is not written).
- The return value of an `@withrevert` call is undefined when the call
  reverted; it is rarely used.
- In CVL, `^` is exponentiation; bitwise exclusive-or is the keyword `xor`.

## Hooks

- Hooks fire only on contract-code storage accesses; reads and writes made
  from CVL, including direct storage access, do not trigger hooks.
- Hooks do not fire recursively: a storage access performed inside a hook
  body does not trigger the hook again.
- Hooks do not fire during a `reset_storage` command; ghost mirrors are stale
  after a reset and are re-established explicitly.
- Hooks do *not* fire during on a rollback of storage at a revert.

## Signature syntax

- Parameter types in `preserved` blocks, signature literals (`sig:...`), and
  dispatch-list signatures carry no data locations. (Methods-block entries
  have their own location rules — summarization knowledge base.)
