"""Which rules a CVLR harness declares, read from the source.

A submission names these rules. A conf with no ``rule`` entry makes the cloud
job end without a report. Both declaration forms name their rules
deterministically:

* ``#[rule]`` adds ``#[no_mangle]`` (``cvlr-macros``), so the symbol is the
  function name.
* ``cvlr_rules!`` and ``cvlr_invariant_rules!`` expand to one
  ``cvlr_rule_for_spec!`` per base. That macro builds the name as
  ``snake_case(name) + "_" + base``, with a ``base_`` prefix stripped from
  the base. :func:`snake_case` matches the crate's own function. The tests
  check it against the examples in the crate's documentation.

:func:`rule_names` returns the names. A shape this parser does not recognize
is a rule the submission will not name, so the known shapes are covered by
tests.
"""

import re

from composer.spec.types import RuleName

#: ``#[rule]``, with other attributes, doc comments, or ``pub`` / ``unsafe``
#: allowed between it and the function name. Not limited to one line.
_RULE_ATTR = re.compile(r"#\[\s*rule\s*\]")
_FN_NAME = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)")

#: The two parametric forms. Both take ``name:`` and ``bases:``. How the spec
#: is given does not affect the names.
_PARAMETRIC = re.compile(r"\b(?:cvlr_rules|cvlr_invariant_rules)\s*!")

_NAME_FIELD = re.compile(r"\bname\s*:\s*\"([^\"]*)\"")
_BASES_FIELD = re.compile(r"\bbases\s*:\s*\[")

_OPENERS = {"{": "}", "(": ")", "[": "]"}


def snake_case(name: str) -> str:
    """The crate's ``to_snake_case``.

    Lowercase, every non-alphanumeric character to ``_``, then collapse runs
    and trim. ``"Vault Solvency"`` and ``"vault-solvency"`` both become
    ``vault_solvency``. The generated symbol is what the prover reports, so a
    different spelling would not match the report.
    """
    mapped = "".join(c if c.isalnum() or c == "_" else "_" for c in name.lower())
    return "_".join(part for part in mapped.split("_") if part)


def generated_name(spec_name: str, base: str) -> RuleName:
    """The rule ``cvlr_rule_for_spec!`` emits for one ``(name, base)`` pair."""
    stripped = base.removeprefix("base_")
    snake = snake_case(spec_name)
    return RuleName(f"{snake}_{stripped}" if stripped else snake)


def _matching(text: str, opener_at: int) -> int:
    """Index just past the delimiter matching the one at ``opener_at``.

    The spec expression between ``name:`` and ``bases:`` can contain its own
    brackets (``cvlr_and(a, b)``, an array, a closure). A regex for
    ``bases: [...]`` would match one of those.
    """
    opener = text[opener_at]
    closer = _OPENERS[opener]
    depth = 0
    index = opener_at
    while index < len(text):
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _parametric_names(text: str, bang_end: int) -> tuple[RuleName, ...] | None:
    """The names one invocation whose ``!`` ends at ``bang_end`` expands to."""
    body_start = next(
        (i for i in range(bang_end, len(text)) if not text[i].isspace()), len(text)
    )
    if body_start >= len(text) or text[body_start] not in _OPENERS:
        return None
    body = text[body_start : _matching(text, body_start)]

    name_match = _NAME_FIELD.search(body)
    bases_match = _BASES_FIELD.search(body)
    if name_match is None or bases_match is None:
        return None
    bracket = bases_match.end() - 1
    listed = body[bracket + 1 : _matching(body, bracket) - 1]
    bases = tuple(b.strip() for b in listed.split(",") if b.strip())
    if not bases:
        return None
    spec_name = name_match.group(1)
    return tuple(generated_name(spec_name, base) for base in bases)


def rule_names(source: str) -> tuple[RuleName, ...]:
    """Every rule name ``source`` declares, in declaration order, without duplicates.

    A duplicated name is a compiler error. This function reports it once.
    Comments are not stripped. A ``#[rule]`` inside a block comment is counted.
    The input is a buffer the agent wrote to compile, and the next gate is the
    compiler. Names from one parametric invocation stay in ``bases`` order.
    """
    found: list[tuple[int, tuple[RuleName, ...]]] = []

    for attr in _RULE_ATTR.finditer(source):
        signature = _FN_NAME.search(source, attr.end())
        if signature is None:
            continue
        found.append((attr.start(), (RuleName(signature.group(1)),)))

    for macro in _PARAMETRIC.finditer(source):
        names = _parametric_names(source, macro.end())
        if names is not None:
            found.append((macro.start(), names))

    seen: dict[RuleName, None] = {}
    for _, names in sorted(found, key=lambda pair: pair[0]):
        for name in names:
            seen.setdefault(name, None)
    return tuple(seen)
