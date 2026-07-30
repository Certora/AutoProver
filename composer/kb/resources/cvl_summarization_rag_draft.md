# CVL Summarization — Knowledge Base (v0.5)

> Where this document and the CVL manual or other Certora materials diverge on
> summarization, this document governs.

This document has two parts. **Part A** states the semantics of CVL
summarization — ground truth usable by any consumer, including diagnostic
agents interpreting counterexamples. **Part B** is policy and workflow for the
agent that *writes* summaries: when to summarize, how to triage havocs, and the
obligations attached to emitting an entry.

---

# PART A — SEMANTICS

## A1. What a summary is

A summary is declared in the `methods` block by appending `=> <summary>` to an
entry:

```cvl
methods {
    function C.f(uint x) external returns (uint) => ALWAYS(0);        // exact
    function _.transfer(address to, uint a) external => NONDET;       // wildcard
    function SomeLibrary._ external => NONDET;                        // catch-all
    unresolved external in C.foo() => DISPATCH [ D.baz() ] default HAVOC_ECF;  // catch-unresolved
}
```

When a matching call site is reached during verification, the Prover replaces
the execution of the callee's code with the summary's approximation. A summary
replaces **only the callee's code execution**: any ETH value attached to the
call is transferred regardless of summary type — balance movement is part of
the call machinery, outside what a summary can alter.

Constructor code is outside the reach of summarization: constructors cannot be
summarized, and summaries are not applied to calls made during constructor
execution — including catch-unresolved (`unresolved external`) entries and
their dispatch lists.

Verification runs against a set of contracts provided as input to the Prover —
the contract being verified plus any additional contracts supplied alongside it
(Certora materials call this set "the scene"). Throughout this document,
"provided contracts" refers to this set.

**The two directions of approximation** (use these words in justifications):

- *Over-approximation*: the summary permits **more** behaviors than the real
  code (e.g., `HAVOC_ALL`). Sound: proofs that survive it are valid. Cost:
  spurious counterexamples.
- *Under-approximation*: the summary permits **fewer** behaviors than the real
  code (e.g., `NONDET` on a state-mutating function suppresses its side
  effects; `DISPATCHER(true)` closes the set of callees). Unsound for proofs:
  real bugs can be masked. Only acceptable when explicitly justified and
  documented as a verification assumption.

General soundness anchor: summary declarations are in general unsound, except
`HAVOC_ALL` (always sound) and `NONDET` applied to `view` functions (sound).

Zero-length-calldata `CALL` behavior is tunable via `optimistic_fallback`.

## A2. Entry matching and application — which call sites an entry affects

### A2.1 Entry patterns

- **Exact**: `function C.f(uint) external returns (uint)` — one method, one
  contract. Contract omitted ⇒ `currentContract`. The contract named is the
  method's *defining* contract: a method `Child` inherits from `Parent` is
  entered as `Parent.foo`, not `Child.foo`. Must declare the return type
  (checked). May carry `envfree`, `optional`, `with(env e)`, and a summary.
- **Wildcard**: `function _.f(uint) external => <summary>` — this signature on
  *any* contract. Must NOT declare a return type; must have a summary; cannot
  be `envfree` or `optional`.
- **Catch-all**: `function C._ external => <summary>` — *every* external method
  of contract `C`. Only **havocing** summaries allowed here. Applies only when
  the Prover can prove the receiver is `C` (always true for libraries, which
  resolve statically).
- **Catch-unresolved-calls**: `unresolved external in <scope> => <summary>` —
  applies only when the callee's **selector** is unresolved (Certora materials
  sometimes say "sighash" for the same thing), e.g. `target.call(data)` with
  symbolic `data`. Scope forms: `C.f()` (unresolved sites inside `C.f`'s
  body), `_.f()` (inside any function with `f`'s signature), `C._` (inside any
  function of `C`), `_._` (anywhere). When several entries could cover the
  same call site, the most specific scope applies: matching tries `C.f()`
  first, then `_.f()`, then `C._`, then `_._`, and the first match's summary
  is used. Only dispatch-list, havoc, or `NONDET` summaries allowed. The entry attaches to the function *containing* the
  unresolved call site: if `C.foo` calls (resolved) `D.bar` and `D.bar`
  contains the unresolved call, an entry scoped to `C.foo()` does not reach it
  — scope to `D.bar()`.

The notation `C._` denotes opposite roles in the two entry kinds above. In a
methods-block catch-all entry, `C._` names `C` as the **callee**: every
external method *of* `C`. In an `unresolved external in C._` scope, `C._`
names `C` as the **caller**: unresolved call sites *within* `C`'s functions.

Entry hygiene: an external entry carrying neither `envfree`/`optional` nor a
summary annotates nothing — external methods of provided contracts are exposed
to CVL automatically, so such entries are omitted. `internal` entries never
carry `envfree`. A signature involving a user-defined type alias names the
alias itself, not the underlying type.

### A2.2 Visibility and the public-function duality

Every entry is marked `internal` or `external` and only matches that face.
Solidity `public` functions compile to an internal implementation plus an
external wrapper:

- An **`internal` summary on a public `f`** effectively summarizes
  *everything*: internal calls, external calls (wrapper → summarized
  implementation), and calls from CVL (CVL calls the wrapper, which calls the
  summarized implementation).
- An **`external` summary on a public `f`** summarizes only true external calls
  (`this.f()`, `c.f()`); internal calls use the real body; calls from CVL are
  not summarized (direct CVL calls never use summaries).

Default guidance: summarize public functions at the `internal` face.

### A2.3 Application policy: `ALL` vs `UNRESOLVED` vs `DELETE`

