// Proxy upgrade entrypoint, library-agnostic: applies to any contract's upgradeToAndCall(address,
// bytes) — OpenZeppelin (ERC1967Utils / UUPSUpgradeable) and Solady (UUPSUpgradeable) alike. The
// upgrade mechanics are removed from analysis (DELETE) and havoced, since verification targets the
// access-control properties of upgrades, not their post-state.
methods {
    function _.upgradeToAndCall(address, bytes) external => HAVOC_ALL DELETE;
}
