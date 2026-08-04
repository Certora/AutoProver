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
}
