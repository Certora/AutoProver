> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R7. Extension contracts (fallback → delegatecall) `[CONF]`

**Trigger:** a contract whose `fallback()` delegatecalls into one or more
"extension" contracts holding parts of its logic:

```solidity
fallback() external payable {
    (bool ok, ) = extensionAddress.delegatecall(msg.data);
    require(ok);
}
```

Calls into the extended contract's extension functions are unresolved by
default, and the delegatecall havocs the caller's state.

**Formula:** the conf setting declaring the extension relationship, which lets
the Prover route those calls to the extension code executing in the base's
storage context:

```jsonc
"contract_extensions": {
    "Base1": [
        { "extension": "Extension1", "exclude": [] },
        { "extension": "Extension2", "exclude": ["someFunction"] }
    ]
}
```

`exclude` lists extension functions to leave out of the routing.

---

# 3. EVM-level facts
