// Summarization of Solady's SafeTransferLib functions.
// Mirrors OpenZeppelin/OZ_SafeERC20.spec: the library wraps a raw token call in
// assembly (which breaks the prover's pointer analysis), so we reroute each safe
// wrapper to a direct token call and `require` success to model revert-on-failure.
//
// Uses an explicit `SafeTransferLib.` receiver (not the wildcard `_.` OZ_SafeERC20
// uses): Solady's safeTransfer(address,address,uint256) shares the same erased
// signature as OZ SafeERC20's, so an explicit receiver avoids a double-declaration
// conflict when a project imports both libraries, and makes each entry eligible for
// the summary_resolver prune pass.
//
// Out of scope (left unsummarized): ETH helpers (safeTransferETH, forceSafeTransferETH,
// trySafeTransferETH, safeTransferAllETH) — raw call{value:}, would need a BALANCE hook
// to model ETH effects; the balance-derived variants (safeTransferAll/safeTransferAllFrom);
// and the Permit2 helpers (safeTransferFrom2, permit2, permit2TransferFrom).

methods {
    function SafeTransferLib.safeTransfer(address token, address to, uint256 amount) internal =>
        cvl_safeTransfer(executingContract, token, to, amount) expect void;

    function SafeTransferLib.safeTransferFrom(address token, address from, address to, uint256 amount) internal =>
        cvl_safeTransferFrom(executingContract, token, from, to, amount) expect void;

    function SafeTransferLib.safeApprove(address token, address to, uint256 amount) internal =>
        cvl_safeApprove(executingContract, token, to, amount) expect void;

    // safeApproveWithRetry has the same success semantics as safeApprove; the retry path
    // only matters when the first approve reverts, which our success-modeling elides.
    function SafeTransferLib.safeApproveWithRetry(address token, address to, uint256 amount) internal =>
        cvl_safeApprove(executingContract, token, to, amount) expect void;

    function SafeTransferLib.balanceOf(address token, address account) internal returns (uint256) =>
        cvl_balanceOf(token, account);
}

// Direct call to the token's transfer function. SafeTransferLib checks the return value
// and reverts on failure, so we require success to model that behavior.
function cvl_safeTransfer(address executing_contract, address token, address to, uint256 amount) {
    env e;
    require e.msg.sender == executing_contract, "The caller must be the contract executing the SafeTransferLib function";

    bool success = token.transfer(e, to, amount);

    require success, "SafeTransferLib would revert on failure, so we model this behavior";
}

// Direct call to the token's transferFrom function.
function cvl_safeTransferFrom(address executing_contract, address token, address from, address to, uint256 amount) {
    env e;
    require e.msg.sender == executing_contract, "The caller must be the contract executing the SafeTransferLib function";

    bool success = token.transferFrom(e, from, to, amount);

    require success, "SafeTransferLib would revert on failure, so we model this behavior";
}

// Direct call to the token's approve function.
function cvl_safeApprove(address executing_contract, address token, address to, uint256 amount) {
    env e;
    require e.msg.sender == executing_contract, "The caller must be the contract executing the SafeTransferLib function";

    bool success = token.approve(e, to, amount);

    require success, "SafeTransferLib would revert on failure, so we model this behavior";
}

// Direct call to the token's balanceOf. SafeTransferLib.balanceOf reverts if the call
// fails; modeled here as a plain successful read.
function cvl_balanceOf(address token, address account) returns uint256 {
    env e;
    return token.balanceOf(e, account);
}
