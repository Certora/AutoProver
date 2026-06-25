// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.19;

import {Token} from "./Token.sol";

/// Share ledger backed by a `Token`. `withdraw` has a deliberate bug so the
/// consistency invariant and a plain rule both fail.
contract Bank {
    Token public token;
    mapping(address => uint256) public shares;
    uint256 public totalShares;
    address public owner;

    constructor(Token _token) {
        token = _token;
        owner = msg.sender;
    }

    function deposit(uint256 amount) external {
        token.transferFrom(msg.sender, address(this), amount);
        shares[msg.sender] += amount;
        totalShares += amount;
    }

    function withdraw(uint256 amount) external {
        shares[msg.sender] -= amount;
        // BUG: forgets `totalShares -= amount;`, so `totalShares` drifts from
        // the sum of `shares` entries.
        token.transfer(msg.sender, amount);
    }

    function transferShares(address to, uint256 amount) external {
        shares[msg.sender] -= amount;
        shares[to] += amount;
    }

    function setOwner(address newOwner) external {
        require(msg.sender == owner, "not owner");
        owner = newOwner;
    }
}
