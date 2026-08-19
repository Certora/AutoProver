"""Decide what the harness contains: classify, own, mangle, order.

Pure functions over a ``LibraryApi``. Every decision is made here so that rendering is
plain formatting and the whole thing is testable without solc, disk or network.

The shape of the problem: a library function is written for an internal caller, and the
harness has to re-expose it across an ABI boundary. Three things do not survive that
crossing, and each has one right answer:

- A ``storage`` parameter cannot be passed in — an external caller has no way to build a
  storage pointer. The harness owns an instance of that type and binds it, which is also
  what gives the harness state to state invariants over.
- Some types cannot appear in a public signature at all (a struct containing a mapping,
  an internal function type). Those functions are skipped and reported.
- A function that mutates a ``memory`` argument in place and returns nothing is the
  identity function once it is called externally, because the callee mutates a fresh
  copy. The wrapper returns the mutated argument so the effect is observable.

Some library functions compile perfectly as wrappers and are still wrong to emit; those
are skipped deliberately, see ``_opaque_handle_reason``.
"""

import re
from collections import defaultdict
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from certora_autosetup.harnesser.cvl_reserved import escape_reserved
from certora_autosetup.harnesser.model import (
    KIND_ARRAY,
    KIND_MAPPING,
    KIND_STRUCT,
    KIND_VALUE,
    LOC_MEMORY,
    MemberNode,
    HarnessPlan,
    LibFunction,
    LibParam,
    LibraryApi,
    LibraryHarnessError,
    OwnedVar,
    Skipped,
    SkipReason,
    StorageReader,
    Wrapper,
)

#: Turns any type expression into an identifier fragment usable in a function name.
#: Needed because a mangling suffix may be derived from a raw mapping type
#: (``mapping(uint256 => uint256)``), which is a real storage receiver in Solady's LibMap.
_NON_IDENTIFIER = re.compile(r"[^A-Za-z0-9]+")

#: A parameter type solc will not accept in a public/external signature. ``function``
#: types are internal-only; a mapping cannot cross the ABI boundary in any position.
_INTERNAL_ONLY_MARKERS = ("function(", "mapping(")

#: ``typeDesc.type`` of an internal function type, which has no Solidity rendering.
_FUNCTION_TYPE_KIND = "Function"

#: Bounds on reader derivation. Depth stops a self-referential struct; the count keeps a
#: deeply nested representation from burying the library's own API in getters.
_MAX_READER_DEPTH = 4
_MAX_READERS = 24


def sanitize_identifier(text: str) -> str:
    """Collapse a type expression into a name fragment."""
    return _NON_IDENTIFIER.sub("_", text).strip("_")


def _is_internal_only(solidity_type: str) -> bool:
    return any(marker in solidity_type for marker in _INTERNAL_ONLY_MARKERS)


def _node_contains_mapping(node: MemberNode, depth: int = 0) -> bool:
    if depth > 8 or node.kind == KIND_MAPPING:
        return True
    if node.kind == KIND_STRUCT:
        return any(_node_contains_mapping(child, depth + 1) for child in node.members)
    if node.kind == KIND_ARRAY and node.value is not None:
        return _node_contains_mapping(node.value, depth + 1)
    return False


def _contains_mapping(
    solidity_type: str, struct_members: Mapping[str, tuple[MemberNode, ...]]
) -> bool:
    """Whether a type transitively contains a mapping, and so cannot cross the ABI.

    Struct members are expanded recursively: OpenZeppelin's ``Bytes32ToBytes32Map``
    holds an ``EnumerableSet.Bytes32Set``, which holds a mapping, so a getter returning
    the outer struct is rejected by solc even though its own members look harmless.
    """
    if "mapping(" in solidity_type:
        return True
    base = solidity_type.replace("[]", "").strip()
    members = struct_members.get(base)
    if not members:
        return False
    return any(_node_contains_mapping(m) for m in members)


def _opaque_handle_reason(fn: LibFunction) -> Optional[str]:
    """Whether this function traffics in pointers that are meaningless across a call.

    Solady's RedBlackTreeLib returns a ``bytes32 ptr`` that encodes a storage offset, and
    JSONParserLib passes an ``Item`` whose single ``uint256`` is a memory address. Wrapped
    naively they compile, and then the Prover is free to invent a pointer value — which
    produces counterexamples that cannot happen in reality. A skipped function is a
    visible gap; an unsound one is a false bug report to the user.
    """
    for param in fn.params:
        if param.name in ("ptr", "pointer") and param.solidity_type == "bytes32":
            return f"parameter '{param.name}' is a library-internal pointer"
    return None


