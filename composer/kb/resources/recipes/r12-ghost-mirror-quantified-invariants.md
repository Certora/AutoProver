> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R12. The mirror pattern `[CVL]`

**Trigger:** an invariant needs to quantify over the contents of an array,
mapping, or set — and quantifier bodies can contain no storage accesses, no
contract calls, and no `require_*`/`assert_*` casts.

For *unquantified* reads of contract state, direct storage access is preferred
and no mirroring is needed; this pattern is necessary primarily when accessing
storage cannot be performed under a quantifier.

**Formula:** mirror the data structure into ghosts via storage hooks, and
quantify over the ghosts. Hooks keep ghost and storage synchronized in both
directions: `Sload` hooks `require` equality (importing storage knowledge into
the ghost), `Sstore` hooks assign (propagating updates).

```cvl
ghost uint256 ghostLength;
ghost mapping(uint256 => bytes32) ghostValues;
ghost mapping(bytes32 => uint256) ghostIndexes;

hook Sload uint256 length currentContract.set._inner._values.length {
    require ghostLength == length;
}
hook Sstore currentContract.set._inner._values.length uint256 newLength {
    ghostLength = newLength;
}
hook Sload bytes32 value currentContract.set._inner._values[INDEX uint256 index] {
    require ghostValues[index] == value;
}
hook Sstore currentContract.set._inner._values[INDEX uint256 index] bytes32 newValue {
    ghostValues[index] = newValue;
}
// ... same pair for _indexes ...

invariant setInvariant()
    (forall uint256 i. 0 <= i && i < ghostLength
        => to_mathint(ghostIndexes[ghostValues[i]]) == i + 1)
 && (forall bytes32 v. ghostIndexes[v] == 0
        || (ghostValues[ghostIndexes[v] - 1] == v
            && ghostIndexes[v] >= 1 && ghostIndexes[v] <= ghostLength));
```

The pattern generalizes to linked lists and any storage-resident structure:
one ghost per quantified field, one hook pair per ghost.
