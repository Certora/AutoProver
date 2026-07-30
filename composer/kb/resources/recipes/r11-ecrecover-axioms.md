> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R11. `ecrecover` `[CVL]`

**Trigger:** the contract validates signatures with `ecrecover`.

**Formula:** `ecrecover` is available in CVL as an uninterpreted function —
determinism is free. The remaining cryptographic facts are supplied as an
axiom pack invoked at the start of each rule that needs them:

```cvl
function ecrecoverAxioms() {
    // zero hash recovers nothing
    require (forall uint8 v. forall bytes32 r. forall bytes32 s.
        ecrecover(to_bytes32(0), v, r, s) == 0);
    // a signature valid for one hash is valid for no other
    require (forall uint8 v. forall bytes32 r. forall bytes32 s. forall bytes32 h1. forall bytes32 h2.
        h1 != h2 => ecrecover(h1, v, r, s) != 0 => ecrecover(h2, v, r, s) == 0);
    // sensitivity to r and to s
    require (forall bytes32 h. forall uint8 v. forall bytes32 s. forall bytes32 r1. forall bytes32 r2.
        r1 != r2 => ecrecover(h, v, r1, s) != 0 => ecrecover(h, v, r2, s) == 0);
    require (forall bytes32 h. forall uint8 v. forall bytes32 r. forall bytes32 s1. forall bytes32 s2.
        s1 != s2 => ecrecover(h, v, r, s1) != 0 => ecrecover(h, v, r, s2) == 0);
}

rule signatureUnique {
    ecrecoverAxioms();
    ...
}
```

Each axiom is an assumption about the cryptography; the pack is the standard,
stated form of that assumption.

---

# 4. Quantified invariants