def _classify(
    fn: LibFunction, struct_members: Mapping[str, tuple[MemberNode, ...]]
) -> Optional[Skipped]:
    """Return why ``fn`` gets no wrapper, or None if it is wrappable. First match wins."""
    if fn.visibility == "private":
        # Private functions are unreachable from outside the library by construction.
        # No semantic loss in these corpora: the internal functions that call them
        # expose the same behavior.
        return Skipped(fn.name, SkipReason.PRIVATE)

    for param in (*fn.params, *fn.returns):
        if param.solidity_type:
            continue
        if param.desc_kind == _FUNCTION_TYPE_KIND:
            # An internal function type has no external form at all. OpenZeppelin's
            # Checkpoints.push takes a comparator this way; it is the single function
            # hardhat-exposed also declines to expose.
            return Skipped(
                fn.name, SkipReason.INTERNAL_ONLY_TYPE, f"'{param.name}' is an internal function type"
            )
        return Skipped(fn.name, SkipReason.UNRESOLVED_TYPE, f"parameter '{param.name}'")

    for param in fn.params:
        if param.is_storage:
            continue
        if _is_internal_only(param.solidity_type) or _contains_mapping(
            param.solidity_type, struct_members
        ):
            return Skipped(
                fn.name, SkipReason.INTERNAL_ONLY_TYPE, f"{param.solidity_type} {param.name}"
            )

    for ret in fn.returns:
        if ret.is_storage:
            # Copying a storage pointer to memory compiles and silently drops the write
            # path, so callers could read the slot but never assign to it. The whole of
            # OpenZeppelin's StorageSlot is this shape.
            return Skipped(
                fn.name, SkipReason.STORAGE_POINTER_RETURN, f"returns {ret.solidity_type} storage"
            )
        if _is_internal_only(ret.solidity_type) or _contains_mapping(
            ret.solidity_type, struct_members
        ):
            return Skipped(fn.name, SkipReason.INTERNAL_ONLY_TYPE, f"returns {ret.solidity_type}")

    if reason := _opaque_handle_reason(fn):
        return Skipped(fn.name, SkipReason.OPAQUE_HANDLE, reason)

    return None


def _external_location(param: LibParam) -> str:
    """The data location this parameter takes in the wrapper's signature.

    Reference parameters stay ``memory``: ``calldata`` would compile but leaves nothing
    to return for an in-place mutator, and the ABI encoding is identical either way.
    """
    return LOC_MEMORY if param.is_reference else ""


def short_type_name(solidity_type: str, library_name: str) -> str:
    """Identifier fragment for a type, without the wrapped library's own qualifier.

    Every storage receiver belongs to the library being wrapped, so repeating its name in
    each generated identifier only makes them long: ``at_Bytes32Set`` says as much as
    ``at_EnumerableSet_Bytes32Set`` and reads like the hand-written harnesses it sits
    beside. A type from elsewhere keeps its qualifier, which is what still tells it apart.
    """
    unqualified = solidity_type
    prefix = f"{library_name}."
    if unqualified.startswith(prefix):
        unqualified = unqualified[len(prefix):]
    return sanitize_identifier(unqualified)


def _owned_var_name(solidity_type: str, library_name: str) -> str:
    """Name the harness state variable that backs a given storage type."""
    return f"_certoraStore_{short_type_name(solidity_type, library_name)}"


def _mutated_memory_param(fn: LibFunction) -> Optional[LibParam]:
    """The memory argument a void function mutates in place, if that is its whole effect.

    Solady's LibSort is 16 such functions (``sort``, ``reverse``, ``uniquifySorted``,
    ``insertionSort``): ``internal pure``, no return, mutating the array they are given.
    Called across the ABI the callee mutates a decoded copy, so without a synthesized
    return the wrapper is observationally the identity function.
    """
    if fn.returns:
        return None
    memory_refs = [p for p in fn.params if p.location == LOC_MEMORY]
    if len(memory_refs) != 1:
        return None
    return memory_refs[0]


def _wrapper_mutability(fn: LibFunction, binds_state: bool) -> str:
    """Mutability of the wrapper, widened when it reads harness state.

    A ``pure`` library function becomes ``view`` once the harness feeds it a state
    variable, because the wrapper now reads storage the library function did not.
    """
    if binds_state and fn.state_mutability == "pure":
        return "view"
    return fn.state_mutability


