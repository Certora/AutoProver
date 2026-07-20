// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.29;

/// Per-account deposit vault with a fixed cap. The implementation you generate
/// must expose exactly this surface.
interface ICappedVault {
    /// Deposit `amount` into the caller's balance.
    function deposit(uint256 amount) external;

    /// Withdraw `amount` from the caller's balance.
    function withdraw(uint256 amount) external;

    /// The caller-visible balance of `account`.
    function balance(address account) external view returns (uint256);

    /// The running total of all deposited funds.
    function totalDeposited() external view returns (uint256);

    /// The per-account balance cap.
    function CAP() external view returns (uint256);
}
