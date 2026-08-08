"""Data model for the library harnesser.

A ``library`` cannot be the Certora Prover's verification target: parametric rules
filter libraries out of the method set, and CVL rejects direct library calls from a
spec. The harnesser generates a plain contract that calls the library, so the library's
bodies get inlined into a verifiable target.

The flow is ``LibraryApi`` (what the build says the library exposes) → ``HarnessPlan``
(what we will emit, decided) → Solidity text. ``LibraryApi`` mirrors the build JSON;
``HarnessPlan`` is fully resolved, so rendering is a pure formatting step with no
decisions left in it.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class SkipReason(Enum):
    """Why a library function got no wrapper.

    Every skip is reported to the user: a library whose interesting half is skipped
    yields a harness that verifies nothing, and that has to be visible rather than
    silently counted as coverage.
    """

    PRIVATE = "private"
    STORAGE_POINTER_RETURN = "storage_pointer_return"
    INTERNAL_ONLY_TYPE = "internal_only_type"
    UNRESOLVED_TYPE = "unresolved_type"
    OPAQUE_HANDLE = "opaque_handle"
    ASSEMBLY_TERMINATOR = "assembly_terminator"
    CONSTRUCTOR = "constructor"


#: Data locations as they appear in the build JSON's per-argument ``location`` field.
LOC_STORAGE = "storage"
LOC_MEMORY = "memory"
LOC_CALLDATA = "calldata"


@dataclass(frozen=True)
class LibParam:
    """One parameter or return value of a library function.

    ``solidity_type`` is already rendered source text (via ``parse_type_descriptor`` in
    SOLIDITY mode), so it carries the qualification the harness needs — e.g.
    ``EnumerableSet.Bytes32Set``, not a bare ``Bytes32Set``.
    """

    name: str
    solidity_type: str
    location: str = ""
    #: Raw ``typeDesc.type`` from the build. Retained because some kinds have no Solidity
    #: rendering at all — a ``Function`` type is internal-only and renders as nothing —
    #: and the reason a function was skipped should say which case it was.
    desc_kind: str = ""

    @property
    def is_storage(self) -> bool:
        return self.location == LOC_STORAGE

    @property
    def is_reference(self) -> bool:
        return self.location in (LOC_STORAGE, LOC_MEMORY, LOC_CALLDATA)


#: How a struct member is shaped, which decides how a reader reaches through it.
KIND_VALUE = "value"
KIND_STRUCT = "struct"
KIND_MAPPING = "mapping"
KIND_ARRAY = "array"


@dataclass(frozen=True)
class MemberNode:
    """One node of an owned struct's member tree.

    Kept as a tree rather than a flat type string because storage readers are derived
    by walking it: a mapping contributes a key parameter, an array an index parameter,
    a nested struct just a longer access path. OpenZeppelin's ``_indexOf`` — the reader
    its own spec needs — is the leaf of ``AddressSet -> _inner -> _indexes[key]``.
    """

    name: str
    solidity_type: str
    kind: str
    key_type: str = ""
    value: Optional["MemberNode"] = None
    members: tuple["MemberNode", ...] = ()


@dataclass(frozen=True)
class LibFunction:
    """One function declared by the library, as the build reports it."""

    name: str
    visibility: str
    state_mutability: str
    params: tuple[LibParam, ...]
    returns: tuple[LibParam, ...]
    source_line: int = 0

    @property
    def storage_params(self) -> tuple[LibParam, ...]:
        return tuple(p for p in self.params if p.is_storage)


@dataclass(frozen=True)
class LibraryApi:
    """Everything the harnesser knows about the library it is wrapping.

    ``source_file`` is the path the build reported, which is what the harness imports
    and what disambiguates same-named libraries (solady ships 17 library names twice,
    under ``src/utils/`` and ``src/utils/g/``).
    """

    name: str
    source_file: str
    functions: tuple[LibFunction, ...]
    #: Qualified struct type (e.g. "EnumerableSet.AddressSet") -> its member tree, as
    #: the build reports it. Storage readers are derived from this; member names are
    #: never hardcoded because they change across library versions (OpenZeppelin
    #: renamed EnumerableSet's ``_indexes`` to ``_positions`` in v5).
    struct_members: Dict[str, tuple[MemberNode, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class OwnedVar:
    """A state variable the harness declares so it can supply a ``storage`` argument.

    A library function taking ``EnumerableSet.AddressSet storage`` has no callable
    external form: the caller cannot construct a storage pointer. The harness owns one
    instance per distinct storage type and binds it, which is also what makes the
    wrappers stateful enough to state invariants over.
    """

    var_name: str
    solidity_type: str


@dataclass(frozen=True)
class Wrapper:
    """One public function the harness will expose."""

    #: Name after CVL-keyword renaming and ABI-collision mangling.
    name: str
    library_function: str
    params: tuple[LibParam, ...]
    returns: tuple[LibParam, ...]
    state_mutability: str
    #: Positional call arguments, already resolved: either a wrapper parameter name or
    #: an owned state variable name.
    call_args: tuple[str, ...]
    #: Set when the library function mutates a memory reference in place and returns
    #: nothing; the wrapper returns the mutated argument so the effect is observable
    #: across the ABI boundary.
    returns_mutated_param: Optional[str] = None


@dataclass(frozen=True)
class StorageReader:
    """A getter over a member of an owned struct.

    Properties worth verifying are usually about the library's internal
    representation — OpenZeppelin's own EnumerableSet spec needs ``_indexOf`` to relate
    ``at(i)`` back to the index. Without readers the harness only echoes its own API.
    """

    name: str
    solidity_type: str
    access_expression: str
    params: tuple[LibParam, ...] = ()


@dataclass(frozen=True)
class Skipped:
    """A library function that got no wrapper, and why."""

    library_function: str
    reason: SkipReason
    detail: str = ""


@dataclass(frozen=True)
class HarnessPlan:
    """The fully-resolved decision of what the harness file contains."""

    harness_name: str
    library_name: str
    library_source_file: str
    harness_file: str
    pragma_line: str
    extra_pragma_lines: tuple[str, ...]
    import_lines: tuple[str, ...]
    owned_vars: tuple[OwnedVar, ...]
    wrappers: tuple[Wrapper, ...]
    readers: tuple[StorageReader, ...]
    skipped: tuple[Skipped, ...]

    @property
    def coverage(self) -> Dict[str, int]:
        wrapped = len(self.wrappers)
        return {
            "total": wrapped + len(self.skipped),
            "wrapped": wrapped,
            "skipped": len(self.skipped),
            "readers": len(self.readers),
        }


class LibraryHarnessError(Exception):
    """The harness could not be generated.

    Raised rather than degraded: a harness missing the wrappers the user cares about
    still runs, still reports "verified", and proves nothing.
    """
