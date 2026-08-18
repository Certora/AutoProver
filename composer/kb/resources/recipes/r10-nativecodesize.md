> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R10. EOA vs contract `[CVL]`

**Trigger:** a property distinguishing externally-owned accounts from
contracts.

**Formula:** `nativeCodesize[addr] == 0` characterizes an EOA.

NB: Within a `preserved constructor` block for a contract `C`, the prover
constrains `nativeCodesize[C] > 0`. This is a known deviation from faithful on-chain
EVM semantics.
