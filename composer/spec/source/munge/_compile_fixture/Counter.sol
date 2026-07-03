// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "./Math.sol";

contract Counter {
    uint256 public count;

    function increment(uint256 by) external {
        count = Math.add(count, by);
    }
}
