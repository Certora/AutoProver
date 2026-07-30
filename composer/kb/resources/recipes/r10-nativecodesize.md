> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R10. EOA vs contract `[CVL]`

**Trigger:** a property distinguishing externally-owned accounts from
contracts.

**Formula:** `nativeCodesize[addr] == 0` characterizes an EOA.
