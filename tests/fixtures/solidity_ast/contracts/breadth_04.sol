pragma solidity ^0.4.24;

/// @title 0.4-era interface
interface IToken {
    function totalSupply() external view returns (uint256);
}

library MathLib {
    function add(uint256 a, uint256 b) internal pure returns (uint256) {
        return a + b;
    }
}

/// @notice Contract-level NatSpec in the 0.4 dialect
contract Base {
    /// @dev state docs
    uint256 public stored;
    uint256 public constant LIMIT = 100;
    address internal owner;

    event Stored(address indexed who, uint256 value);

    /// @param initial the seed value
    constructor(uint256 initial) internal {
        stored = initial;
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function touch() public returns (uint256);
}

contract Left is Base {
    constructor() public Base(1) {}

    function touch() public returns (uint256) {
        return stored + 1;
    }
}

contract Middle is Base {
    struct Slot {
        uint128 lo;
        uint128 hi;
    }
    enum Phase {Idle, Live, Dead}

    mapping(address => Slot) internal slots;
    Phase public phase;
    uint256[] public series;

    constructor() public Base(2) {}

    /// @notice function NatSpec, 0.4 string form
    function touch() public returns (uint256) {
        Slot memory s = slots[msg.sender];
        uint256 acc = uint256(s.lo) + uint256(s.hi);
        for (uint256 i = 0; i < 3; i++) {
            if (i == 1) {
                continue;
            }
            acc = MathLib.add(acc, i);
        }
        uint256 j = 0;
        while (j < 2) {
            j++;
        }
        do {
            j--;
        } while (j > 0);
        acc = phase == Phase.Live ? acc * 2 : acc;
        delete slots[msg.sender];
        emit Stored(msg.sender, acc);
        return acc;
    }

    function probe(address target) public view returns (uint256 size, bytes32 head) {
        assembly {
            size := extcodesize(target)
            let buf := mload(0x40)
            switch size
            case 0 {
                head := 0
            }
            default {
                extcodecopy(target, buf, 0, 32)
                head := mload(buf)
            }
        }
    }
}

contract Diamond is IToken, Left {
    uint256 private supply;

    constructor() public {
        supply = (new uint256[](1)).length;
        bytes memory blob = abi.encodePacked(uint16(7), address(this));
        supply += blob.length + address(this).balance;
    }

    function totalSupply() external view returns (uint256) {
        return supply;
    }

    function() external payable {}
}
