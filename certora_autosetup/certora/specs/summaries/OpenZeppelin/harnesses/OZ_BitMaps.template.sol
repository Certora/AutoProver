// SPDX-License-Identifier: MIT
$PRAGMA$

// BitMaps.BitMap cannot be a ghost mapping key, so OZ_BitMaps.spec reroutes the library's
// calls here and summarizes these functions instead, keying the ghost on the storage slot.
//
// The import is what makes the reroute resolve. A reroute host's parameter must carry the
// same canonicalId as the summarized function's, and canonicalId is
// "<file resolved under .certora_sources>|<qualified name>" — so BitMaps.BitMap has to come
// from the very BitMaps.sol the project compiles. A copy of the struct is a different type,
// however identical it looks. That path differs per project, which is why this is a template.
import {BitMaps} from "$BITMAPS_IMPORT$";

library OZ_BitMaps {
    // Reroute targets, and external for a reason: the Prover keeps a candidate only when its
    // evmExternalMethodInfo reports a library function, and that is populated for EXTERNAL
    // visibility alone. External is also the only place a storage parameter can be bound.
    function get(BitMaps.BitMap storage bitmap, uint256 index) external view returns (bool) {
        return get(slotOf(bitmap), index);
    }

    function set(BitMaps.BitMap storage bitmap, uint256 index) external {
        set(slotOf(bitmap), index);
    }

    function unset(BitMaps.BitMap storage bitmap, uint256 index) external {
        unset(slotOf(bitmap), index);
    }

    function setTo(BitMaps.BitMap storage bitmap, uint256 index, bool value) external {
        setTo(slotOf(bitmap), index, value);
    }

    // The summarized functions. CVL replaces each body with a ghost read or write, so the
    // bodies below only run when a summary failed to attach — the distinct revert strings say
    // which one. They are distinct for a second reason: four identical always-reverting bodies
    // are a merge candidate for solc, and merging them collapses the per-function markers the
    // summarizer keys on.
    function get(uint256 bitmap, uint256 index) internal view returns (bool) {
        require(false, "OZ_BitMaps.get summary not applied");
        return false;
    }

    function set(uint256 bitmap, uint256 index) internal {
        require(false, "OZ_BitMaps.set summary not applied");
    }

    function unset(uint256 bitmap, uint256 index) internal {
        require(false, "OZ_BitMaps.unset summary not applied");
    }

    function setTo(uint256 bitmap, uint256 index, bool value) internal {
        require(false, "OZ_BitMaps.setTo summary not applied");
    }

    // The ghost is keyed on the slot, which is what stands in for the BitMap identity.
    function slotOf(BitMaps.BitMap storage bitmap) internal pure returns (uint256 ret) {
        assembly { ret := bitmap.slot }
    }
}
