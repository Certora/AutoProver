// OpenZeppelin Arrays.unsafeMemoryAccess: an unchecked (assembly `mload`) memory
// array read whose raw pointer arithmetic defeats the points-to analysis. It reads
// the element at `pos` without a bounds check, so it is modeled as `arr[pos]` (the
// caller is responsible for `pos` being in bounds, matching the function's contract).
methods {
    function Arrays.unsafeMemoryAccess(uint256[] memory arr, uint256 pos) internal returns (uint256) => arr[pos];
    function Arrays.unsafeMemoryAccess(bytes32[] memory arr, uint256 pos) internal returns (bytes32) => arr[pos];
    function Arrays.unsafeMemoryAccess(address[] memory arr, uint256 pos) internal returns (address) => arr[pos];
}
