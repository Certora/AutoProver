// SPDX-License-Identifier: MIT
// Self-authored signal-bait fixture for certora-fv-amenability tests.
// Each construct below exists to trip exactly one static signal; none of this
// is copied from any real project.
pragma solidity ^0.8.30;

contract PackedBook {
    uint256 internal constant MASK_LOW = 0x000000000000000000000000000000000000000000000000ffffffffffffffff;
    uint256 internal constant SHIFT_HIGH = 192;

    mapping(uint256 => uint256) internal packed;
    uint256[] internal items;
    address public target;
    uint256 internal acc;

    // S3: delegatecall trampoline (assembly) + S2: free-memory-pointer write
    function forward(bytes calldata data) external returns (bytes memory out) {
        address t = target;
        assembly {
            let ptr := mload(0x40)
            calldatacopy(ptr, data.offset, data.length)
            let ok := delegatecall(gas(), t, ptr, data.length, 0, 0)
            returndatacopy(ptr, 0, returndatasize())
            mstore(0x40, add(ptr, returndatasize()))
            if iszero(ok) { revert(ptr, returndatasize()) }
            out := ptr
        }
    }

    // S3: Solidity-level low-level delegatecall + S10: low-level call
    function forwardSimple(bytes calldata data) external returns (bool ok) {
        (ok, ) = target.delegatecall(data);
        (bool sent, ) = target.call("");
        require(sent);
    }

    // S9: computed-slot storage access
    function rawRead(uint256 key) external view returns (uint256 v) {
        assembly {
            mstore(0x00, key)
            let slot := keccak256(0x00, 0x20)
            v := sload(slot)
        }
    }

    // S4 + S7: inline bit surgery mixed with nonlinear math, no accessor seam
    function decodeAndPrice(uint256 word, uint256 reserveA, uint256 reserveB)
        public pure returns (uint256 price)
    {
        uint256 size = word & MASK_LOW;
        uint256 tick = (word >> 64) & MASK_LOW;
        uint256 flags = (word >> 128) & 0xffff;
        uint256 top = word >> SHIFT_HIGH;
        uint256 mixed = (size | (tick << 8)) ^ (flags & top);
        price = (reserveA * size * 9975) / (reserveB * tick * 10000 + 1);
        price = price * mixed / (top + 1);
        price = (price << 4) | (price >> 8) | (price & MASK_LOW);
    }

    // S6: unchecked nonlinear with symbolic operands
    function unsafeMath(uint256 a, uint256 b) external pure returns (uint256 r) {
        unchecked {
            r = a * b;
            r = r / (a + b);
            r = r % (b + 1);
        }
    }

    // S8: hand-rolled curated-name kernel
    function mulDiv(uint256 x, uint256 y, uint256 d) public pure returns (uint256) {
        return (x * y) / d;
    }

    // S11: dynamic loops (storage length + symbolic bound)
    function sweep(uint256 n) external {
        for (uint256 i = 0; i < items.length; i++) {
            packed[i] = items[i];
        }
        for (uint256 j = 0; j < n; j++) {
            packed[j] += 1;
        }
    }

    // S5: >150-line function
    function veryLong() external {
        acc = acc + 1;
        acc = acc + 2;
        acc = acc + 3;
        acc = acc + 4;
        acc = acc + 5;
        acc = acc + 6;
        acc = acc + 7;
        acc = acc + 8;
        acc = acc + 9;
        acc = acc + 10;
        acc = acc + 11;
        acc = acc + 12;
        acc = acc + 13;
        acc = acc + 14;
        acc = acc + 15;
        acc = acc + 16;
        acc = acc + 17;
        acc = acc + 18;
        acc = acc + 19;
        acc = acc + 20;
        acc = acc + 21;
        acc = acc + 22;
        acc = acc + 23;
        acc = acc + 24;
        acc = acc + 25;
        acc = acc + 26;
        acc = acc + 27;
        acc = acc + 28;
        acc = acc + 29;
        acc = acc + 30;
        acc = acc + 31;
        acc = acc + 32;
        acc = acc + 33;
        acc = acc + 34;
        acc = acc + 35;
        acc = acc + 36;
        acc = acc + 37;
        acc = acc + 38;
        acc = acc + 39;
        acc = acc + 40;
        acc = acc + 41;
        acc = acc + 42;
        acc = acc + 43;
        acc = acc + 44;
        acc = acc + 45;
        acc = acc + 46;
        acc = acc + 47;
        acc = acc + 48;
        acc = acc + 49;
        acc = acc + 50;
        acc = acc + 51;
        acc = acc + 52;
        acc = acc + 53;
        acc = acc + 54;
        acc = acc + 55;
        acc = acc + 56;
        acc = acc + 57;
        acc = acc + 58;
        acc = acc + 59;
        acc = acc + 60;
        acc = acc + 61;
        acc = acc + 62;
        acc = acc + 63;
        acc = acc + 64;
        acc = acc + 65;
        acc = acc + 66;
        acc = acc + 67;
        acc = acc + 68;
        acc = acc + 69;
        acc = acc + 70;
        acc = acc + 71;
        acc = acc + 72;
        acc = acc + 73;
        acc = acc + 74;
        acc = acc + 75;
        acc = acc + 76;
        acc = acc + 77;
        acc = acc + 78;
        acc = acc + 79;
        acc = acc + 80;
        acc = acc + 81;
        acc = acc + 82;
        acc = acc + 83;
        acc = acc + 84;
        acc = acc + 85;
        acc = acc + 86;
        acc = acc + 87;
        acc = acc + 88;
        acc = acc + 89;
        acc = acc + 90;
        acc = acc + 91;
        acc = acc + 92;
        acc = acc + 93;
        acc = acc + 94;
        acc = acc + 95;
        acc = acc + 96;
        acc = acc + 97;
        acc = acc + 98;
        acc = acc + 99;
        acc = acc + 100;
        acc = acc + 101;
        acc = acc + 102;
        acc = acc + 103;
        acc = acc + 104;
        acc = acc + 105;
        acc = acc + 106;
        acc = acc + 107;
        acc = acc + 108;
        acc = acc + 109;
        acc = acc + 110;
        acc = acc + 111;
        acc = acc + 112;
        acc = acc + 113;
        acc = acc + 114;
        acc = acc + 115;
        acc = acc + 116;
        acc = acc + 117;
        acc = acc + 118;
        acc = acc + 119;
        acc = acc + 120;
        acc = acc + 121;
        acc = acc + 122;
        acc = acc + 123;
        acc = acc + 124;
        acc = acc + 125;
        acc = acc + 126;
        acc = acc + 127;
        acc = acc + 128;
        acc = acc + 129;
        acc = acc + 130;
        acc = acc + 131;
        acc = acc + 132;
        acc = acc + 133;
        acc = acc + 134;
        acc = acc + 135;
        acc = acc + 136;
        acc = acc + 137;
        acc = acc + 138;
        acc = acc + 139;
        acc = acc + 140;
        acc = acc + 141;
        acc = acc + 142;
        acc = acc + 143;
        acc = acc + 144;
        acc = acc + 145;
        acc = acc + 146;
        acc = acc + 147;
        acc = acc + 148;
        acc = acc + 149;
        acc = acc + 150;
        acc = acc + 151;
        acc = acc + 152;
        acc = acc + 153;
        acc = acc + 154;
        acc = acc + 155;
    }
}

// Control: nothing here should trip any signal.
contract CleanVault {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount, "insufficient");
        balances[msg.sender] -= amount;
        payable(msg.sender).transfer(amount);
    }

    // Small internal pure accessor: encapsulated bit use is the GOOD pattern (S4).
    function _flagOf(uint256 word) internal pure returns (bool) {
        return word & 1 == 1;
    }

    function flagged(uint256 word) external pure returns (bool) {
        return _flagOf(word);
    }
}
