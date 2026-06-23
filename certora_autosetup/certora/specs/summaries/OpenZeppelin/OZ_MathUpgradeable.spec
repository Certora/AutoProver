import "../Math.spec";

methods {
    function MathUpgradeable.mulDiv(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivDownSummary(x,y,denominator);
    function MathUpgradeable.mulDiv(uint256 x, uint256 y, uint256 denominator, MathUpgradeable.Rounding rounding) internal returns (uint256) => mathUpgradeableMulDivDirectionalSummary(x, y, denominator, rounding);
    function MathUpgradeable.average(uint256 a, uint256 b) internal returns (uint256) => averageSummary(a,b);
}


function mathUpgradeableMulDivDirectionalSummary(uint256 x, uint256 y, uint256 denominator, MathUpgradeable.Rounding rounding) returns uint256 {
    if (rounding == MathUpgradeable.Rounding.Up) {
        return mulDivUpSummary(x, y, denominator);
    } else {
        return mulDivDownSummary(x, y, denominator);
    }
}
