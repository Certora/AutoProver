
// From soroban-sdk 23 a transfer's `to` is a `MuxedAddress`; the token moves the balance of the
// address it wraps.
use soroban_sdk::MuxedAddress;

impl Recipient for MuxedAddress {
    fn recipient(self) -> Address {
        self.address()
    }
}

impl Recipient for &MuxedAddress {
    fn recipient(self) -> Address {
        self.address()
    }
}
