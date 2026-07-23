// Generic spec to adapt for dealing with extsload and exttload functions.
// These variants come from different mixins (Uniswap Extsload / Exttload, Aave ExtSload)
// and a given contract implements only a subset. On instantiation, entries that don't
// match a real method of the contract are dropped (see _filter_template_entries_to_contract),
// so no `optional` is needed and same-signature/different-return variants can coexist here.
methods {
    function $CONTRACT_NAME$.extsload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function $CONTRACT_NAME$.extsload(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
    function $CONTRACT_NAME$.extsload(bytes32 startSlot, uint256 nSlots) external returns (bytes32[] memory) => ArbNBytes32(startSlot, nSlots) DELETE;
    function $CONTRACT_NAME$.extsload(bytes32 startSlot, uint256 nSlots) external returns (bytes memory) => ArbNBytes(startSlot, nSlots) DELETE;
    function $CONTRACT_NAME$.exttload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function $CONTRACT_NAME$.exttload(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
    function $CONTRACT_NAME$.exttload(bytes32[] slots) external returns (bytes memory) => ArbBytes(slots) DELETE;
    // camelCase naming (e.g. Aave: extSload / extSloads)
    function $CONTRACT_NAME$.extSload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function $CONTRACT_NAME$.extSloads(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
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