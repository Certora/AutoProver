// SG: This is not working. We cannot use BitMaps.BitMap as a key in a ghost mapping.
methods {
    function BitMaps.get(BitMaps.BitMap storage bitmap, uint256 index) internal returns bool => 
        ghost_bitmap_get[currentContract][bitmap][index];

    function BitMaps.set(BitMaps.BitMap storage bitmap, uint256 index) internal => 
        ghost_bitmap_set(currentContract, bitmap, index);

    function BitMaps.unset(BitMaps.BitMap storage bitmap, uint256 index) internal => 
        ghost_bitmap_unset(currentContract, bitmap, index);

    function BitMaps.setTo(BitMaps.BitMap storage bitmap, uint256 index, bool value) internal => 
        ghost_bitmap_setTo(currentContract, bitmap, index, value);
}

// Ghost variable to track bitmap state per contract and index
ghost mapping(address => mapping (BitMaps.BitMap => mapping(uint256 => bool))) ghost_bitmap_get;

// Ghost functions to update bitmap state
function ghost_bitmap_set(address contract_addr, BitMaps.BitMap bitmap, uint256 index) {
    ghost_bitmap_get[contract_addr][bitmap][index] = true;
}

function ghost_bitmap_unset(address contract_addr, BitMaps.BitMap bitmap, uint256 index) {
    ghost_bitmap_get[contract_addr][bitmap][index] = false; 
}

function ghost_bitmap_setTo(address contract_addr, BitMaps.BitMap bitmap, uint256 index, bool value) {
    ghost_bitmap_get[contract_addr][bitmap][index] = value;
}