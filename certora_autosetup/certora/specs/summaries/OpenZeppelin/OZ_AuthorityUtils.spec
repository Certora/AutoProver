// Summarization of OpenZeppelin's AuthorityUtils.canCallWithDelay
//
// The library body is a hand-written `staticcall` into an `IAuthority` the Prover
// cannot resolve, followed by a manual decode of the returned `(bool, uint32)`:
//
//     (bool immediate, uint32 delay) = IAuthority(authority).canCall(caller, target, selector)
//
// It is the gate behind OpenZeppelin AccessManager's `restricted` modifier, so it sits
// on the entry path of every access-controlled function in an AccessManaged contract.
// Left unsummarized the raw call leaves the return buffer unconstrained, which the
// Prover explores as spurious reverts and havoc.
//
// The authority is an arbitrary external contract, so the sound model is an arbitrary
// *but consistent* function of everything the real call depends on: the authority
// address, the caller, the target and the selector. The ghosts below are unconstrained
// -- they can take any value, including the `(false, 0)` that the real library returns
// when the staticcall fails or returns short data -- so no behaviour of a real authority
// is excluded.
//
// Do NOT key these ghosts on fewer arguments. Dropping `target` or `selector` forces one
// answer across call sites that a real authority can answer differently, which removes
// reachable behaviour and can make an access-control rule pass vacuously.

methods {
    // A wildcard entry may not declare return types in the signature; the summary's
    // result type is given by `expect` instead.
    function _.canCallWithDelay(
        address authority, address caller, address target, bytes4 selector
    ) internal =>
        cvl_canCallWithDelay(authority, caller, target, selector) expect (bool, uint32);
}

/// Whether `caller` may call `target.selector` immediately, per `authority`.
ghost mapping(address =>
       mapping(address =>
       mapping(address =>
       mapping(bytes4 => bool)))) cvl_authorityImmediate;

/// The execution delay `authority` imposes on that call, in seconds.
ghost mapping(address =>
       mapping(address =>
       mapping(address =>
       mapping(bytes4 => uint32)))) cvl_authorityDelay;

function cvl_canCallWithDelay(
    address authority, address caller, address target, bytes4 selector
) returns (bool, uint32) {
    return (
        cvl_authorityImmediate[authority][caller][target][selector],
        cvl_authorityDelay[authority][caller][target][selector]
    );
}
