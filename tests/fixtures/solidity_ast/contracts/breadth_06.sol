// SPDX-License-Identifier: MIT
// Breadth fixture for solc 0.6.x / 0.7.x compact-AST generation. Exercises:
// inheritance diamond, abstract contract, interface, library, struct/enum/event/
// modifier, constructor params, try/catch, inline assembly (function def, for,
// switch, literals, break/continue/leave), tuple & conditional expressions,
// internal function types, `using for`, staticcall probing (no address.code
// member before 0.8), constant/immutable state vars, mapping/array types.
pragma solidity >=0.6.12 <0.8.0;

interface IToken {
    event Transfer(address indexed from, address indexed to, uint256 value);

    function balanceOf(address who) external view returns (uint256);
}

library MathLib {
    function clampedAdd(uint256 a, uint256 b) internal pure returns (uint256) {
        uint256 c = a + b;
        return c < a ? type(uint256).max : c;
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
        require(msg.sender == owner, "not owner");
        _;
    }

    // `internal`: required by 0.6 for abstract-contract constructors, warning-only on 0.7.
    constructor() internal {
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

    mapping(address => Account) public accounts;
    uint256[] internal history;
    Phase public phase;

    constructor(address firstUser, uint256 seed) public {
        accounts[firstUser] = Account({balance: seed, nonce: 0});
        history.push(seed);
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
        require(from.balance >= value, "insufficient");
        from.balance -= value;
        accounts[to].balance = accounts[to].balance.clampedAdd(value);
        emit Transfer(msg.sender, to, value);
        return true;
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

    // Pre-0.8 there is no address.code member; probe the target via staticcall.
    function probe(address target) public view returns (bool ok, bytes memory data) {
        (ok, data) = target.staticcall(abi.encodeWithSignature("ping()"));
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
