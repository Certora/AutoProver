"""CVL grammar keywords, and the escape for a Solidity name that collides with one.

Anything we generate for a spec — a summary's parameter names, a harness wrapper's function
names — has to be nameable in CVL. The grammar reserves words that are perfectly legal
Solidity identifiers, and a mechanically-generated name hits them routinely: `at` appears 41
times across OpenZeppelin and Solady, `sort` 4. Such a name compiles as Solidity and then makes
the spec unparseable.

The escape is a trailing underscore, matching OpenZeppelin's hand-written Certora harness
(`at_`), so generated specs read like the human-written ones.
"""

from typing import FrozenSet

#: Keyword terminals that cannot double as an identifier. A name equal to one of these is lexed
#: as the keyword inside a methods{} entry, which is a syntax error.
CVL_RESERVED_WORDS: FrozenSet[str] = frozenset({
    "ALL", "ALWAYS", "ASSERT_FALSE", "AUTO", "CONSTANT", "Create", "DELETE", "DISPATCH", "DISPATCHER",
    "HAVOC_ALL", "HAVOC_ECF", "NONDET", "PER_CALLEE_CONSTANT", "STORAGE", "Sload", "Sstore", "Tload",
    "Tstore", "UNRESOLVED", "assert", "assuming", "at", "axiom", "default", "definition", "else",
    "event", "expect", "fallback", "false", "filtered", "function", "ghost", "good_description",
    "havoc", "if", "in", "indexed", "lastReverted", "lastStorage", "links", "mapping", "methods",
    "new", "norevert", "persistent", "require", "requireInvariant", "reset_storage", "return",
    "returns", "revert", "rule", "satisfy", "sort", "true", "void", "with", "withrevert", "xor",
})

#: The terminals under the `usable_keywords` production in cvl.cup. The grammar accepts these
#: wherever an identifier is expected, so a name equal to one of them parses fine and must NOT
#: be mangled — renaming it would only produce a spec that reads worse.
#:
#: Verified rather than assumed: a contract with functions named `exists`, `sum`, `old`, `forall`
#: and `invariant`, declared in a methods block and called from a rule, typechecks; the same
#: shape with `havoc` or `rule` is a syntax error.
#:
#: Note uppercase "UNRESOLVED" is a distinct summary keyword and stays reserved above.
CVL_USABLE_KEYWORDS: FrozenSet[str] = frozenset({
    "as", "builtin", "description", "exists", "forall", "hook", "import", "invariant",
    "old", "onTransactionBoundary", "override", "preserved", "sig", "strong", "sum",
    "unresolved", "use", "using", "usum", "weak",
})


def escape_reserved(name: str) -> str:
    """Rename `name` if CVL reserves it, else return it unchanged.

    Apply this before collision mangling, not after: renaming afterwards could turn a distinct
    name into one already taken — a library declaring both `at` and `at_`, and `at_` is in
    active use in OpenZeppelin's own harness.
    """
    return f"{name}_" if name in CVL_RESERVED_WORDS else name
