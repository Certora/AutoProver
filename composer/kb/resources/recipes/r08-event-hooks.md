> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R8. Events `[CVL]`

**Trigger:** a property about emitted events — that one is emitted, with what
payload, by whom, how many times, in what order.

**Formula:** event hooks. Two forms: unqualified (matches by event signature)
and contract-qualified (pins the declaring contract; required when several
contracts declare same-named events with different payloads). Inside the body,
parameters bind by name — `indexed` markers mirroring the Solidity declaration
— `executingContract` is the emitter, and the body may update ghosts, `assert`,
and read contract storage. Struct payloads and dynamic arrays decode
(`payload.sender`, `path.length`, `path[i]`).

```cvl
ghost mathint transferCount;
ghost address lastFrom;

hook event Transferred(address indexed from, address indexed to, uint amount) {
    transferCount = transferCount + 1;
    lastFrom = from;
}

hook event Counter.OrderPlaced(Counter.Order payload) {
    assert payload.sender == lastFrom;
}

rule transferEmits(env e, address to, uint amount) {
    transferCount = 0;
    transfer(e, to, amount);
    assert transferCount == 1 && lastFrom == e.msg.sender;
}
```

Semantics and limits:

- Matching and decoding are best-effort with **no type checking**: a
  parameter list that does not exactly mirror the Solidity declaration
  misbehaves silently. Copy the declaration, including `indexed` placement.
- Anonymous events are not supported.
- Nested dynamic payload types decode, at significant verification-time cost.
- Rollback: hooks fire at emit time; plain ghost updates roll back with a
  reverting frame — coinciding exactly with the EVM's erasure of logs on
  revert — so plain ghosts model *events of the surviving execution*, while
  `persistent` ghosts model *emit was attempted*. Choose per property.
- Composition with summaries: a summary replaces the callee's code execution,
  so no `LOG` executes and no event hook fires through a summarized, havocked,
  or `AUTO`-handled call. When a summarized callee's events matter, the summary
  body models them by updating the same ghosts the hook maintains.
