> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K2. Log the value an assertion reads when it is an enum or a bitfield `[RULE]`

**Trigger:** the value an assertion inspects is a status field, an enum discriminant, or anything
derived from a chain of comparisons — `is_open!(s) || is_closed!(s) || ...`. The rule then fails
with a counterexample whose value looks arbitrary rather than wrong.

**Formula:** `clog!` the exact expression the assertion reads, immediately before the assertion.

```rust
crate::pool_program::close_if_stale(ctx, stale).unwrap();

// Without this, the comparison chain can be folded into a bitmask test, and the
// solver then treats the result as a fresh unconstrained value.
clog!(matches!(pool.status, Status::Closed));

cvlr_assert!(matches!(pool.status, Status::Closed) == stale);
```

**Why it is not only about legibility.** Logging every value an assertion depends on is already the
standing habit, and this is a second reason for it: the log gives the value a use site, which
defeats a compiler optimization that would otherwise fold the discriminant comparisons into a
bitmask operation and lose the derivation the solver needs. The rule then fails on a value the
program could never produce.

**The part that is easy to get wrong:** log the expression the assertion reads, not a copy of it and
not a differently-computed equivalent. A `clog!` on the wrong value is a no-op that looks like the
fix, and the rule keeps racing the optimizer.

**What this rests on:** 4 clients, 4 projects, 11 occurrences across solana and soroban, observed on
cvlr 0.4.0 through 0.6.1. Why the log defeats the fold is not documented upstream, and whether it is
needed for every such assertion or only at some optimization levels is not established — so treat a
counterexample that survives the log as a different problem, not as a reason to add more logs.
