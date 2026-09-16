// Curated summaries for the Aave fixed-point math libraries (WadRayMath, PercentageMath,
// MathUtils). All three compute x*y in a single 256-bit word and revert on intermediate
// overflow, so they use the 256-bit-intermediate helpers from Math.spec
// (mulDivDownSummary256 / mulDivUpSummary256), not the full-precision ones. Each op is a
// floor/ceil of x*y/scale (WAD=1e18, RAY=1e27, PERCENTAGE_FACTOR=1e4); e.g.
// rayMulDown(a,b) = mulDivDownSummary256(a, b, 1e27).
import "../Math.spec";

methods {
    // ---- WadRayMath (WAD = 1e18, RAY = 1e27) ----
    function WadRayMath.wadMulDown(uint256 a, uint256 b) internal returns (uint256) => mulDivDownSummary256(a, b, 10^18);
    function WadRayMath.wadMulUp(uint256 a, uint256 b)   internal returns (uint256) => mulDivUpSummary256(a, b, 10^18);
    function WadRayMath.wadDivDown(uint256 a, uint256 b) internal returns (uint256) => mulDivDownSummary256(a, 10^18, b);
    function WadRayMath.wadDivUp(uint256 a, uint256 b)   internal returns (uint256) => mulDivUpSummary256(a, 10^18, b);
    function WadRayMath.rayMulDown(uint256 a, uint256 b) internal returns (uint256) => mulDivDownSummary256(a, b, 10^27);
    function WadRayMath.rayMulUp(uint256 a, uint256 b)   internal returns (uint256) => mulDivUpSummary256(a, b, 10^27);
    function WadRayMath.rayDivDown(uint256 a, uint256 b) internal returns (uint256) => mulDivDownSummary256(a, 10^27, b);
    function WadRayMath.rayDivUp(uint256 a, uint256 b)   internal returns (uint256) => mulDivUpSummary256(a, 10^27, b);
    function WadRayMath.toWad(uint256 a)       internal returns (uint256) => mulDivDownSummary256(a, 10^18, 1);
    function WadRayMath.toRay(uint256 a)       internal returns (uint256) => mulDivDownSummary256(a, 10^27, 1);
    function WadRayMath.fromWadDown(uint256 a) internal returns (uint256) => mulDivDownSummary256(a, 1, 10^18);
    function WadRayMath.fromRayUp(uint256 a)   internal returns (uint256) => mulDivUpSummary256(a, 1, 10^27);

    // ---- PercentageMath (PERCENTAGE_FACTOR = 1e4) ----
    function PercentageMath.percentMulDown(uint256 value, uint256 percentage) internal returns (uint256) => mulDivDownSummary256(value, percentage, 10^4);
    function PercentageMath.percentMulUp(uint256 value, uint256 percentage)   internal returns (uint256) => mulDivUpSummary256(value, percentage, 10^4);
    function PercentageMath.percentDivDown(uint256 value, uint256 percentage) internal returns (uint256) => mulDivDownSummary256(value, 10^4, percentage);
    function PercentageMath.percentDivUp(uint256 value, uint256 percentage)   internal returns (uint256) => mulDivUpSummary256(value, 10^4, percentage);
    function PercentageMath.fromBpsDown(uint256 value) internal returns (uint256) => mulDivDownSummary256(value, 1, 10^4);

    // ---- MathUtils (generic 256-bit mulDiv / divUp) ----
    function MathUtils.mulDivDown(uint256 a, uint256 b, uint256 c) internal returns (uint256) => mulDivDownSummary256(a, b, c);
    function MathUtils.mulDivUp(uint256 a, uint256 b, uint256 c)   internal returns (uint256) => mulDivUpSummary256(a, b, c);
    function MathUtils.divUp(uint256 a, uint256 b) internal returns (uint256) => mulDivUpSummary256(a, 1, b);

    // ---- MathUtils.uncheckedExp: a ** b, in practice always 10 ** decimals ----
    function MathUtils.uncheckedExp(uint256 a, uint256 b) internal returns (uint256) => limitedExp(a, b);
}

// uncheckedExp(a, b) = a ** b. Every call site passes base 10 and an asset's decimals, so it is the
// 10 ** decimals decimal-scaling factor, modeled as an exact powers-of-ten table lookup (a symbolic
// a ** b is prover-hostile). limitedExp asserts base 10 and an exponent within the table (0..18).
ghost mapping(uint256 => uint256) expCVL {
    axiom expCVL[0] == 1;
    axiom expCVL[1] == 10;
    axiom expCVL[2] == 100;
    axiom expCVL[3] == 1000;
    axiom expCVL[4] == 10000;
    axiom expCVL[5] == 100000;
    axiom expCVL[6] == 1000000;
    axiom expCVL[7] == 10000000;
    axiom expCVL[8] == 100000000;
    axiom expCVL[9] == 1000000000;
    axiom expCVL[10] == 10000000000;
    axiom expCVL[11] == 100000000000;
    axiom expCVL[12] == 1000000000000;
    axiom expCVL[13] == 10000000000000;
    axiom expCVL[14] == 100000000000000;
    axiom expCVL[15] == 1000000000000000;
    axiom expCVL[16] == 10000000000000000;
    axiom expCVL[17] == 100000000000000000;
    axiom expCVL[18] == 1000000000000000000;
}

function limitedExp(uint256 a, uint256 b) returns (uint256) {
    assert a == 10;
    // expCVL covers exponents 0..18 (standard ERC-20 decimal ranges); extend it to cover a larger one.
    assert b <= 18;
    return expCVL[b];
}
