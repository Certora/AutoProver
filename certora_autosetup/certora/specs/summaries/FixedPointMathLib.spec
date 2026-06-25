import "Math.spec";

methods {
    // Solmate naming (rounds down = "Down" suffix)
    function FixedPointMathLib.mulDivDown(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivDownSummary(x, y, denominator);
    function FixedPointMathLib.mulDivUp(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivUpSummary(x, y, denominator);
    function FixedPointMathLib.mulWadDown(uint256 x, uint256 y) internal returns (uint256) => mulWadDownSummary(x, y);
    function FixedPointMathLib.mulWadUp(uint256 x, uint256 y) internal returns (uint256) => mulWadUpSummary(x, y);
    function FixedPointMathLib.divWadDown(uint256 x, uint256 y) internal returns (uint256) => divWadDownSummary(x, y);
    function FixedPointMathLib.divWadUp(uint256 x, uint256 y) internal returns (uint256) => divWadUpSummary(x, y);
    function FixedPointMathLib.sqrt(uint256 x) internal returns (uint256) => sqrtSummaryDown(x);

    // Solady naming (rounds down = no suffix)
    function FixedPointMathLib.mulDiv(uint256 x, uint256 y, uint256 d) internal returns (uint256) => mulDivDownSummary(x, y, d);
    function FixedPointMathLib.fullMulDiv(uint256 x, uint256 y, uint256 d) internal returns (uint256) => mulDivDownSummary(x, y, d);
    function FixedPointMathLib.fullMulDivUp(uint256 x, uint256 y, uint256 d) internal returns (uint256) => mulDivUpSummary(x, y, d);
    function FixedPointMathLib.mulWad(uint256 x, uint256 y) internal returns (uint256) => mulWadDownSummary(x, y);
    function FixedPointMathLib.divWad(uint256 x, uint256 y) internal returns (uint256) => divWadDownSummary(x, y);
}