An optional policy trails the summary: `=> NONDET ALL;`. Semantics:

- `ALL`: apply at every matching call site, resolved or not.
- `UNRESOLVED`: apply only where the callee (target contract or selector)
  could not be resolved.

These defaults govern every entry; consult this table rather than assuming a
policy:

| Entry kind                          | Default policy | Notes |
|-------------------------------------|----------------|-------|
| `internal` (any)                    | `ALL`          | internal calls are always resolved; `UNRESOLVED` is a compile error |
| `external`, exact (named contract)  | `ALL`          | |
| `external`, wildcard `_.f(...)`     | `UNRESOLVED`   | a wildcard summary silently skips call sites the Prover resolved (e.g. via a link, or a statically known receiver) |

Consequence: `function _.balanceOf(address) external => NONDET;` does not apply
to a call the Prover resolved to a provided token contract. Reaching resolved
sites as well requires writing `ALL` — which overrides real, available code and
needs justification.

Resolution is per call site, so under `UNRESOLVED` two call sites invoking the
same `C.foo` may split: one resolved (real code runs) and one unresolved (the
summary applies). The same function then behaves differently at different
sites within one rule. Where this split is possible, make it deliberate — or
use `ALL` so every site sees the same model.

#### `DELETE` Behavior

The `DELETE` policy acts like `ALL`, but also removes the matched
method from the prover's model entirely. CVL calls to a deleted method
become rule violations; parametric rules skip it. `DELETE` exists for one
narrow situation: a function whose implementation pattern interferes with the
Prover's internal analyses in a way no summary can repair, and which is
irrelevant to every property being verified. It is not an abstraction or
performance tool, and it changes what parametric rules quantify over (B2.4).


### A2.4 Precedence when several entries match

Most-specific wins: **exact > wildcard > catch-all** — computed only over
entries that carry summaries. An entry without a summary neither wins nor
blocks. It is fine to declare an exact, summary-less `envfree` entry for
`C.f` alongside a wildcard summary for the same signature:

```cvl
function C.f(uint) external returns (uint) envfree;   // no summary
function _.f(uint) external => NONDET;                // wildcard summary
```

The wildcard's summary still applies to `C.f` (subject to its application
policy); the exact entry neither overrides nor shields it.

When one call site could fall under both a wildcard methods-block entry (with
`UNRESOLVED` policy) and an `unresolved external` entry, the wildcard
methods-block entry applies.

### A2.5 Calls from CVL

**No summary ever applies to a call made directly from CVL.** This holds
regardless of entry kind or application policy, and for both ways a rule
invokes contract code: a call to a concrete function written out in the rule
(`c.deposit(e, args)`) and a call through a parametric `method f` variable
(`f(e, args)`). Both run the real code.

Calls made *by* the invoked code are ordinary contract call sites, and
summaries apply to them normally — which is how the public-function duality
(A2.2) produces the one indirect effect: CVL calls the public function's
external wrapper, and the wrapper's call into the internal implementation is
subject to `internal`-face summaries.

## A3. The `links` block

A `links` entry tells the Prover that a storage location holding an address
resolves to a specific provided contract instance — the call becomes resolved
and **real code runs**:

```cvl
using Pool as pool;
using TokenImpl as tokenImpl;

links {
    pool.token => tokenImpl;                  // scalar field
    pool.holder.token => tokenImpl;           // struct path, arbitrary nesting
    pool.fixedTokens[2] => tokenImpl;         // array element
    pool.tokenMap[to_bytes4(0x12345678)] => tokenImpl;  // mapping, bytesN key
    pool.addrMap[tokenImpl] => tokenImpl;     // mapping, contract-alias key
    pool.registry[_] => tokenImpl;            // wildcard: every key/index
    pool.immutableToken => tokenImpl;         // immutables too
    pool.oracle => [oracleA, oracleB];        // multi-target
}
```

Semantics and fine print:
- Concrete entries beat wildcard entries on the same path; one entry may not
  mix concrete and wildcard indices across nesting levels.
- Dynamic-array links at concrete indices are **conditional on length**:
  `pool.tokens[1] => tokenA` asserts `tokens.length > 1 => tokens[1] ==
  tokenA`. A rule that never establishes the length gets no binding.
- Library contracts cannot be linked. Requires storage-layout information
  (solc ≥ 0.5.13).
- Multi-target (`=> [a, b]`) has the Prover consider every binding — e.g. a
  two-token contract where the tokens may or may not coincide.

**Scoping.** A link scopes to one *storage location*: `pool.token => [tokenA,
tokenB]` affects only calls made through `pool.token`, while covering every
function of `pool.token`'s interface through it.