def _build_wrapper(
    fn: LibFunction, owned: Dict[str, OwnedVar]
) -> Wrapper:
    """Turn a wrappable library function into an exposed harness function."""
    exposed: List[LibParam] = []
    call_args: List[str] = []
    binds_state = False

    for param in fn.params:
        if param.is_storage:
            var = owned[param.solidity_type]
            call_args.append(var.var_name)
            binds_state = True
            continue
        exposed.append(
            LibParam(
                name=param.name,
                solidity_type=param.solidity_type,
                location=_external_location(param),
            )
        )
        call_args.append(param.name)

    returns = tuple(
        LibParam(
            name=ret.name,
            solidity_type=ret.solidity_type,
            location=_external_location(ret),
        )
        for ret in fn.returns
    )

    mutated = _mutated_memory_param(fn)
    if mutated is not None:
        returns = (
            LibParam(name="", solidity_type=mutated.solidity_type, location=LOC_MEMORY),
        )

    return Wrapper(
        name=fn.name,
        library_function=fn.name,
        params=tuple(exposed),
        returns=returns,
        state_mutability=_wrapper_mutability(fn, binds_state),
        call_args=tuple(call_args),
        returns_mutated_param=mutated.name if mutated is not None else None,
    )


def _abi_key(wrapper: Wrapper) -> Tuple[str, ...]:
    """The signature solc uses to detect a duplicate definition.

    Return types are excluded deliberately — they are not part of the key, which is why
    OpenZeppelin's three ``values()`` overloads collide once their storage receivers are
    dropped.
    """
    return (wrapper.name, *(p.solidity_type for p in wrapper.params))


def _suffixed(name: str, suffix: str) -> str:
    """Append a mangling suffix without doubling the separator.

    A CVL-escaped name already ends in ``_`` (``at`` becomes ``at_``), so the naive join
    would produce ``at__AddressSet``.
    """
    return f"{name}{suffix}" if name.endswith("_") else f"{name}_{suffix}"


def _mangle_collisions(
    wrappers: Sequence[Wrapper], originals: Sequence[LibFunction], library_name: str
) -> List[Wrapper]:
    """Give colliding wrappers distinct names, leaving unique ones untouched.

    Dropping the storage receiver is what creates the collisions: OpenZeppelin's
    ``length(Bytes32Set)``, ``length(AddressSet)`` and ``length(UintSet)`` all become
    ``length()``. The receiver type is therefore what distinguishes them again.

    ``originals`` is positional, not keyed by name: overloads share a name, so a lookup
    by name would hand every member of a collision group the same receiver and rename
    them all identically — reproducing the collision it set out to remove.

    A group that collides with no storage receiver to name falls back to an ordinal, and
    the whole pass repeats until no key is shared, so a suffix that happens to recreate
    a collision still converges.
    """
    resolved = list(wrappers)
    for _ in range(len(resolved) + 1):
        grouped: Dict[Tuple[str, ...], List[int]] = defaultdict(list)
        for index, wrapper in enumerate(resolved):
            grouped[_abi_key(wrapper)].append(index)

        colliding = [indices for indices in grouped.values() if len(indices) > 1]
        if not colliding:
            return resolved

        for indices in colliding:
            for ordinal, index in enumerate(indices):
                wrapper = resolved[index]
                receivers = originals[index].storage_params
                if receivers:
                    suffix = "_".join(
                        short_type_name(p.solidity_type, library_name) for p in receivers
                    )
                else:
                    suffix = str(ordinal)
                resolved[index] = Wrapper(
                    name=_suffixed(wrapper.name, suffix),
                    library_function=wrapper.library_function,
                    params=wrapper.params,
                    returns=wrapper.returns,
                    state_mutability=wrapper.state_mutability,
                    call_args=wrapper.call_args,
                    returns_mutated_param=wrapper.returns_mutated_param,
                )

    raise LibraryHarnessError(
        "could not give every wrapper a unique signature; names still collide after "
        "repeated mangling"
    )


