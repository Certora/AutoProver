methods {
    function BytesLib.toAddress(bytes memory b, uint256 start) internal returns (address) => byteslib_toAddressSummary(b, start);
    function BytesLib.toUint8(bytes memory b, uint256 start) internal returns (uint8) => byteslib_toUint8Summary(b, start);
    function BytesLib.toUint16(bytes memory b, uint256 start) internal returns (uint16) => byteslib_toUint16Summary(b, start);
    function BytesLib.toUint32(bytes memory b, uint256 start) internal returns (uint32) => byteslib_toUint32Summary(b, start);
    function BytesLib.toUint64(bytes memory b, uint256 start) internal returns (uint64) => byteslib_toUint64Summary(b, start);
    function BytesLib.toUint96(bytes memory b, uint256 start) internal returns (uint96) => byteslib_toUint96Summary(b, start);
    function BytesLib.toUint128(bytes memory b, uint256 start) internal returns (uint128) => byteslib_toUint128Summary(b, start);
}

ghost mapping(bytes => mapping(uint256 => address)) toAddress_result;
ghost mapping(bytes => mapping(uint256 => uint8)) toUint8_result;
ghost mapping(bytes => mapping(uint256 => uint16)) toUint16_result;
ghost mapping(bytes => mapping(uint256 => uint32)) toUint32_result;
ghost mapping(bytes => mapping(uint256 => uint64)) toUint64_result;
ghost mapping(bytes => mapping(uint256 => uint96)) toUint96_result;
ghost mapping(bytes => mapping(uint256 => uint128)) toUint128_result;



function byteslib_toAddressSummary(bytes b, uint256 start) returns (address) {
    require b.length >= start + 20;
    return toAddress_result[b][start];
}


function byteslib_toUint8Summary(bytes b, uint256 start) returns (uint8) {
    require b.length >= start + 1;
    return toUint8_result[b][start];
}

function byteslib_toUint16Summary(bytes b, uint256 start) returns (uint16) {
    require b.length >= start + 2;
    return toUint16_result[b][start];
}

function byteslib_toUint32Summary(bytes b, uint256 start) returns (uint32) {
    require b.length >= start + 4;
    return toUint32_result[b][start];
}

function byteslib_toUint64Summary(bytes b, uint256 start) returns (uint64) {
    require b.length >= start + 8;
    return toUint64_result[b][start];
}

function byteslib_toUint96Summary(bytes b, uint256 start) returns (uint96) {
    require b.length >= start + 12;
    return toUint96_result[b][start];
}

function byteslib_toUint128Summary(bytes b, uint256 start) returns (uint128) {
    require b.length >= start + 16;
    return toUint128_result[b][start];
}