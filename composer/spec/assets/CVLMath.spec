/*
 * CVLMath — reusable CVL math library, shipped into generated projects by the
 * autoprove pipeline as certora/specs/summaries/CVLMath.spec.
 *
 * Two tiers of models for the standard Solidity mulDiv/WAD helpers:
 *   1. Exact summaries (`*Summary`): faithful models preserving the exact
 *      rounding and revert behavior. Copied verbatim from the AutoSetup-bundled
 *      Math.spec library. Use these by default.
 *   2. Relational abstractions (`*Abstract`): replace the nonlinear product /
 *      quotient with solver-friendly axioms (zero-preservation, exactness at
 *      the denominator, monotonicity, down/up rounding within 1). Use these as
 *      summaries when the exact formulas make the prover time out and the
 *      property only needs relational facts — do NOT weaken the property
 *      instead.
 *
 * NB: if this project's AutoSetup summaries already pulled in the bundled
 * Math.spec (e.g. via an OpenZeppelin Math template), importing this file too
 * would define the `*Summary` functions twice and fail typechecking. In that
 * case rely on the already-imported exact summaries, and copy just the
 * `*Abstract` tier below into your own spec if you need it.
 */

definition WAD() returns uint256 = 10^18;
definition RAY() returns uint256 = 10^27;

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

/*************************************************************
 * Tier 2: relational abstractions
 *
 * The `*Abstract` functions read their result from an uninterpreted ghost
 * mapping instead of computing the exact quotient, so the solver never sees
 * the nonlinear `x * y / d` term. The ghost is shared by every call, hence
 * two calls with equal arguments agree — which is what makes cross-call
 * reasoning (e.g. round-trip inequalities) possible. The quantified axioms
 * carry the global monotonicity facts; everything argument-specific is
 * `require`d at the call site.
 *
 * These abstractions deliberately UNDER-constrain: they are sound (every real
 * mulDiv execution satisfies them, up to the overflow caveat noted on each
 * function) but too weak to prove exact-value properties.
 *************************************************************/

// Uninterpreted model of floor(x * y / d), indexed as [x][y][d].
ghost mapping(uint256 => mapping(uint256 => mapping(uint256 => uint256))) mulDivDownGhost {
    // Weakly monotone in each numerator factor, antitone in the denominator.
    axiom forall uint256 x1. forall uint256 x2. forall uint256 y. forall uint256 d.
        x1 <= x2 => mulDivDownGhost[x1][y][d] <= mulDivDownGhost[x2][y][d];
    axiom forall uint256 x. forall uint256 y1. forall uint256 y2. forall uint256 d.
        y1 <= y2 => mulDivDownGhost[x][y1][d] <= mulDivDownGhost[x][y2][d];
    axiom forall uint256 x. forall uint256 y. forall uint256 d1. forall uint256 d2.
        d1 <= d2 => mulDivDownGhost[x][y][d2] <= mulDivDownGhost[x][y][d1];
}

// Uninterpreted model of ceil(x * y / d), indexed as [x][y][d]. Its coupling
// with the floor model (down <= up <= down + 1) is required per call in
// mulDivUpAbstract below.
ghost mapping(uint256 => mapping(uint256 => mapping(uint256 => uint256))) mulDivUpGhost {
    axiom forall uint256 x1. forall uint256 x2. forall uint256 y. forall uint256 d.
        x1 <= x2 => mulDivUpGhost[x1][y][d] <= mulDivUpGhost[x2][y][d];
    axiom forall uint256 x. forall uint256 y1. forall uint256 y2. forall uint256 d.
        y1 <= y2 => mulDivUpGhost[x][y1][d] <= mulDivUpGhost[x][y2][d];
    axiom forall uint256 x. forall uint256 y. forall uint256 d1. forall uint256 d2.
        d1 <= d2 => mulDivUpGhost[x][y][d2] <= mulDivUpGhost[x][y][d1];
}

// Relational model of floor(x * y / d). The result is constrained only by the
// ghost axioms above and the `require`-based axioms below — NOT the exact
// quotient — so exact-value assertions will not be provable against it.
// VACUITY WARNING: because the constraints are imposed with `require`, a rule
// whose other assumptions contradict them is silently pruned rather than
// reported (potential vacuity); keep rule_sanity checks on when summarizing
// with this function. Also unlike mulDivDownSummary, the overflow revert is
// not modeled: the result is assumed to fit in uint256.
function mulDivDownAbstract(uint256 x, uint256 y, uint256 d) returns uint256 {
    if (d == 0) revert();
    uint256 result = mulDivDownGhost[x][y][d];
    // Zero-preservation: 0 * y == x * 0 == 0.
    require (x == 0 || y == 0) => result == 0;
    // Exactness when one factor equals the denominator: x * d / d == x.
    require y == d => result == x;
    require x == d => result == y;
    // Linear relaxation of result * d <= x * y < (result + 1) * d: comparing
    // one factor against the denominator bounds the result by the other factor.
    require y <= d => result <= x;
    require y >= d => result >= x;
    require x <= d => result <= y;
    require x >= d => result >= y;
    return result;
}

// Relational model of ceil(x * y / d) = the mulDivUp / round-up family.
// Same VACUITY WARNING as mulDivDownAbstract: all constraints are
// `require`-based axioms, so contradicting assumptions prune silently, and
// the overflow revert is not modeled.
function mulDivUpAbstract(uint256 x, uint256 y, uint256 d) returns uint256 {
    if (d == 0) revert();
    uint256 result = mulDivUpGhost[x][y][d];
    // Zero-preservation: ceil(0 / d) == 0.
    require (x == 0 || y == 0) => result == 0;
    // Exactness when one factor equals the denominator (no remainder to round).
    require y == d => result == x;
    require x == d => result == y;
    // Linear relaxation of (result - 1) * d < x * y <= result * d (see the
    // floor variant for the reading).
    require y <= d => result <= x;
    require y >= d => result >= x;
    require x <= d => result <= y;
    require x >= d => result >= y;
    // Rounding-direction coupling with the floor model: down <= up <= down + 1.
    require mulDivDownGhost[x][y][d] <= result;
    require to_mathint(result) <= mulDivDownGhost[x][y][d] + 1;
    return result;
}

// WAD convenience wrappers over the relational models (the abstract
// counterparts of mulWadDownSummary etc.). They inherit the vacuity /
// no-overflow-revert caveats of the functions they delegate to.
function mulWadDownAbstract(uint256 x, uint256 y) returns uint256 {
    return mulDivDownAbstract(x, y, WAD());
}

function mulWadUpAbstract(uint256 x, uint256 y) returns uint256 {
    return mulDivUpAbstract(x, y, WAD());
}

function divWadDownAbstract(uint256 x, uint256 y) returns uint256 {
    return mulDivDownAbstract(x, WAD(), y);
}

function divWadUpAbstract(uint256 x, uint256 y) returns uint256 {
    return mulDivUpAbstract(x, WAD(), y);
}