def _walk_member(
    node: MemberNode,
    access: str,
    name_parts: List[str],
    params: List[LibParam],
    readers: List[StorageReader],
    depth: int,
) -> None:
    """Reach through one member towards an ABI-encodable leaf, emitting a reader there.

    A struct extends the access path; a mapping or array adds a lookup parameter and
    descends into the value. The leaf is what can actually cross the ABI boundary — so
    a mapping is never returned, but the value it holds is.
    """
    if depth > _MAX_READER_DEPTH or len(readers) >= _MAX_READERS:
        return

    if node.kind == KIND_STRUCT:
        for child in node.members:
            _walk_member(
                child,
                f"{access}.{child.name}",
                [*name_parts, child.name],
                params,
                readers,
                depth + 1,
            )
        return

    if node.kind in (KIND_MAPPING, KIND_ARRAY) and node.value is not None:
        key_type = node.key_type if node.kind == KIND_MAPPING else "uint256"
        key_name = f"key{len(params)}"
        _walk_member(
            node.value,
            f"{access}[{key_name}]",
            name_parts,
            [*params, LibParam(name=key_name, solidity_type=key_type)],
            readers,
            depth + 1,
        )
        return

    if node.kind != KIND_VALUE:
        return

    readers.append(
        StorageReader(
            name="_".join(sanitize_identifier(part) for part in name_parts if part),
            solidity_type=node.solidity_type,
            access_expression=access,
            params=tuple(params),
        )
    )


def _storage_readers(
    owned: Sequence[OwnedVar],
    struct_members: Mapping[str, tuple[MemberNode, ...]],
    library_name: str,
) -> List[StorageReader]:
    """Expose the owned structs' representation so a spec can state properties about it.

    Without these the harness only echoes the library's own API, and the interesting
    properties are exactly the ones relating the API to the representation —
    OpenZeppelin's own EnumerableSet spec needs ``_indexOf`` to say that ``at(i)`` maps
    back to index ``i``. That reader is the leaf of ``AddressSet -> _inner -> _indexes[key]``,
    which is why the walk descends through mappings instead of refusing them.
    """
    readers: List[StorageReader] = []
    for var in owned:
        # Named after the type and member path rather than the state variable, so a
        # reader reads as an accessor (``Bytes32Set_inner_indexes``) instead of repeating
        # the storage plumbing in every name.
        root = short_type_name(var.solidity_type, library_name)
        for member in struct_members.get(var.solidity_type, ()):
            _walk_member(
                member,
                f"{var.var_name}.{member.name}",
                [root, member.name],
                [],
                readers,
                0,
            )
    return readers


def build_plan(
    api: LibraryApi,
    harness_name: str,
    harness_file: str,
    pragma_line: str,
    import_lines: Sequence[str],
    extra_pragma_lines: Sequence[str] = (),
) -> HarnessPlan:
    """Decide the complete contents of the harness for ``api``.

    Ordering is by library source line, so regenerating an unchanged library produces a
    byte-identical file and the harness does not churn in diffs.
    """
    ordered = sorted(api.functions, key=lambda f: (f.source_line, f.name))

    skipped: List[Skipped] = []
    wrappable: List[LibFunction] = []
    for fn in ordered:
        if reason := _classify(fn, api.struct_members):
            skipped.append(reason)
        else:
            wrappable.append(fn)

    if not wrappable:
        raise LibraryHarnessError(
            f"no function of library {api.name} can be exposed through a harness "
            f"({len(skipped)} skipped) — verifying it would prove nothing"
        )

    owned: Dict[str, OwnedVar] = {}
    for fn in wrappable:
        for param in fn.storage_params:
            if param.solidity_type not in owned:
                owned[param.solidity_type] = OwnedVar(
                    var_name=_owned_var_name(param.solidity_type, api.name),
                    solidity_type=param.solidity_type,
                )

    wrappers = [_build_wrapper(fn, owned) for fn in wrappable]
    # CVL renaming first: doing it after collision mangling could rename a wrapper onto
    # a name the mangling had just handed out.
    wrappers = [
        Wrapper(
            name=escape_reserved(w.name),
            library_function=w.library_function,
            params=w.params,
            returns=w.returns,
            state_mutability=w.state_mutability,
            call_args=w.call_args,
            returns_mutated_param=w.returns_mutated_param,
        )
        for w in wrappers
    ]
    wrappers = _mangle_collisions(wrappers, wrappable, api.name)

    owned_vars = tuple(owned[key] for key in sorted(owned))
    return HarnessPlan(
        harness_name=harness_name,
        library_name=api.name,
        library_source_file=api.source_file,
        harness_file=harness_file,
        pragma_line=pragma_line,
        extra_pragma_lines=tuple(extra_pragma_lines),
        import_lines=tuple(import_lines),
        owned_vars=owned_vars,
        wrappers=tuple(wrappers),
        readers=tuple(_storage_readers(owned_vars, api.struct_members, api.name)),
        skipped=tuple(skipped),
    )
