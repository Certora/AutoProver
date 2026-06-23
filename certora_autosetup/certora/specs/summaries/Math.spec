function mulDivDownSummary(uint256 x, uint256 y, uint256 denominator) returns uint256 {
    mathint result;
    if (denominator == 0) revert();
    result = x * y / denominator;
    if (result >= 2^256) revert();
    return assert_uint256(result);
}

function mulDivUpSummary(uint256 x, uint256 y, uint256 denominator) returns uint256 {
    mathint result;
    if (denominator == 0) revert();
    result = (x * y + denominator - 1) / denominator;
    if (result >= 2^256) revert();
    return assert_uint256(result);
}

function averageSummary(uint256 a, uint256 b) returns uint256 {
    return require_uint256((a+b)/2);
}

// Exact (real) square root: the result squared equals the argument. This is the
// strongest abstraction and keeps the constraint simple for the solver (no Babylonian
// loop to unroll), but for arguments that are not perfect squares no such `result`
// exists, so the `require` prunes that path (potential vacuity). Use for invariant
// reasoning where sqrt is treated as exact (e.g. constant-product AMM invariants).
function sqrtSummaryPrecise(uint256 x) returns uint256 {
    mathint result;
    require result >= 0 && result * result == x;
    return assert_uint256(result);
}

// Floor (integer) square root: result == floor(sqrt(x)), matching Solidity's integer
// sqrt. Sound for every argument (always has a solution), at the cost of an extra
// multiplication / strict upper bound for the solver.
function sqrtSummaryDown(uint256 x) returns uint256 {
    mathint result;
    require result >= 0 && result * result <= x && x < (result + 1) * (result + 1);
    return assert_uint256(result);
}

function mulWadDownSummary(uint256 x, uint256 y) returns uint256 {
    mathint result;
    result = x * y / 1000000000000000000;
    if (result >= 2^256) revert();
    return assert_uint256(result);
}

function mulWadUpSummary(uint256 x, uint256 y) returns uint256 {
    mathint result;
    result = (x * y + 999999999999999999) / 1000000000000000000;
    if (result >= 2^256) revert();
    return assert_uint256(result);
}

function divWadDownSummary(uint256 x, uint256 y) returns uint256 {
    mathint result;
    if (y == 0) revert();
    result = x * 1000000000000000000 / y;
    if (result >= 2^256) revert();
    return assert_uint256(result);
}

function divWadUpSummary(uint256 x, uint256 y) returns uint256 {
    mathint result;
    if (y == 0) revert();
    result = (x * 1000000000000000000 + y - 1) / y;
    if (result >= 2^256) revert();
    return assert_uint256(result);
}