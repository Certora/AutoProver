"""CVL reserved words that a generated wrapper name must avoid.

A wrapper is only useful if a spec can name it. CVL's grammar reserves words that are
perfectly legal Solidity function names, so a mechanically-wrapped library hits them
routinely: ``at`` appears 41 times across OpenZeppelin and Solady, ``sort`` 4, ``exists``
3. A wrapper called ``at`` compiles and then makes the spec unparseable.

The list is the identifier-shaped terminal set of the CVL grammar, transcribed from
``TerminalId.kt`` in the Prover repo. It is vendored rather than derived because the
harnesser has no access to the Prover's sources at runtime; it is a closed grammar, so
it changes rarely, and an entry that disappears only costs one needless rename.

The escape is a trailing underscore, which is what OpenZeppelin's hand-written Certora
harness uses (``at_``), so generated specs read like the human-written ones.
"""

from typing import FrozenSet

#: Identifier-shaped terminals of the CVL grammar. Operators and punctuation are
#: omitted: they cannot collide with a Solidity function name.
CVL_RESERVED_WORDS: FrozenSet[str] = frozenset(
    {
        "ALL",
        "ALWAYS",
        "ASSERT_FALSE",
        "AUTO",
        "CONSTANT",
        "Create",
        "DELETE",
        "DISPATCH",
        "DISPATCHER",
        "EOF",
        "HAVOC_ALL",
        "HAVOC_ECF",
        "NONDET",
        "PER_CALLEE_CONSTANT",
        "STORAGE",
        "Sload",
        "Sstore",
        "Tload",
        "Tstore",
        "UNRESOLVED",
        "as",
        "assert",
        "assuming",
        "at",
        "axiom",
        "builtin",
        "default",
        "definition",
        "description",
        "else",
        "error",
        "event",
        "exists",
        "expect",
        "fallback",
        "false",
        "filtered",
        "forall",
        "function",
        "ghost",
        "good_description",
        "havoc",
        "hook",
        "if",
        "import",
        "in",
        "indexed",
        "invariant",
        "lastReverted",
        "lastStorage",
        "links",
        "mapping",
        "methods",
        "new",
        "norevert",
        "old",
        "onTransactionBoundary",
        "override",
        "persistent",
        "preserved",
        "require",
        "requireInvariant",
        "reset_storage",
        "return",
        "returns",
        "revert",
        "rule",
        "satisfy",
        "sig",
        "sort",
        "strong",
        "sum",
        "true",
        "unresolved",
        "use",
        "using",
        "usum",
        "void",
        "weak",
        "with",
        "withrevert",
        "xor",
    }
)


def escape_reserved(name: str) -> str:
    """Rename ``name`` if CVL reserves it, else return it unchanged.

    Applied before collision mangling: renaming afterwards could turn a distinct name
    into one already taken (a library declaring both ``at`` and ``at_`` — and ``at_`` is
    in active use in OpenZeppelin's own harness).
    """
    return f"{name}_" if name in CVL_RESERVED_WORDS else name
