> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R20. Calling internal functions from CVL `[CVL]`

**Trigger:** a rule needs an internal function's value.

**Formula:** internal functions are callable with the usual syntax
(`anInternalFunction(e)`), with restrictions: `envfree` is not available, and
storage-located parameters or return values are rejected at compile time.

The governing constraint: the call is modeled as a fresh EVM call frame, and
**side effects of the invoked internal function are erased**. The construct is
therefore for effect-free computation only — reading a value, evaluating a
predicate. Behavior that mutates state is exercised through the contract's
external surface or modeled explicitly; an internal call from CVL is never a
way to *perform* an effect.

---
