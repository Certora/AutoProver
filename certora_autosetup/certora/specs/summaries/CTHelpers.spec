// Curated summary for the Gnosis Conditional Tokens helper library (CTHelpers).
//
// CTHelpers.getCollectionId derives a collection id as a compressed alt_bn128 curve point: it hashes
// (conditionId, indexSet) to a curve point via a modular square root (the private CTHelpers.sqrt, a
// large unrolled mulmod add-chain computing x^((P+1)/4) mod P) and, when parentCollectionId != 0,
// EC-adds the parent point. The function is a deterministic function of its inputs, so a deterministic
// ghost summary is a valid over-approximation. Summarizing it also keeps the modular-sqrt add-chain
// out of the prover, which otherwise makes loop-summarization fold a huge nested mulmod/addmod
// expression and exhaust memory.
//
// Over-approximation note: the ghost is an arbitrary deterministic function constrained only by the
// injectivity axiom below. It does not model the homomorphic composition of nested collections (the
// parentCollectionId point addition), so it cannot prove properties that assert how collections
// compose under that composition; those must keep the real implementation. The injectivity axiom
// states that distinct (parentCollectionId, conditionId, indexSet) inputs yield distinct ids, which
// holds for the real collision-resistant derivation, so distinct collections remain distinct.
//
// Injectivity is encoded via left-inverse ghosts rather than a 6-variable forall: asserting that each
// input component is recoverable from the id is equivalent to injectivity (equal ids force equal
// inputs through the inverses) but only quantifies 3 variables and applies ghostCollectionId once, so
// the solver instantiates it linearly instead of over every pair of ids.

persistent ghost ghostCollectionIdInv1(bytes32) returns bytes32;
persistent ghost ghostCollectionIdInv2(bytes32) returns bytes32;
persistent ghost ghostCollectionIdInv3(bytes32) returns uint256;

persistent ghost ghostCollectionId(bytes32, bytes32, uint256) returns bytes32 {
    axiom forall bytes32 p1. forall bytes32 c1. forall uint256 i1.
          ghostCollectionIdInv1(ghostCollectionId(p1, c1, i1)) == p1 &&
          ghostCollectionIdInv2(ghostCollectionId(p1, c1, i1)) == c1 &&
          ghostCollectionIdInv3(ghostCollectionId(p1, c1, i1)) == i1;
}

methods {
    function CTHelpers.getCollectionId(bytes32 p, bytes32 c, uint256 i) internal returns (bytes32) => ghostCollectionId(p, c, i);
}
