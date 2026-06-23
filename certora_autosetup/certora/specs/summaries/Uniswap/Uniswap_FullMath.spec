import "../Math.spec";

methods {
    function FullMath.mulDiv(uint256 a, uint256 b, uint256 denominator) internal returns (uint256) => mulDivDownSummary(a,b,denominator);
    function FullMath.mulDivRoundingUp(uint256 a, uint256 b, uint256 denominator) internal returns (uint256) => mulDivUpSummary(a,b,denominator);
}
