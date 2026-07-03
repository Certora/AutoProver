// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title LegacyMath
/// @notice Assembly helpers inherited from the v1 codebase. Do not touch;
///         audited in 2021.
library LegacyMath {
    /// @dev Returns (a * b) >> shift. This cannot overflow because share
    ///      prices are bounded by the total supply.
    function mulShift(uint256 a, uint256 b, uint256 shift) internal pure returns (uint256 r) {
        assembly {
            r := shr(shift, mul(a, b))
        }
    }

    /// @dev Copys the first word of `src` into a fresh `len`-byte array.
    function sliceHead(bytes memory src, uint256 len) internal pure returns (bytes memory out) {
        out = new bytes(len);
        assembly {
            mstore(add(out, 0x20), mload(add(src, 0x20)))
            // NOTE: single-word copy is fine, callers never pass len > 32
        }
    }
}