Soundness accounting: a single-target link is an assumption ("this slot holds
that instance's address"), normally discharged by constructor/deployment
reality. A multi-target link additionally closes a world, exactly as
`DISPATCHER(true)` does, and carries the same obligation to state exclusions
(B4). Composition with summaries: linked call sites are resolved, so
default-policy (`UNRESOLVED`) wildcard summaries stop applying to them — link
what is known; wildcard-summarize the residue.

Where linking cannot apply: selector-unresolved raw calls (`target.call(data)`);
library calls; dynamically computed callee addresses (factory-deployed
children, assembly-derived) with no static storage path; callees with no code
available.

## A4. Summary catalog

The built-in summary operators follow. Two further kinds of summary complete
the picture and have their own sections: expression summaries — the right-hand
side is an arbitrary CVL expression — in A5, and rerouting summaries in A6.2.

### A4.1 View summaries: `ALWAYS`, `CONSTANT`, `PER_CALLEE_CONSTANT`, `NONDET`

All four model the callee as side-effect free: no contract storage changes
(attached ETH still moves, per A1). They differ in return-value assumptions:

- `ALWAYS(v)`: every call returns literal `v` (bool or integer). Strong
  under-approximation of return behavior; appropriate only when the return
  value is truly fixed by contract or stated assumption.
- `CONSTANT`: all calls *with this signature* return one identical (symbolic)
  value, across all callees.
- `PER_CALLEE_CONSTANT`: one symbolic constant per receiver contract.
- `NONDET`: below.

`CONSTANT`/`PER_CALLEE_CONSTANT` on functions with variable-sized outputs is a
vacuity source. Where a single consistent unknown value is wanted, a ghost
(A5) expresses it directly and is generally preferable to `CONSTANT`.

### A4.2 `NONDET` — precise semantics

`NONDET` replaces the call with: *state untouched, and the call returns a
fresh, unconstrained **return buffer***. The nondeterminism is injected at the
raw returndata level; a typed value exists only after the caller's decoder runs
on those bytes:

- `returndatasize()` is itself nondeterministic (may be zero, short, long)
  unless `optimisticReturnsize` or `superOptimisticReturnsize` is set.
- The *caller's own decoding logic* runs on that buffer. Solidity's generated
  returndatasize checks and ABI decoding apply, and decoding can revert on
  malformed or short data.
- The summarized call itself succeeds at the call boundary; any revert arises
  downstream in the caller's handling of the buffer.
- No functional consistency: two calls with identical arguments and receiver
  may yield different buffers. When a property needs `f(x)` consistent across
  calls, use a ghost summary (A5).

After a `NONDET`-summarized call, every contract's storage
and every ETH balance (beyond value carried by the call itself) holds exactly
what it held before; the summary's entire degree of freedom is the contents and
size of the returndata buffer.

Soundness: return side over-approximates (spurious counterexamples possible,
nothing hidden); state side **under-approximates for non-view callees** — real
side effects, including reentrant effects on the caller, are erased. `NONDET`
summaires are thus sound for `view`/`pure` callees;
they are a _documented_ assumption for anything else.

### A4.3 Havoc summaries: `HAVOC_ALL`, `HAVOC_ECF`

- `HAVOC_ALL`: arbitrary effects on all contracts' storage and ETH balances,
  arbitrary return buffer. Obliterates knowledge; the only always-sound
  summary.
- `HAVOC_ECF`: encodes "callee is not reentrant": arbitrary effects on *other*
  contracts, with two balance constraints. The verified contract's ETH balance
  does not decrease (beyond value sent with the call) — it may *increase*, as
  the callee may transfer ETH to the verified contract. The **callee's own ETH
  balance is unchanged** across the summarized call (beyond value carried by
  the call itself). The second constraint excludes real callee behaviors that
  change the callee's balance — most plainly, a callee that forwards received
  ETH onward to third parties; such executions do not exist in the model.
  Sound *given* the no-reentrancy assumption **and** given that the callee's
  execution does not change its own ETH balance — both stated when relied on.
  These balance constraints apply to every havoc type except `HAVOC_ALL`,
  including the `HAVOC_ECF` behavior `AUTO` applies (A4.5).
- Havoc return buffers are unconstrained, including size; wrong-size returns
  typically make the caller revert (suppress via `optimisticReturnsize`).
- Havoc summaries also havoc **non-persistent ghost state**: across the havoc,
  regular ghost values become unconstrained along with the havocked storage.
  Ghosts recording never-forget events (a call happened, an emit was
  attempted) are declared `persistent`, surviving both havoc and revert
  (A5.6); ghosts mirroring storage-like state stay regular, so they havoc when
  the storage they mirror does.

### A4.4 `DISPATCHER` and `DISPATCH` lists

- `DISPATCHER(false)` (= bare `DISPATCHER`): receiver is one of the provided
  implementations of the signature **or an unknown contract** whose behavior
  is modeled with an `AUTO` summary.
  Keeps the world open; in practice reports approximately the same
  violations as `AUTO`.
- `DISPATCHER(true)` (= `optimistic=true`): receiver is one of the provided
  implementations, period. **Closes the world** — an under-approximation whose
  justification ("the deployed callee is one of exactly these") must be
  documented. One receiver address binds to one implementation consistently
  within a counterexample. The Prover errors if no provided method matches the
  signature.
- `use_fallback=<bool>`: also consider provided contracts' `fallback` (proxy
  patterns).
- `DISPATCHER` cannot summarize library calls.
- `DISPATCH(...) [ C.foo(uint), _.bar(address), D._ ] default <havoc_summary>`:
  a curated candidate list, usable on wildcard entries and (chiefly) on
  catch-unresolved-calls entries. Pattern forms: exact `C.foo(uint)`; wildcard
  contract `_.bar(address)`; wildcard function `D._` (includes `D`'s fallback
  if implemented). Pessimistic mode (default) falls through to the `default`
  summary when no candidate's selector+address matches. `<havoc_summary>`
  is one of `NONDET` (with the usual soundness caveats), `HAVOC_ECF`, `HAVOC_ALL`, or `ASSERT_FALSE` (to _assert_ closed world behavior).
  `DISPATCH(optimistic=true)` inlines an `ASSUME FALSE` on the no-match branch
  — vacuity risk: if nothing can match, the whole path is assumed unreachable.
  No application policy should be applied on dispatch lists. Dispatch-list signatures carry no data locations on reference-type parameters.

### A4.5 `AUTO` (the default for unresolved calls)

Behavior depends on the call's opcode:
- `STATICCALL` (view/pure): `NONDET`.
- `CALL` / `CREATE`: `HAVOC_ECF`.
- `DELEGATECALL` / `CALLCODE` (includes external library calls): havoc the
  current contract only; other contracts' storage and ETH balances unchanged.

