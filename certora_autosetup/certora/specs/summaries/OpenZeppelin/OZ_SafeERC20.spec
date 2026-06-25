// Summarization of OpenZeppelin's SafeERC20 library functions
// Maps safeTransfer and safeTransferFrom to direct token calls

methods {
    // SafeERC20 internal functions summarized as direct token calls
    function _.safeTransfer(address token, address to, uint256 value) internal => 
        cvl_safeTransfer(executingContract, token, to, value) expect void;
    
    function _.safeTransferFrom(address token, address from, address to, uint256 value) internal => 
        cvl_safeTransferFrom(executingContract, token, from, to, value) expect void;
}

// CVL function that directly calls transfer on the token
function cvl_safeTransfer(address executing_contract, address token, address to, uint256 value) {
    // Direct call to the token's transfer function
    // The SafeERC20 wrapper handles return value checking, but here we call directly
    env e;
    require e.msg.sender == executing_contract, "The caller must be the contract executing the SafeERC20 function";
    
    bool success = token.transfer(e, to, value);
    
    require success, "SafeERC20 would revert on failure, so we model this behavior";
}

// CVL function that directly calls transferFrom on the token
function cvl_safeTransferFrom(address executing_contract, address token, address from, address to, uint256 value) {
    // Direct call to the token's transferFrom function
    // The SafeERC20 wrapper handles return value checking, but here we call directly
    env e;
    require e.msg.sender == executing_contract, "The caller must be the contract executing the SafeERC20 function";
    
    bool success = token.transferFrom(e, from, to, value);
    
    require success, "SafeERC20 would revert on failure, so we model this behavior";
}