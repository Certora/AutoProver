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

// CTHelpers.getPositionId derives a position id as a keccak256 hash of its two arguments (a token
// address and a collection id). The function is a deterministic function of its inputs, so a deterministic
// ghost summary is a valid over-approximation.
//
// Injectivity note: under the prover's hashing abstraction the hash is not injective, so distinct
// (token, collection id) pairs can collapse onto one id. Making the ghost injective restores
// the collision-resistance the real keccak256 has, which distinct positions rely on: a caller that
// derives a YES/NO position pair from one collection id and moves both in a single batch transfer needs
// the two ids to differ, or a modelled ledger moves the same balance twice and the second move reverts
// for insufficient balance — a revert with no real counterpart.
//
// As with getCollectionId, injectivity is encoded via left-inverse ghosts rather than a pairwise forall:
// each input component is recoverable from the id (equal ids force equal inputs through the inverses),
// which quantifies only 2 variables and applies ghostPositionId once, so the solver instantiates it
// linearly instead of over every pair of ids.

persistent ghost ghostPositionIdInvToken(uint256) returns address;
persistent ghost ghostPositionIdInvColl(uint256) returns bytes32;

persistent ghost ghostPositionId(address, bytes32) returns uint256 {
    axiom forall address t1. forall bytes32 c1.
          ghostPositionIdInvToken(ghostPositionId(t1, c1)) == t1 &&
          ghostPositionIdInvColl(ghostPositionId(t1, c1)) == c1;
}

methods {
    function CTHelpers.getCollectionId(bytes32 p, bytes32 c, uint256 i) internal returns (bytes32) => ghostCollectionId(p, c, i);
    function CTHelpers.getPositionId(address t, bytes32 c) internal returns (uint256) => ghostPositionId(t, c);
}
