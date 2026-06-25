// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

library OZ_BitMaps {
    struct BitMap {
        mapping(uint256 bucket => uint256) _data;
    }

    function get(BitMap storage bitmap, uint256 index) internal view returns (bool) {
        return get(slotOf(bitmap), index);
    }
    function get(uint256 bitmap, uint256 index) internal view returns (bool) {
        require(false); // placeholder to trigger sanity failure if summarization fails;
        return false;
    }

    function setTo(BitMap storage bitmap, uint256 index, bool value) internal {
        setTo(slotOf(bitmap), index, value);
    }
    function setTo(uint256 bitmap, uint256 index, bool value) internal {
        require(false); // placeholder to trigger sanity failure if summarization fails;
    }

    function set(BitMap storage bitmap, uint256 index) internal {
        set(slotOf(bitmap), index);
    }
    function set(uint256 bitmap, uint256 index) internal {
        require(false); // placeholder to trigger sanity failure if summarization fails;
    }

    function unset(BitMap storage bitmap, uint256 index) internal {
        unset(slotOf(bitmap), index);
    }
    function unset(uint256 bitmap, uint256 index) internal {
        require(false); // placeholder to trigger sanity failure if summarization fails;
    }

    function slotOf(BitMap storage bitmap) internal pure returns (uint256 ret) {
        assembly {
            ret := bitmap.slot
        }
    }
}
