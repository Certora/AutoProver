> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R2. Raw-slot unstructured storage `[CONF]` + `[EDIT]`

**Trigger:** storage reached through `assembly { x.slot := <constant> }` with
no erc7201 annotation — or an erc7201 namespace outside R1's recognized
grammar.

**Formula:** a purely declarative harness contract mapping each slot constant
to a typed field, plus the conf setting wiring it to the target contract:

```solidity
import {Example} from "./Example.sol";

contract Harness {
    /** @custom:certoralink 0x8580001204012435 */
    Example.Book b1;

    /** @custom:certoralink 0x1234123412341234 */
    Example.Book b2;
}
```

```jsonc
"storage_extension_harnesses": [ "Example=Harness" ]
```

CVL then accesses the storage through the harness field names
(`currentContract.b1.field1...`), with hooks and direct storage access
supported as for declared storage. The harness contains field declarations
only — no functions, no behavior — so it satisfies the edit obligations
trivially (the diff adds a file and touches nothing else; no contract
semantics are affected).

**Manual:** *Storage Layout Annotations* (user-provided harnesses).