### A4.6 `ASSERT_FALSE`

Replaces the method with `assert false` — a proof obligation that the call site
is unreachable. Idiomatic as the `default` of a dispatch list
(`... default ASSERT_FALSE`) to *prove* every unresolved call was dispatched,
rather than assume it. Also enables Prover optimizations.

### A4.8 Soundness ledger at a glance

| Summary | Contract state | Return value | World |
|---|---|---|---|
| `HAVOC_ALL` | havoc all | unconstrained buffer | open — sound, always |
| `HAVOC_ECF` | havoc others; verified contract preserved; own balance non-decreasing; callee balance pinned | unconstrained buffer | open under no-reentrancy + callee-balance-unchanged assumptions |
| `AUTO` | per opcode (A4.5) | unconstrained buffer | open; the default for unresolved calls |
| `NONDET` | unchanged (under-approximation unless the callee is view) | unconstrained buffer (over-approximation) | — |
| `CONSTANT` / `PER_CALLEE_CONSTANT` | unchanged | one symbolic constant (per signature / per callee) — under-approximation; vacuity risk on variable-size returns | — |
| `ALWAYS(v)` | unchanged | fixed literal — strong under-approximation | — |
| `DISPATCHER(false)` | real code of candidates ∪ unknown-with-`AUTO` | real | open |
| `DISPATCHER(true)` / optimistic `DISPATCH` | real code of candidates | real | closed — an assumption |
| expression summary | whatever the expression does to ghosts (plus any contract calls it makes) | whatever it evaluates to; may revert (A5.6) | as modeled — the assumption is the expression itself |
| `ASSERT_FALSE` | n/a — unreachability is *checked*, not assumed | — | — |

## A5. Expression summaries: CVL functions, ghosts, environment, idioms

### A5.1 Basics

```cvl
function C.f(uint x) external returns (uint) => cvl_f(x);
function _.foo() external => fooImpl() expect uint256 ALL;
function _.g(uint x) external => ghostState[calledContract][x] expect uint256;
function _.getMoney(uint x) external => require_uint256(x + 22) expect uint256;
```

- The right-hand side is an **arbitrary CVL expression**, evaluated per call
  with the entry's bound parameters in scope, along with `calledContract`,
  `executingContract`, and the `with(env e)` variable if declared. A call to a
  CVL function or ghost is the common form; direct ghost-mapping accesses and
  compound expressions over the bound parameters (the third and fourth entries
  above) are equally valid.
- Wildcard entries require an `expect` clause (`expect uint256`, `expect void`,
  …) telling the Prover how to reinterpret the summary's value at each call
  site. The Prover often cannot check `expect` against the callee's true return
  type; a wrong `expect` is undefined behavior. Derive `expect` from the call
  sites' declared interfaces, and flag any site whose expected type differs.
- Ghost summary vs CVL-function summary: an uninterpreted `ghost` function
  gives **functional consistency** (same arguments ⇒ same value, within a
  model) — the right model for deterministic view functions (square roots,
  interest math). A CVL function gives full procedural freedom (branching,
  `require`s, ghost updates, calls into contract code).

### A5.2 Environment: `with(env e)`

Attach `with(env e)` to the entry and pass `e` into the summary. Inside the
summarized context: `e.msg.sender`/`e.msg.value` are those of the most recent
non-library external call (Solidity semantics — unchanged across delegatecalls
and library calls); `e.tx.origin` and `e.block.*` come from the outermost
call's environment.

### A5.3 `calledContract` and `executingContract`

