// Summarization of Solady's CREATE3 deterministic-deployment library.
//
// CREATE3 deploys via raw create/create2 assembly the prover cannot reason about, so each
// function is summarized as NONDET: it returns an arbitrary address and models no side
// effects. This is the same over-approximation setups apply by hand to a factory's
// Clones.cloneDeterministic / predictDeterministicAddress calls.
//
// Documented limitation (inherent to the summary, not a bug): properties about the exact
// deterministic address, or about duplicate-salt reverts (guards of the form
// `predictDeterministicAddress(salt).code.length > 0`), cannot be verified — the address
// is havoc'd, so such guards fire nondeterministically.
//
// deployDeterministic's argument order changed across Solady versions ((initCode, salt) in
// older releases, (salt, initCode) in newer ones); both orders are listed. A project only
// has one of them: the summary_resolver prune pass keeps entries by (receiver, name, arity),
// and the TypecheckerLoop backstop auto-disables whichever overload is not in the project's
// actual code — so listing both is safe (the non-matching one becomes inert).

methods {
    // predictDeterministicAddress — signatures are stable across Solady versions.
    function CREATE3.predictDeterministicAddress(bytes32 salt) internal returns (address) => NONDET;
    function CREATE3.predictDeterministicAddress(bytes32 salt, address deployer) internal returns (address) => NONDET;

    // deployDeterministic — older Solady order (initCode, salt).
    function CREATE3.deployDeterministic(bytes memory initCode, bytes32 salt) internal returns (address) => NONDET;
    function CREATE3.deployDeterministic(uint256 value, bytes memory initCode, bytes32 salt) internal returns (address) => NONDET;

    // deployDeterministic — newer Solady order (salt, initCode).
    function CREATE3.deployDeterministic(bytes32 salt, bytes memory initCode) internal returns (address) => NONDET;
    function CREATE3.deployDeterministic(uint256 value, bytes32 salt, bytes memory initCode) internal returns (address) => NONDET;
}
