> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K5. `Cannot overflow` on an inferred pointer `[RULE]` `[SKIP]`

**Trigger:** a rule stops on `Cannot overflow: inferred pointer` or `Cannot overflow: computing TAC
map index` before it reaches its property.

**What it is.** A check the Prover adds on its own, not one your rule asserts. The conf's
`-solanaTACSoundSignedMath` is what adds it: the prover asserts that every value it takes for a
pointer stays below `2^64`, and it could not show that for an expression it treats as one. That is
almost always because a value the rule reads from an account (a balance, an amount) is free to sit
just below `u64::MAX`. The verdict says nothing about the program.

**Formula:** bound the account-data values on that rule's path once, `cvlr_assume!(x < 1 << 63)`,
and record each bound as an assumption.

**If it comes back with the bound in place**, the bound is not what the rule needs, and no rewording
of the rule reliably gets past it. Which expressions the Prover takes for pointers depends on how the
code is laid out, not on what it means: two rules that mean the same thing, one verifying and one
stopping, can differ only in statement order. State the property against the program's own logic
below the handler, a function that takes no `AccountInfo`, if the program has one; otherwise skip the
property and give the stop as the reason.

**A property about the top of the range cannot take the bound.** A `checked_add` that must fail at
`u64::MAX` loses exactly the paths it is about under that bound. Skip it and say so. For the same
reason, do not bound account data ahead of time to prevent this stop.
