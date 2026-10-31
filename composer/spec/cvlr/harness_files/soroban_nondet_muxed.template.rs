
// `MuxedAddress` arrived in soroban-sdk 23.
use soroban_sdk::MuxedAddress;

pub fn nondet_muxedaddress() -> MuxedAddress {
    MuxedAddress::from(nondet_address())
}
