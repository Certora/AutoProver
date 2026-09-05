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

    // The summarized functions. CVL replaces each body with a ghost read or write, so what is
    // written here only runs if a summary failed to attach.
    //
    // These were `require(false)` tripwires, meant to make that failure loud. They made it
    // certain instead: a body that unconditionally reverts leaves nothing for the summary to
    // attach to, so every call reverted and every rule over it passed vacuously. The tripwire
    // caused the failure it was meant to announce.
    //
    // Neutral bodies invert that. An unattached summary now means `set` stores nothing and
    // `get` reads false, so a rule that sets a bit and reads it back fails outright. A
    // violated rule is a far better failure than a green vacuous one, and `rule_sanity`
    // catches what is left.
    function get(uint256 bitmap, uint256 index) internal view returns (bool) {
        return false;
    }

    function set(uint256 bitmap, uint256 index) internal {}

    function unset(uint256 bitmap, uint256 index) internal {}

    function setTo(uint256 bitmap, uint256 index, bool value) internal {}

    // The ghost is keyed on the slot, which is what stands in for the BitMap identity.
    function slotOf(BitMaps.BitMap storage bitmap) internal pure returns (uint256 ret) {
        assembly { ret := bitmap.slot }
    }
}
