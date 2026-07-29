// EIP-712 typed-data hashing (solady `_hashTypedData`, OpenZeppelin `_hashTypedDataV4`).
// The digest is a keccak over the domain separator and the struct hash, whose assembly
// defeats the points-to analysis. It is modeled as a deterministic, injective
// uninterpreted function of the struct hash: sound, keeps equal struct hashes mapped to
// equal digests, and distinct struct hashes to distinct digests (collision-freedom,
// matching the optimistic-hashing assumption).
//
// Injectivity is encoded via a left-inverse ghost rather than a two-variable forall:
// recovering the struct hash from the digest is equivalent to injectivity (equal digests
// force equal struct hashes through the inverse) but quantifies one variable and applies
// the hash once, so the solver instantiates it linearly instead of over every pair.

persistent ghost cvlHashTypedDataInv(bytes32) returns bytes32;

persistent ghost cvlHashTypedData(bytes32) returns bytes32 {
    axiom forall bytes32 s. cvlHashTypedDataInv(cvlHashTypedData(s)) == s;
}

methods {
    function EIP712._hashTypedData(bytes32 structHash) internal returns (bytes32) => cvlHashTypedData(structHash);
    function EIP712._hashTypedDataV4(bytes32 structHash) internal returns (bytes32) => cvlHashTypedData(structHash);
}
