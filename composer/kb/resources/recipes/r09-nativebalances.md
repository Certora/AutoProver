> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R9. ETH balances `[CVL]`

**Trigger:** a property about native ETH.

**Formula:** `nativeBalances[addr]`. Entry-time subtlety: at the entry to a
`payable` function, `address(this).balance` (and `nativeBalances[currentContract]`)
already includes `msg.value`.
