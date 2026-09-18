> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K1. Satisfy rules, and the one they must not replace `[RULE]`

**Trigger:** you want to show that an execution *exists* — the setup runs at all, a branch is
reachable, a post-state is attainable — rather than that every execution is correct.

**Formula:** `cvlr_satisfy!(cond)`. The prover reports the rule verified when it finds at least one
execution satisfying `cond`, so it is an existence claim, and the opposite quantifier from
`cvlr_assert!`.

Two uses earn their place:

```rust
// Reachability: does this setup execute at all, or has something pruned it away?
#[rule]
pub fn sanity_deposit_reachable() {
    let mut ix = /* accounts, as an assert-rule would build them */;
    crate::vault_program::deposit(ctx, amount).unwrap();
    cvlr_satisfy!(true);
}

// Witness: show that the interesting branch is attainable, not just the trivial one.
#[rule]
pub fn witness_deposit_increases_balance() {
    let before = /* ... */;
    crate::vault_program::deposit(ctx, amount).unwrap();
    cvlr_satisfy!(balance_of(&ix) > before);
}
```

**The line this recipe exists to draw.** An *acceptance* property — "any signer may deposit", "the
handler accepts a zero amount" — is a claim about every admissible input, and
`handler(..).unwrap(); cvlr_satisfy!(true);` does not state it. It passes as soon as one execution
reaches the end, whether or not the property holds for the rest, and a green verdict on it is a
verification of nothing. The judge rejects exactly this shape. If a property needs an assertion,
write the assertion; if the assertion cannot be written because the failure path is unanalyzable,
`record_skip` naming that reason.

So: a satisfy rule is a good sanity check and a good witness, and never a property's rule. Do not
publish one as the rule that drives a property.

**Failure symptom:** a satisfy rule that comes back *violated* means no execution satisfies the
condition. For `cvlr_satisfy!(true)` that is the setup being unreachable — usually an assumption
that contradicts the state you built, which is worth finding before you trust any assert-rule over
the same setup.

**What this rests on:** the sanctioned uses are attested across 4 projects and 12 occurrences in the
surveyed corpus; the prohibition is the backend's own, and is what the feedback judge checks.
