// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Math} from "../lib/oz-v4/Math.sol";

/// Small consumer of OpenZeppelin v4 Math: the 4-arg mulDiv call (with a
/// Math.Rounding literal) is what brings the directional signature and the
/// Rounding enum into autosetup's build data.
contract HarnessV4 {
    function mulDivFloor(uint256 x, uint256 y, uint256 d) external pure returns (uint256) {
        return Math.mulDiv(x, y, d);
    }

    function mulDivUp(uint256 x, uint256 y, uint256 d) external pure returns (uint256) {
        return Math.mulDiv(x, y, d, Math.Rounding.Up);
    }

    function sqrtOf(uint256 x) external pure returns (uint256) {
        return Math.sqrt(x);
    }
}
