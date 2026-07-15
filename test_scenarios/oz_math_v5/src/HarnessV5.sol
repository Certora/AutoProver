// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Math} from "../lib/oz-v5/Math.sol";

/// Small consumer of OpenZeppelin v5 Math: the 4-arg mulDiv call (with a
/// Math.Rounding literal) is what brings the directional signature and the
/// Rounding enum into autosetup's build data.
contract HarnessV5 {
    function mulDivFloor(uint256 x, uint256 y, uint256 d) external pure returns (uint256) {
        return Math.mulDiv(x, y, d);
    }

    function mulDivCeil(uint256 x, uint256 y, uint256 d) external pure returns (uint256) {
        return Math.mulDiv(x, y, d, Math.Rounding.Ceil);
    }

    function sqrtOf(uint256 x) external pure returns (uint256) {
        return Math.sqrt(x);
    }
}