Usable only in `methods`-block summary invocations (`executingContract` also in
hooks):
- `calledContract`: the `address(this)` of the call being summarized — the
  receiver for plain external calls; for internal, library, and delegate calls
  it is the *caller* (they execute in the caller's context).
- `executingContract`: the contract that *made* the call. Differs from
  `calledContract` only for non-delegate external calls.
Canonical uses: keying per-token ghost state in wildcard token summaries
(`cvlTransferFrom(calledContract, from, to, amount)`); modeling wrappers where
the inner callee must observe `msg.sender == executingContract`.

### A5.4 The ghost-mapping pattern

The standard way to give contracts *outside the provided set* persistent,
consistent state. The Prover has no storage for such contracts; a ghost mapping
keyed by `calledContract` becomes their storage, and CVL-function summaries
become their state transitions:

```cvl
ghost mapping(address => mapping(address => mathint)) tokenBalances;

methods {
    function _.balanceOf(address who) external
        => cvlBalanceOf(calledContract, who) expect uint256;
    function _.transferFrom(address from, address to, uint256 amount) external
        => cvlTransferFrom(calledContract, from, to, amount) expect bool;
}

function cvlBalanceOf(address token, address who) returns uint256 {
    return require_uint256(tokenBalances[token][who]);
}

function cvlTransferFrom(address token, address from, address to, uint256 amount) returns bool {
    bool success;
    if (success) {
        tokenBalances[token][from] = tokenBalances[token][from] - amount;
        tokenBalances[token][to]   = tokenBalances[token][to] + amount;
    }
    return success;
}
```

Properties of the pattern: reads and writes are mutually consistent across the
whole rule (unlike `NONDET`, where each `balanceOf` is fresh); one model covers
unboundedly many unknown token instances; the transition function *is* the
documented behavioral assumption ("moves exactly `amount` or nothing" —
excludes fee-on-transfer, rebasing, etc., and must say so). It is also the
natural residual branch of the hybrid dispatch (B4).

### A5.5 Summaries calling contract code; recursion

A CVL-function summary may call contract functions (e.g. invoking
`token.transfer(e, to, value)` from within a summary of a wrapper). Recursion
introduced this way is bounded by `--summary_recursion_limit N`; the default
bound is 0, so a summary whose evaluation re-triggers itself immediately
produces an assertion failure at the bound. `--optimistic_summary_recursion`
converts that assertion into an assumption (vacuity risk).

### A5.6 Reverts in summaries

CVL functions can revert, and the `revert(...)` statement is available.
Reverts propagate to callers exactly like Solidity reverts: a `revert` executed
inside an expression summary makes the *summarized call* revert in the calling
contract's execution, indistinguishably from a real callee revert. On such a
revert, all state is rolled back to the point before the call — **including
ghost mutations made inside the summary** — except ghosts declared
`persistent`, which survive reverts (CVL manual: Ghosts → Persistent ghosts). `lastReverted` is set uniformly for
CVL and Solidity calls; `@withrevert` stops propagation at the annotated call.
The revert populates returndata, but revert-*reason* propagation is not
guaranteed: contract code that parses specific error data from a summarized
revert should not be relied on.

```cvl
function cvlTransfer(address token, address to, uint256 amount) returns bool {
    if (tokenBalances[token][executingContract] < amount) {
        revert("insufficient balance");   // the caller observes a real revert
    }
    ...
}
```

The distinction that matters: **`require` vs `revert` inside a summary are
different models.** `require success` *prunes* the failing paths — an
assumption that failure never happens. `revert` *models* the failure so the
caller's handling of it — propagation, `try/catch`, low-level success flags —
is actually verified. If the property depends on what the contract does when
the callee fails, use `revert`.

Standing contrast: `NONDET` and havoc summaries never revert themselves (A4.2)
— reverts there arise only downstream, from the caller decoding a bad buffer.
Expression summaries are the only place revert behavior is modeled
deliberately.

### A5.7 Varying summary behavior across rules

There is one summary per method per specification; per-rule summaries are not
supported. The idiom for rule-dependent behavior: branch inside the summary on
a ghost flag that each rule sets to select the behavior it needs. The
alternative is splitting the specification into separate files/configurations.

### A5.8 Instrumentation idioms (summaries as observers)

- **Was `f` called / with what?** Summarize with a CVL function that sets
  `ghost bool fCalled` (and records arguments into ghosts), then performs the
  intended model. Counting and ordering: `ghost mathint callCount` increments;
  ordering protocols via ghosts encoding a state machine.
- **Observing while preserving exact behavior**: for *external* functions, have
  the summary itself call the real callee from CVL (ghost updates plus the real
  call — A5.5). For *internal* functions there is no reliable equivalent:
  calling internal functions from CVL is not dependable and should not be
  relied on; the options are modeling the behavior in the summary body or a
  rerouting harness that reimplements it (A6.2).
- Summaries read and write ghosts freely; they cannot write contract storage
  (only havoc summaries and rerouting harnesses affect storage).
- Instrumentation ghosts that must survive reverting paths — recording that a
  call was attempted even when the surrounding call later reverts — are
  declared `persistent` (A5.6).

## A6. Types across the summary boundary; rerouting

### A6.1 What converts

Summaries accept essentially arbitrary argument and return types: structs,
arrays, and user-defined value types all convert between Solidity and CVL. The
exceptions:

1. **Function pointers**: unconvertible.
2. **Storage pointers** (mappings above all): unconvertible.

An entry may still *mention* storage-pointer types in its signature — the entry
matches and the summary fires — but those parameters cannot be named-and-used
in the summary body. Unaccessed parameters may have any type:

```cvl
function MyLibrary.guess(int[] storage numbers) internal returns (int) => goodGuess1(numbers); // ILLEGAL
function MyLibrary.guess(int[] storage numbers, int g) internal returns (int) => goodGuess2(g); // legal
function MyLibrary.guess(int[] storage numbers) internal returns (int) => ALWAYS(42);           // legal
```

Location annotations on reference-type parameters in methods-block entries
follow a three-way rule: entries for **internal** functions carry them on
every reference-type parameter; entries for **external library** functions
must *include* them for `storage` parameters and *omit* them for the other
data locations (`memory` or `calldata`); entries for all other **external** functions carry none.

### A6.2 Rerouting summaries — the storage-pointer escape hatch

When the summary *needs* the storage-pointed data, reroute the internal call to
an `external` **library** harness function, invoked via `delegatecall` so it
can reach the original contract's storage through the pointer:

```cvl
function Bank.computeInterest(Bank.Vault storage v, address d, uint p)
    internal returns (uint) => VaultHarness.computeInterestHarness(v, d, p);
```

Restrictions: internal functions only (public/private included); the harness
must be an external library function among the provided contracts; return types
must match the summarized function exactly; the right-hand side must be *only*
the harness call (no surrounding expression); arguments must be a permutation
of a subset of the entry's bound parameters (no expressions, no
`calldata`-located parameters); the harness signature must exactly match the
passed parameter types (no subtyping — a `uint128` parameter requires `uint128`
in the harness). The harness body is arbitrary Solidity: it may loop, call
externally, call other (possibly summarized) internals — and it may *mutate*
storage through the pointer. Default guidance: read-only harnesses unless
mutation is the point and is argued sound. `memory` mutations in the harness do
not propagate back to the caller. The harness observes harness-encoded
calldata, not the original call's.

A rerouting summary replaces the original body wholesale with the harness body:
original side effects survive only if the harness performs them itself.

## A7. Canonical facts card

Where these facts overlap with fuller sections, both are intentional
redundancy; on any perceived tension, these govern.

