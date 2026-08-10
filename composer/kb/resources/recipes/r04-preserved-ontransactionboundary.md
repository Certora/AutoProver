> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R4. Invariants over transient state `[CVL]`

**Trigger:** an invariant mentions transient storage. Transient storage
resets at the transaction boundary, which is an extra state transition the
invariant must survive.

**Formula:** a `preserved onTransactionBoundary` block supplying what holds at
the boundary:

```cvl
invariant deltaEqualsStorage(env e)
    getDelta(e) == currentContract.storageValue
{
    preserved onTransactionBoundary with (env e2) {
        requireInvariant isUnlocked(e2);
        requireInvariant deltaZeroWhenUnlocked(e2);
    }
}
```

Functions guarded to run only mid-transaction (e.g. under a lock) are excluded
from the invariant's `filtered` clause rather than fought.
