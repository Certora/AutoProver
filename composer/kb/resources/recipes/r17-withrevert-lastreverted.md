> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R17. Revert-condition properties `[CVL]`

**Trigger:** the property is about *when a function reverts* — that it
reverts exactly under stated conditions.

**Formula:** call with `@withrevert` (reverting paths are otherwise pruned)
and assert an iff over `lastReverted`:

```cvl
rule transferRevertConditions(env e, address to, uint256 amount) {
    uint256 balance = balanceOf(e.msg.sender);
    transfer@withrevert(e, to, amount);
    assert lastReverted <=> (balance < amount || e.msg.value != 0 || ...);
}
```

`lastReverted` refers to the most recent `@withrevert` call; reading it after
a call made without `@withrevert` is a checker error.