1. `NONDET` leaves every storage slot of every contract exactly as it was; the
   entirety of its nondeterminism is the returned data.
2. What a `NONDET` summary returns is a fresh raw *returndata buffer*: its size
   is nondeterministic (unless `optimisticReturnsize`), and the typed value a
   caller sees is whatever the caller's own ABI decoding extracts from those
   bytes — decoding may revert on short or malformed data.
3. A call summarized by `NONDET` or a havoc summary always succeeds at the call
   boundary; any revert arises downstream in the caller's handling of the
   buffer. Deliberate revert modeling lives in exactly one place: expression
   summaries, via the `revert` statement (A5.6).
4. Summaries accept essentially arbitrary argument and return types — structs,
   arrays, user-defined types all convert. The exceptions are function pointers
   and storage pointers (mappings above all), and an entry may mention such
   types in its signature so long as the summary body leaves those parameters
   unnamed and untouched (A6.1); rerouting summaries handle the cases that need
   the pointed-to storage (A6.2).
5. A wildcard external entry applies, by default, exactly at call sites the
   Prover could not resolve (policy `UNRESOLVED`); reaching resolved sites as
   well requires writing `ALL` explicitly. Internal and exact external entries
   default to `ALL`.
6. A call written in CVL invokes real contract code. The one route by which a
   CVL-initiated call meets a summary is indirect: CVL calls a public
   function's external wrapper, and the wrapper's call into the internal
   implementation is subject to `internal`-face summaries.
7. `DISPATCHER`'s domain is non-library external calls. Library calls are
   summarized through exact, wildcard, expression, or rerouting entries.
8. Summary-application precedence (exact > wildcard > catch-all) is computed
   over entries that carry summaries; an entry declaring only
   `envfree`/`optional` is invisible to it, so a wildcard summary reaches a
   method whose exact entry is summary-less.
9. `unresolved external in C.foo()` binds to unresolved call sites textually
   inside `C.foo`'s body; unresolved sites inside functions that `C.foo` calls
   need entries scoped to those functions.
10. A rerouting summary replaces the original body wholesale with the harness
    body; original side effects survive only if the harness performs them
    itself.
11. The license for weakening a summary (havoc → `NONDET` or another view
    summary) derives from a stated fact about the real callee's code, and is independent
    of the rule's verdict; a verdict change after the weakening licenses
    nothing (B3.5).
12. A `links` entry scopes to one storage location and covers that callee's
    whole interface through it; a `DISPATCHER` entry scopes to one function
    signature and covers every call site of that signature in any provided
    contract. Calls reached through an identifiable storage slot are resolved
    with a link first; signature-level summaries then apply only to the residue
    (wildcard default policy `UNRESOLVED`, A2.3).
13. ETH value attached to a summarized call is transferred regardless of
    summary type; a summary replaces only the execution of the callee's code.
14. Constructor code is outside the reach of summarization: constructors cannot
    be summarized, and summaries — including catch-unresolved entries and
    their dispatch lists — are not applied within constructor execution.
