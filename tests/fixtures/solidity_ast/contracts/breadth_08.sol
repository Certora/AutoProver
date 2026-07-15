// SPDX-License-Identifier: MIT
// Breadth fixture for solc 0.8.x compact-AST generation. Everything in
// breadth_06.sol PLUS: custom errors + revert statement, user-defined value
// type, unchecked blocks, free functions, index-range access (calldata bytes
// slicing), named mapping params, user-defined operators with
// `using {...} for ... global`, address.code / .code.length.
pragma solidity ^0.8.19;

type Price is uint128;

function addPrice(Price a, Price b) pure returns (Price) {
    return Price.wrap(Price.unwrap(a) + Price.unwrap(b));
}

function eqPrice(Price a, Price b) pure returns (bool) {
    return Price.unwrap(a) == Price.unwrap(b);
}

using {addPrice as +, eqPrice as ==} for Price global;

error Unauthorized(address who);
error Insufficient(uint256 requested, uint256 available);

// Free function.
function clamp(uint256 x, uint256 limit) pure returns (uint256) {
    return x > limit ? limit : x;
}

interface IToken {
    event Transfer(address indexed from, address indexed to, uint256 value);

    function balanceOf(address who) external view returns (uint256);
}

library MathLib {
    function clampedAdd(uint256 a, uint256 b) internal pure returns (uint256) {
        unchecked {
            uint256 c = a + b;
            return c < a ? type(uint256).max : c;
        }
    }
}

abstract contract Base {
    enum Phase {
        Init,
        Active,
        Done
    }

    struct Account {
        uint256 balance;
        uint64 nonce;
    }

    uint256 public constant LIMIT = 100;
    uint256 public immutable createdAt;
    address internal owner;

    event PhaseChanged(Phase indexed newPhase);

    modifier onlyOwner() {
        if (msg.sender != owner) {
            revert Unauthorized(msg.sender);
        }
        _;
    }

    constructor() {
        createdAt = block.timestamp;
        owner = msg.sender;
    }

    function ping() public virtual returns (uint256);
}

contract Left is Base {
    function ping() public virtual override returns (uint256) {
        return 1;
    }
}

contract Right is Base {
    function ping() public virtual override returns (uint256) {
        return 2;
    }
}

contract Diamond is Left, Right, IToken {
    using MathLib for uint256;

    // Named mapping key/value params (solc >= 0.8.18).
    mapping(address owner => Account account) public accounts;
    uint256[] internal history;
    Phase public phase;
    Price public floorPrice;

    constructor(address firstUser, uint256 seed) {
        accounts[firstUser] = Account({balance: seed, nonce: 0});
        history.push(seed);
        floorPrice = Price.wrap(uint128(clamp(seed, LIMIT)));
    }

    function ping() public override(Left, Right) returns (uint256) {
        phase = Phase.Active;
        emit PhaseChanged(phase);
        return super.ping();
    }

    function balanceOf(address who) external view override returns (uint256) {
        return accounts[who].balance;
    }

    function transfer(address to, uint256 value) external onlyOwner returns (bool) {
        Account storage from = accounts[msg.sender];
        if (from.balance < value) {
            revert Insufficient(value, from.balance);
        }
        unchecked {
            from.balance -= value;
        }
        accounts[to].balance = accounts[to].balance.clampedAdd(value);
        emit Transfer(msg.sender, to, value);
        return true;
    }

    // User-defined operators on the user-defined value type.
    function total(Price a, Price b) public pure returns (Price) {
        if (a == b) {
            return a + a;
        }
        return a + b;
    }

    function applyTwice(
        function(uint256) internal pure returns (uint256) f,
        uint256 x
    ) internal pure returns (uint256) {
        return f(f(x));
    }

    function bump(uint256 x) internal pure returns (uint256) {
        return x + 1;
    }

    function tupleAndConditional(uint256 a, uint256 b)
        public
        pure
        returns (uint256 lo, uint256 hi)
    {
        uint256 m = a < b ? a : b;
        (lo, hi) = (m, a + b);
        lo = applyTwice(bump, lo);
    }

    function safeBalance(IToken token, address who) external view returns (uint256) {
        try token.balanceOf(who) returns (uint256 value) {
            return value;
        } catch Error(string memory) {
            return 0;
        } catch (bytes memory) {
            return type(uint256).max;
        }
    }

    function codeProbe(address target) public view returns (uint256 size, bytes memory blob) {
        size = target.code.length;
        blob = address(this).code;
    }

    // Index-range access on calldata bytes.
    function selectorOf(bytes calldata data) external pure returns (bytes calldata) {
        return data[0:4];
    }

    function controlFlow(uint256 n) public pure returns (uint256 acc) {
        for (uint256 i = 0; i < n; i++) {
            if (i == 3) {
                continue;
            } else if (i > 7) {
                break;
            }
            acc += i;
        }
        uint256 j = 0;
        while (j < n) {
            j++;
        }
        do {
            acc = acc + 1;
        } while (acc < n);
        uint256[] memory scratch = new uint256[](n + 1);
        scratch[0] = acc;
        delete scratch[0];
        acc = scratch.length;
    }

    function sendNothing(address payable to) external onlyOwner {
        (bool ok, ) = to.call{value: 0}("");
        require(ok, "call failed");
    }

    receive() external payable {}

    fallback() external payable {}

    function yulStuff(uint256 n) public pure returns (uint256 r) {
        assembly {
            function double(a) -> b {
                if iszero(a) {
                    leave
                }
                b := mul(a, 2)
            }
            let acc := 0
            for {
                let i := 0
            } lt(i, n) {
                i := add(i, 1)
            } {
                if eq(i, 5) {
                    continue
                }
                if gt(i, 10) {
                    break
                }
                acc := add(acc, double(i))
            }
            switch and(acc, 1)
            case 0 {
                r := acc
            }
            case 1 {
                r := add(acc, 1)
            }
            default {
                r := 0
            }
            let tag := "yul"
            let flag := true
            if and(flag, gt(r, 0xff)) {
                r := byte(0, tag)
            }
        }
    }
}
