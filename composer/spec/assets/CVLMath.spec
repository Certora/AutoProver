/*
 * CVLMath — reusable CVL math library, shipped into generated projects by the
 * autoprove pipeline as certora/specs/summaries/CVLMath.spec.
 *
 * Two tiers of models for the standard Solidity mulDiv/WAD helpers:
 *   1. Exact summaries (`*Summary`, defined below): faithful models preserving
 *      the exact rounding and revert behavior. Copied verbatim from the
 *      AutoSetup-bundled Math.spec library. Use these by default.
 *   2. Relational abstractions (`*Abstract`, pulled in from the imported
 *      CVLMathAbstract.spec): replace the nonlinear product / quotient with
 *      solver-friendly axioms (zero-preservation, exactness at the
 *      denominator, monotonicity, down/up rounding within 1). Use these as
 *      summaries when the exact formulas make the prover time out and the
 *      property only needs relational facts — do NOT weaken the property
 *      instead.
 *
 * NB: because Tier 1 is byte-identical to the AutoSetup-bundled Math.spec,
 * importing both would define the `*Summary` functions twice and fail
 * typechecking. The pipeline therefore only installs this file when the
 * project's AutoSetup summaries did NOT already pull in Math.spec; otherwise
 * it installs CVLMathAbstract.spec alone. Keep that in mind if you copy this
 * file into a project by hand.
 */

import "CVLMathAbstract.spec";

/*************************************************************
 * Tier 1: exact summaries (verbatim from AutoSetup Math.spec)
 *************************************************************/

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