15. In a methods-block catch-all entry, `C._` names `C` as the callee (every
    external method of `C`). In an `unresolved external in C._` scope, `C._`
    names `C` as the caller (unresolved call sites within `C`'s functions).
    The same notation denotes opposite roles in the two positions.
16. Havoc summaries havoc non-persistent ghosts along with storage; `persistent`
    ghosts survive both havoc and revert.
17. The contract named in a methods-block entry is the method's *defining*
    contract: a method `Child` inherits from `Parent` is entered as
    `Parent.foo`, and an entry written `Child.foo` does not match it.

---

# PART B — GENERATION POLICY AND WORKFLOW

## B1. Obligations of the summary-writing agent

Summaries **replace code with assumptions**. Every summary emitted is a claim
about the world; an unjustified summary can silently turn a failing proof into
a passing one. Obligations, in priority order:

1. **Soundness first.** Prefer over-approximation (spurious counterexamples
   are visible and fixable) over under-approximation (missed bugs are
   invisible).
2. **Say what was assumed.** Every summary carries a comment stating (a) what
   the summary assumes, (b) why that is believed safe for the property being
   verified, and (c) what classes of real-world behavior it excludes.
3. **Verify applicability before emitting.** A syntactically valid entry that
   never fires gives false confidence. Reason explicitly about which call
   sites the entry matches (A2) and state the expected reach alongside the
   entry.
4. **Do not invent semantics.** If the needed behavior is not in this
   document, say so rather than extrapolating. When in doubt, restate from the
   canonical facts card (A7), which governs over paraphrase.

## B2. When to summarize — and when not to

### B2.1 The governing question

A summary trades fidelity for tractability, so the decision is always relative
to a property: *is the behavior about to be erased load-bearing for this
property, and does the replacement preserve it in the sound direction?* There
is no property-independent answer to "should `f` be summarized" — and the
methods block is **spec-global**: a summary that is benign for rule A silently
applies to rule B in the same file. Re-ask the question per rule the summary
will touch.

### B2.2 When a summary is the right tool

1. *The code isn't there or isn't resolvable.* Unresolved callee or selector:
   the only choice is *which* model — declining just means accepting `AUTO`.
2. *The code is there but intractable.* Nonlinear math, hashing, deep call
   trees, timeout-driving loops. The abstraction should preserve exactly the
   facts the property needs (monotonicity, bounds, functional consistency) and
   no more.
3. *Deliberate modular decomposition.* Verify the callee against its own spec
   separately; summarize it with that spec elsewhere. The one case where a
   summary is a *paid* assumption, backed by another proof — provided the
   callee-side proof actually exists.
4. *Modeling an adversarial or open environment.* Arbitrary callbacks, unknown
   tokens: the property should hold against any possible callee behavior, and
   an over-approximating model (havoc, or the hybrid pattern's residual
   branch) expresses exactly that.
5. *Instrumentation at interface points* where hooks cannot see (calls to
   contracts outside the provided set) and observation requires interposition
   (A5.8).
6. *Genuine irrelevance* — the callee provably cannot affect the property
   (event emitters, logging). Cheap pruning; "provably" is doing the work —
   state the argument.

### B2.3 When a summary is the wrong tool

1. *The callee is the subject.* Summarizing the logic the property is about
   proves the assumption back to itself. The insidious form is partial:
   summarizing a helper that computes the very quantity the invariant
   constrains.
2. *The real code is present and tractable.* Default is real code: link the
   callee (A3) if it is unresolved, or leave it unsummarized. Every summary is
   an assumption someone must maintain, and it rots: the contract evolves, the
   summary doesn't, and nothing fails when they diverge.
3. *A faithful summary would replicate the callee.* If soundness for the
   property requires most of the callee's complexity, no tractability was
   gained and a divergence channel was added — verify the real thing, or do
   decomposition properly (B2.2 case 3, with the callee-side proof).
4. *The justification comment can't be written.* The comment requirement (B1)
   is the diagnostic: inability to articulate what the summary assumes means it
   is doing load-bearing work nobody understands.

### B2.4 Summaries of the verified contract's own surface rewrite the theorem

`DELETE` and aggressive summarization of `currentContract`'s own methods change
what "for all methods `f`" ranges over in parametric rules and what an
invariant is preserved against. The verified statement gets weaker, and the
introduction of a trust assumption or excluded function is never highlighted
to the end reviewer of the verification report — only a reviewer of the
specification itself sees it. Treat any summary touching the
contract-under-verification's own methods as an edit to the theorem statement,
to be surfaced to the user, not as a tactic. `DELETE` in particular should never
proposed on the spec author's own initiative; its legitimate occasions
are recognized, not sought.

## B3. Resolving havocs: the triage ladder

### B3.1 Run bare first

Summaries are responses to *observed* damage, not premeditated architecture.
The normal loop: run with no summaries; intervene only where **both** hold:
(1) a call was unresolved, and (2) the resulting `AUTO` havoc is on the causal
path of a rule failure. Then descend the ladder, stopping at the first rung
that closes the proof.

### B3.2 Rung 1 — resolve: `links`

Ask *why* the call is unresolved. A storage-held address of a known deployment
is resolved with a `links` entry (A3): the call resolves, real code runs, and
the only assumption added is the slot binding — normally discharged by
deployment reality; say so in the justification. When the callee is reached
through an identifiable storage slot, prefer the link over signature-level
machinery: it is the narrower claim (that slot only) with the broader per-entry
coverage (the callee's whole interface), and it composes with wildcard
summaries automatically (linked sites are resolved; default-`UNRESOLVED`
wildcards skip them). Multi-target links close a world and carry the exclusion
obligation (B4).

### B3.3 Rung 2 — dispatch over candidates

Selector-unresolved raw calls take `unresolved external ... DISPATCH [...]
default <summary>` (A4.4), with the `default` choice made deliberately —
`ASSERT_FALSE` where "everything is dispatched" should be a checked obligation
rather than an assumption. Receiver-unresolved calls with a known selector
take the hybrid CVL dispatch (B4), or `DISPATCHER` when its scene-wide
signature scoping is genuinely intended.

### B3.4 Rungs 3–4 — model: spec-backed, then axiomatized

If resolution is impossible or defeats the purpose (the code is the timeout),
model: first preference, a summary backed by a separately verified spec of the
callee (B2.2 case 3); otherwise a CVL model — ghost-mapping state (A5.4),
ghost functions for deterministic math, revert modeling where failure handling
is under test (A5.6) — preserving exactly the facts the property needs. Model
in CVL; Solidity mock contracts are a rare alternative and are outside this
agent's capabilities — when a property genuinely hinges on fallback/receive or
plain-ETH-transfer behavior (which requires a mock plus `DISPATCH`), flag for
a human rather than attempt it.

### B3.5 Rung 5 — weaken, under license

Replacing a havoc with a weaker summary (`NONDET` or another view summary) is
the **last resort**. On a side-effecting callee this erases behavior the
property may depend on, and the rule passing afterward does not license it.
The license is a stated fact about the real callee's code — "`f` is a view
function"; "`f` writes only contract X's storage, which this invariant never
reads" — established independently of the verification outcome and recorded in
the justification comment.

## B4. The open-world callee problem (flagship: the ERC20 case)

Scenario: the contract holds `IERC20 token` (or oracle, vault, callback
target) whose identity is a storage value the Prover cannot resolve. The
candidate callees split into (a) implementations deliberately provided to the
Prover and (b) arbitrary external code. Every treatment picks a point on the
open/closed-world spectrum:

1. **Leave it to `AUTO`/havoc** — fully open world, sound, usually proves
   nothing useful about token-dependent state.
2. **`DISPATCHER(true)` with curated tokens provided** — closed world. States:
   "results hold for callees drawn from {A, B, ...}". Everything outside the
   list — fee-on-transfer, rebasing, hook-reentrancy in the ERC777 style,
   missing-return tokens (unless a representative is provided) — is out of
   scope and must be listed as an exclusion in the spec's assumptions.
3. **Dispatch list with explicit default** — for selector-unresolved sites:
   ```cvl
   unresolved external in C.flashLoan(uint,address,address,bytes) =>
       DISPATCH(use_fallback=true) [
           token.approve(address, uint256),
           currentContract._
       ] default NONDET;
   ```
   The `default` choice is itself a soundness decision:
   - `default HAVOC_ECF` — open world for the residue, minus reentrancy.
   - `default NONDET` — residue assumed side-effect free (document why).
   - `default ASSERT_FALSE` — converts "everything is dispatched" from
     assumption into a checked obligation. Prefer this when feasible.
4. **Hybrid CVL dispatch (preferred middle ground)** — an expression summary
   that *manually* dispatches: known provided callees get their **real code**,
   called from CVL with a correctly constructed environment; everything else
   falls through to a **symbolic model** instead of a blunt havoc/`NONDET`:
   ```cvl
   methods {
       function _.foo(uint arg1) external with(env e)
           => foo_cvl(e, arg1, calledContract, executingContract) expect uint256;
   }

   ghost mapping(address => mapping(uint => uint)) symbolicFoo;  // state of unknown callees

   function foo_cvl(env e, uint arg1, address callee, address caller) returns uint {
       if (callee == knownImpl1) {
           env call;
           require call.msg.sender == caller;  // the contract that made the summarized call
           require call.msg.value  == 0;
           return knownImpl1.foo(call, arg1);  // real provided code
       } else if (callee == knownImpl2) {
           env call;
           require call.msg.sender == caller;
           require call.msg.value  == 0;
           return knownImpl2.foo(call, arg1);
       } else {
           // symbolic model: as strong or as weak as the property justifies —
           // ghost-backed for consistency, revert modeling (A5.6), axioms, etc.
           return symbolicFoo[callee][arg1];
       }
   }
   ```
   (`knownImpl1`/`knownImpl2` are `using` aliases for provided contracts.)
   The final branch keeps arbitrary callees in play, modeled by the residual
   expression, and the whole treatment sits in one auditable function. It
   applies on wildcard entries where the *selector* is resolved but the
   receiver is not (`DISPATCH` lists apply when the selector itself is
   unresolved). Constructing the
   inner `env` is part of the model: `msg.sender == caller` and
   `msg.value == 0` mirror a plain non-payable call; tie the inner `block.*`
   / `tx.origin` to the outer `e` when the callee is sensitive to those fields
   (time- or block-number-dependent logic) — otherwise leaving them free is
   acceptable. If a provided implementation itself makes calls matching the
   same wildcard, the summary re-enters — see the recursion bound (A5.5).
5. **Symbolic-only model** — the ghost-mapping pattern (A5.4) with no concrete
   branches: one behavioral axiom covering any number of unknown instances.
   The axiom is the documented assumption.
6. **`link`** — when the callee is reached through an identifiable storage
   slot, resolve it instead (B3.2): real code, and the only assumption is the
   slot binding (single-target) or a curated candidate set (multi-target — one
   entry covering the whole interface, far narrower than per-signature
   `DISPATCHER` entries).

Rules for this agent: (a) when the receiver is unresolved but the selector is
known, prefer the hybrid CVL dispatch (recipe 4) over `DISPATCHER(true)`
unless the closed-world assumption is explicitly acceptable to the user; (b)
when generating any closed-world construct (`DISPATCHER(true)`, optimistic
dispatch list, multi-target link), emit alongside it a natural-language
assumption statement — "verified only for callees in {…}; results do not
extend to arbitrary tokens" — and enumerate at least the standard excluded
token behaviors when tokens are involved.

## B5. Diagnosing summaries that did not apply

When a rule failure shows a havoc at a call site an entry was expected to
cover, the common causes: wrong visibility face (public duality, A2.2);
default `UNRESOLVED` policy on a wildcard while the call is resolved (A2.3); a
catch-all entry whose receiver the Prover could not pin to the named contract;
entry pattern mismatch (locations on reference-type parameters, exact
signature); an entry naming the inheriting contract instead of the defining
contract for an inherited method (A2.1); the call originating from CVL (A2.5).

## B6. Pre-emission checklist

1. **Necessity and triage** (B3): is the call actually unresolved, or actually
   a timeout source? Resolution options (`links` A3, dispatch on candidates)
   come before any behavior-erasing summary; behavior erasure is the last
   resort.
2. **Weakening license** (B3.5): if this summary is weaker than the havoc it
   replaces (`NONDET` or another view summary), cite the callee-side fact that
   licenses the weakening. A changed rule verdict is not a license.
3. **Face** (A2.2): internal or external? Public ⇒ almost always `internal`.
4. **Reach** (A2.3): given entry kind and policy defaults, which call sites
   match? Any resolved sites a default-`UNRESOLVED` wildcard will skip? Is
   that intended? State the expected reach (B1).
5. **State direction**: can the real callee mutate state the property reads
   (directly or reentrantly)? If yes, `NONDET`/view summaries are
   under-approximations — either escalate to havoc/dispatch or document the
   assumption prominently.
6. **Return direction**: does the property depend on return-value structure
   (consistency across calls, relation to arguments)? If yes, `NONDET` is too
   weak — use a ghost (consistency) or a CVL function encoding exactly the
   assumed relation.
7. **Revert behavior** (A5.6): if the real callee's reverts are load-bearing
   for the property, model them with `revert` in an expression summary, and
   choose `require` vs `revert` deliberately — they encode assumption vs
   model.
8. **World closure**: does the construct close the callee set
   (`DISPATCHER(true)`, optimistic dispatch, multi-target link)? Emit the
   exclusion list.
9. **Vacuity guards**: optimistic dispatch with unmatchable candidates;
   `--optimistic_summary_recursion`. Prefer `default ASSERT_FALSE` where
   "everything dispatched" should be checked rather than assumed.
10. **Comment** (B1): emit the assumption comment.
