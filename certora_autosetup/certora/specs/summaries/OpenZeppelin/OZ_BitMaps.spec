// BitMaps.BitMap cannot be a ghost mapping key, so the library's calls are rerouted to
// OZ_BitMaps (added to the scene alongside this spec) and the ghost is keyed on the storage
// slot instead. The reroute host is generated per project from OZ_BitMaps.template.sol,
// because its parameter type has to come from the project's own BitMaps.sol.
//
// The model replaces the BitMap's storage with a ghost and nothing ties the two together:
// reading bitmap._data directly, deleting the struct, a raw sstore, or copying the struct all
// diverge from it silently. The slot key is also only as sound as distinct BitMaps having
// distinct slots — true for a plain field, but a BitMap reached through a mapping has a
// hashed pointer, so a collision would alias two of them.
methods {
    // rerouting
    function BitMaps.get(BitMaps.BitMap storage bitmap, uint256 index) internal returns bool => 
        OZ_BitMaps.get(bitmap, index);

    function BitMaps.set(BitMaps.BitMap storage bitmap, uint256 index) internal => 
        OZ_BitMaps.set(bitmap, index);

    function BitMaps.unset(BitMaps.BitMap storage bitmap, uint256 index) internal => 
        OZ_BitMaps.unset(bitmap, index);

    function BitMaps.setTo(BitMaps.BitMap storage bitmap, uint256 index, bool value) internal => 
        OZ_BitMaps.setTo(bitmap, index, value);

    // actual summaries
    function OZ_BitMaps.get(uint256 bitmap, uint256 index) internal returns bool => 
        ghost_bitmap_get[calledContract][bitmap][index];

    function OZ_BitMaps.set(uint256 bitmap, uint256 index) internal => 
        ghost_bitmap_set(calledContract, bitmap, index);

    function OZ_BitMaps.unset(uint256 bitmap, uint256 index) internal => 
        ghost_bitmap_unset(calledContract, bitmap, index);

    function OZ_BitMaps.setTo(uint256 bitmap, uint256 index, bool value) internal => 
        ghost_bitmap_setTo(calledContract, bitmap, index, value);
}

// Ghost variable to track bitmap state per contract and index
ghost mapping(address => mapping (uint256 => mapping(uint256 => bool))) ghost_bitmap_get;

// Ghost functions to update bitmap state
function ghost_bitmap_set(address contract_addr, uint256 bitmap, uint256 index) {
    ghost_bitmap_get[contract_addr][bitmap][index] = true;
}

function ghost_bitmap_unset(address contract_addr, uint256 bitmap, uint256 index) {
    ghost_bitmap_get[contract_addr][bitmap][index] = false; 
}

function ghost_bitmap_setTo(address contract_addr, uint256 bitmap, uint256 index, bool value) {
    ghost_bitmap_get[contract_addr][bitmap][index] = value;
}