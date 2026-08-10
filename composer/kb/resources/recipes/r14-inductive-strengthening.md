> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R14. Invariant fails from an unreachable start state `[CVL]`

**Trigger:** an invariant is violated by a counterexample whose *initial*
state the contract can never actually reach.

**Formula:** the invariant is not inductive; strengthen it until it is. The
Prover starts from an arbitrary state satisfying only the invariant itself, so
the invariant must exclude the unreachable configurations it implicitly relies
on excluding — typically by conjoining the reachable-configuration facts
(often a disjunction of legal states) into the invariant, or by
`requireInvariant`-ing an auxiliary invariant in a `preserved` block.
