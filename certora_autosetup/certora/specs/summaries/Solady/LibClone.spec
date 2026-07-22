// Summarization of Solady's LibClone (minimal-proxy / ERC-1967 clone factory library).
//
// Every LibClone deploy/clone/create/predict function creates or derives an address via raw
// create/create2/extcodecopy assembly the prover cannot reason about, so each is summarized
// as NONDET: arbitrary return values (address / (bool,address) / bytes32 code hash) with no
// modeled side effects. This is the over-approximation setups apply by hand to a proxy
// factory's deployDeterministicERC1967 / predictDeterministicAddress calls.
//
// Scope: the address-returning families (clone*, deploy*/create*ERC1967*, predict*,
// implementationOf, erc1967Bootstrap) and the bytes32 initCodeHash* family. Deliberately
// EXCLUDED: the immutable-argument readers argsOnClone/argsOnERC1967*/argLoad and the
// initCode* (bytes) builders. Those read a proxy's immutable args out of its bytecode and,
// for meaningful rules, need per-project ghosts correlating each clone to its args (a NONDET
// bytes blob would drop that correlation) — so they are left to hand-written summaries.
//
// Documented limitation: exact-address and duplicate-salt-revert properties cannot be
// verified under NONDET (the address is havoc'd).
//
// Signatures were generated from a real Solady LibClone compilation. Overloads absent from a
// given project's Solady version are dropped by the summary_resolver prune pass (name/arity)
// or auto-disabled by the TypecheckerLoop backstop (type mismatch), so listing the full set
// is safe.

methods {
    function LibClone.clone(address) internal returns (address) => NONDET;
    function LibClone.clone(address, bytes memory) internal returns (address) => NONDET;
    function LibClone.clone(uint256, address) internal returns (address) => NONDET;
    function LibClone.clone(uint256, address, bytes memory) internal returns (address) => NONDET;
    function LibClone.cloneDeterministic(address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.cloneDeterministic(address, bytes32) internal returns (address) => NONDET;
    function LibClone.cloneDeterministic(uint256, address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.cloneDeterministic(uint256, address, bytes32) internal returns (address) => NONDET;
    function LibClone.cloneDeterministic_PUSH0(address, bytes32) internal returns (address) => NONDET;
    function LibClone.cloneDeterministic_PUSH0(uint256, address, bytes32) internal returns (address) => NONDET;
    function LibClone.clone_PUSH0(address) internal returns (address) => NONDET;
    function LibClone.clone_PUSH0(uint256, address) internal returns (address) => NONDET;
    function LibClone.createDeterministicClone(address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicClone(uint256, address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967(address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967(address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967(uint256, address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967(uint256, address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967BeaconProxy(address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967BeaconProxy(address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967BeaconProxy(uint256, address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967BeaconProxy(uint256, address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967I(address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967I(address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967I(uint256, address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967I(uint256, address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967IBeaconProxy(address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967IBeaconProxy(address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967IBeaconProxy(uint256, address, bytes memory, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.createDeterministicERC1967IBeaconProxy(uint256, address, bytes32) internal returns (bool, address) => NONDET;
    function LibClone.deployDeterministicERC1967(address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967(address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967(uint256, address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967(uint256, address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967BeaconProxy(address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967BeaconProxy(address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967BeaconProxy(uint256, address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967BeaconProxy(uint256, address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967I(address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967I(address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967I(uint256, address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967I(uint256, address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967IBeaconProxy(address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967IBeaconProxy(address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967IBeaconProxy(uint256, address, bytes memory, bytes32) internal returns (address) => NONDET;
    function LibClone.deployDeterministicERC1967IBeaconProxy(uint256, address, bytes32) internal returns (address) => NONDET;
    function LibClone.deployERC1967(address) internal returns (address) => NONDET;
    function LibClone.deployERC1967(address, bytes memory) internal returns (address) => NONDET;
    function LibClone.deployERC1967(uint256, address) internal returns (address) => NONDET;
    function LibClone.deployERC1967(uint256, address, bytes memory) internal returns (address) => NONDET;
    function LibClone.deployERC1967BeaconProxy(address) internal returns (address) => NONDET;
    function LibClone.deployERC1967BeaconProxy(address, bytes memory) internal returns (address) => NONDET;
    function LibClone.deployERC1967BeaconProxy(uint256, address) internal returns (address) => NONDET;
    function LibClone.deployERC1967BeaconProxy(uint256, address, bytes memory) internal returns (address) => NONDET;
    function LibClone.deployERC1967I(address) internal returns (address) => NONDET;
    function LibClone.deployERC1967I(address, bytes memory) internal returns (address) => NONDET;
    function LibClone.deployERC1967I(uint256, address) internal returns (address) => NONDET;
    function LibClone.deployERC1967I(uint256, address, bytes memory) internal returns (address) => NONDET;
    function LibClone.deployERC1967IBeaconProxy(address) internal returns (address) => NONDET;
    function LibClone.deployERC1967IBeaconProxy(address, bytes memory) internal returns (address) => NONDET;
    function LibClone.deployERC1967IBeaconProxy(uint256, address) internal returns (address) => NONDET;
    function LibClone.deployERC1967IBeaconProxy(uint256, address, bytes memory) internal returns (address) => NONDET;
    function LibClone.erc1967Bootstrap() internal returns (address) => NONDET;
    function LibClone.erc1967Bootstrap(address) internal returns (address) => NONDET;
    function LibClone.implementationOf(address) internal returns (address) => NONDET;
    function LibClone.initCodeHash(address) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHash(address, bytes memory) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967(address) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967(address, bytes memory) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967BeaconProxy(address) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967BeaconProxy(address, bytes memory) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967Bootstrap(address) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967I(address) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967I(address, bytes memory) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967IBeaconProxy(address) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHashERC1967IBeaconProxy(address, bytes memory) internal returns (bytes32) => NONDET;
    function LibClone.initCodeHash_PUSH0(address) internal returns (bytes32) => NONDET;
    function LibClone.predictDeterministicAddress(address, bytes memory, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddress(address, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddress(bytes32, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967(address, bytes memory, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967(address, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967BeaconProxy(address, bytes memory, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967BeaconProxy(address, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967Bootstrap() internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967Bootstrap(address, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967I(address, bytes memory, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967I(address, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967IBeaconProxy(address, bytes memory, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddressERC1967IBeaconProxy(address, bytes32, address) internal returns (address) => NONDET;
    function LibClone.predictDeterministicAddress_PUSH0(address, bytes32, address) internal returns (address) => NONDET;
}
