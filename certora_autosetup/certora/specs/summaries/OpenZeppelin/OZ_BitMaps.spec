// SG: We cannot use BitMaps.BitMap as a key in a ghost mapping.
// Solution: reroute to OZ_BitMaps.sol, add it to scene, and summarize its methods.
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
        ghost_bitmap_get[currentContract][bitmap][index];

    function OZ_BitMaps.set(uint256 bitmap, uint256 index) internal => 
        ghost_bitmap_set(currentContract, bitmap, index);

    function OZ_BitMaps.unset(uint256 bitmap, uint256 index) internal => 
        ghost_bitmap_unset(currentContract, bitmap, index);

    function OZ_BitMaps.setTo(uint256 bitmap, uint256 index, bool value) internal => 
        ghost_bitmap_setTo(currentContract, bitmap, index, value);
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