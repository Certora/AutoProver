> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R3. Transient-storage locks `[CVL]`

**Trigger:** the contract implements locks/mutexes/reentrancy guards over
transient storage (`tstore`/`tload`).

**Formula:** `ALL_TSTORE`/`ALL_TLOAD` hooks maintaining a persistent ghost
mirror of the lock slot. The discipline: act only on the known slot in the
verified contract's frame; havoc the mirror for any other transient write, so
the mirror never claims knowledge it does not have.

```cvl
definition isLockedCVL(uint256 status) returns bool = status == 1;
definition slot() returns uint = 0xa2e3...;   // the lock's transient slot

persistent ghost bool contract_lock_status {
    init_state axiom contract_lock_status == false;
}

hook ALL_TSTORE(uint loc, uint v) {
    if (loc == slot() && executingContract == currentContract) {
        contract_lock_status = isLockedCVL(v);
    } else {
        havoc contract_lock_status;
    }
}

hook ALL_TLOAD(uint loc) uint v {
    if (loc == slot() && executingContract == currentContract) {
        require contract_lock_status == isLockedCVL(v);
    } else {
        havoc contract_lock_status;
    }
}
```
