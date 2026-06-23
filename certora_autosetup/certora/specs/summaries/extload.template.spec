// Generic spec to adapt for dealing with extsload and exttload functions.
methods {
    function $CONTRACT_NAME$.extsload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function $CONTRACT_NAME$.extsload(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
    function $CONTRACT_NAME$.extsload(bytes32 startSlot, uint256 nSlots) external returns (bytes32[] memory) => ArbNBytes32(startSlot, nSlots) DELETE;
    function $CONTRACT_NAME$.extsload(bytes32 startSlot, uint256 nSlots) external returns (bytes memory) => ArbNBytes(startSlot, nSlots) DELETE;
    function $CONTRACT_NAME$.exttload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function $CONTRACT_NAME$.exttload(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
    function $CONTRACT_NAME$.exttload(bytes32[] slots) external returns (bytes memory) => ArbBytes(slots) DELETE;
}

function ArbBytes32(bytes32[] slots) returns bytes32[] {
    bytes32[] data;
    require data.length == slots.length, "match returned length to input length";
    return data;
}

/// Returns an arbitrary bytes32 array of length nSlots.
function ArbNBytes32(bytes32 startSlot, uint256 nSlots) returns bytes32[] {
    bytes32[] data;
    require data.length == nSlots, "match returned length to input length";
    return data;
}

/// Returns an arbitrary bytes array of length nSlots _words_.
function ArbNBytes(bytes32 startSlot, uint256 nSlots) returns bytes {
    bytes data;
    require data.length == 32*nSlots, "match returned length to input length";
    return data;
}