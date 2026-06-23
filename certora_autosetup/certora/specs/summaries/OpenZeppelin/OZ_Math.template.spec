import "../Math.spec";

methods {
    function Math.mulDiv(uint256 x, uint256 y, uint256 denominator) internal returns (uint256) => mulDivDownSummary(x,y,denominator);
    $COMMENT_IF_NO_ROUNDING$ function Math.mulDiv(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) internal returns (uint256) => mulDivDirectionalSummary(x, y, denominator, rounding);
    function Math.average(uint256 a, uint256 b) internal returns (uint256) => averageSummary(a,b);
    function Math.sqrt(uint256 x) internal returns (uint256) => sqrtSummaryDown(x);
}

$COMMENT_BLOCK_START_IF_NO_ROUNDING$
function mulDivDirectionalSummary(uint256 x, uint256 y, uint256 denominator, Math.Rounding rounding) returns uint256 {
    // OZ v<5 used `Up`, v>=5 uses `Ceil`.
    if (rounding == Math.Rounding.$UINT_ROUND_UP$) {
        return mulDivUpSummary(x, y, denominator);
    } else {
        return mulDivDownSummary(x, y, denominator);
    }
}
$COMMENT_BLOCK_END_IF_NO_ROUNDING$
