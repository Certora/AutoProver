> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R19. Calling functions on `address` values `[CVL]`

**Trigger:** a rule holds an `address`-typed value and needs to call a
function on it.

**Formula:** `x.foo(e, args)` and `x.foo(e, v1, ...)` are both legal, with
different dispatch:

- With a `calldataarg`: dispatches over every provided contract that has a
  function named `foo`.
- With typed arguments: dispatches over every method in any provided contract
  whose name and declared parameter types match.

The dispatch is a chain over `x == <ProvidedContract>.address` whose final
branch is `assume false`: **the address is assumed to be one of the provided
contracts.** This is a closed-world assumption; any rule using address-calls
states it.
