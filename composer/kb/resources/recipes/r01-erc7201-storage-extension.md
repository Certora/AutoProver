> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R1. ERC-7201 namespaced storage `[CONF]`

**Trigger:** the contract declares structs annotated
`/** @custom:storage-location erc7201:<namespace> */` and reaches them through
getters of the form:

```solidity
function _getMainStorage() private pure returns (MainStorage storage $) {
    assembly { $.slot := MAIN_STORAGE_LOCATION }
}
```

**Formula:** the conf setting `"storage_extension_annotation": true`. The
Prover scans for the annotations and generates the storage layout; the
namespaced struct then appears in CVL as a field of the contract named
`ext_` + the namespace with `.` replaced by `_`:

```cvl
// namespace test.book1 =>
currentContract.ext_test_book1.field1.words[0]
```

Hooks and direct storage access are both fully supported on `ext_` paths — the
namespaced struct behaves exactly like declared storage. Typed paths on the
extension struct are the tool for namespaced storage; hooks and access go
through them, never through raw slot numbers.

**Failure symptom:** the setting is enabled but no `ext_` path materializes.
Recognized namespaces match `[a-zA-Z.0-9]+`; a namespace containing any other
character (hyphens included) is silently not picked up. Fallback: R2.

**Manual:** *Storage Layout Annotations* (CVL section).
