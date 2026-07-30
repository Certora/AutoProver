> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R15. `preserved` blocks `[CVL]`

**Trigger:** an invariant needs different or additional assumptions for
specific functions, or environment access during preservation checks.

**Formula:**

```cvl
invariant inv(...)
    ...
{
    preserved {  ... }                                  // for every function
    preserved transfer(address to, uint a) with (env e) { ... }  // per function
    preserved onTransactionBoundary with (env e) { ... }         // R4
    preserved constructor() { ... }                     // constructor base case
}
```

The generic (unnamed) `preserved` block applies only to the induction step; an
invariant failing on the base case takes its assumptions from a dedicated
`preserved constructor()` block.

A `preserved` block adds prestate requirements *on top of* the invariant being
proved: the check is `assume I; m(); assert I`, with the block's contents
inserted after the `assume I`. The invariant itself is always assumed —
requiring `I` manually in its own preserved block is redundant.
