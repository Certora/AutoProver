> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R6. Immutables, including private ones `[CVL]`

**Trigger:** a property about an immutable value.

**Formula:** direct storage access by name — `currentContract.OWNER`,
`currentContract.MY_UINT` — or binding via linking when the immutable holds a
contract address. Works for private immutables as well. Caveat: the immutable
must be referenced somewhere in the compiled code to exist for the Prover.

```cvl
rule ownerNeverChanges(env e, method f, calldataarg args) {
    address before = currentContract.OWNER;
    f(e, args);
    assert before == currentContract.OWNER;
}
```
