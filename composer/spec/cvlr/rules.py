"""Which rules a CVLR harness declares, read from the source.

The publish gate checks the property-to-rule mapping before any submission.
The names come from the draft. Both declaration forms name their rules
deterministically:

* ``#[rule]`` adds ``#[no_mangle]`` (``cvlr-macros``), so the symbol is the
  function name.
* ``cvlr_rules!`` and ``cvlr_invariant_rules!`` expand to one
  ``cvlr_rule_for_spec!`` per base. That macro builds the name as
  ``snake_case(name) + "_" + base``, with a ``base_`` prefix stripped from
  the base. :func:`snake_case` matches the crate's own function. The tests
  check it against the examples in the crate's documentation.

:func:`declared_rules` keeps each parametric invocation as one declaration
that expands to several rules. :func:`rule_names` returns the names.
A shape this parser does not recognize is a rule the mapping gate will not
see, so the known shapes are covered by tests and a rejection lists what
was found.
"""

import dataclasses
import re

from composer.spec.types import RuleName

#: ``#[rule]``, with other attributes, doc comments, or ``pub`` / ``unsafe``
#: allowed between it and the function name. Not limited to one line.
_RULE_ATTR = re.compile(r"#\[\s*rule\s*\]")
_FN_NAME = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)")

#: The two parametric forms. Both take ``name:`` and ``bases:``. How the spec
#: is given does not affect the names.
_PARAMETRIC = re.compile(r"\b(cvlr_rules|cvlr_invariant_rules)\s*!")

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


@dataclasses.dataclass(frozen=True)
class DirectRule:
    """One ``#[rule]`` function."""

    name: RuleName
    line: int

    @property
    def names(self) -> tuple[RuleName, ...]:
        return (self.name,)


@dataclasses.dataclass(frozen=True)
class ParametricRules:
    """One ``cvlr_rules!`` / ``cvlr_invariant_rules!`` invocation, and what it expands to.

    ``spec_name`` is the literal the author wrote, before snake-casing. That is
    the string a reader searches the source for.
    """

    macro: str
    spec_name: str
    bases: tuple[str, ...]
    line: int

    @property
    def names(self) -> tuple[RuleName, ...]:
        return tuple(generated_name(self.spec_name, b) for b in self.bases)


type Declaration = DirectRule | ParametricRules


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


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _parametric_at(text: str, macro: str, bang_end: int) -> ParametricRules | None:
    """Parse one invocation whose ``!`` ends at ``bang_end``."""
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
    return ParametricRules(
        macro=macro,
        spec_name=name_match.group(1),
        bases=bases,
        line=_line_of(text, body_start),
    )


def declared_rules(source: str) -> tuple[Declaration, ...]:
    """Every rule declaration in ``source``, in source order.

    Comments are not stripped. A ``#[rule]`` inside a block comment is counted.
    The input is a buffer the agent wrote to compile, and the next gate is the
    compiler.
    """
    found: list[tuple[int, Declaration]] = []

    for attr in _RULE_ATTR.finditer(source):
        signature = _FN_NAME.search(source, attr.end())
        if signature is None:
            continue
        found.append(
            (
                attr.start(),
                DirectRule(
                    name=RuleName(signature.group(1)), line=_line_of(source, attr.start())
                ),
            )
        )

    for macro in _PARAMETRIC.finditer(source):
        parsed = _parametric_at(source, macro.group(1), macro.end())
        if parsed is not None:
            found.append((macro.start(), parsed))

    return tuple(declaration for _, declaration in sorted(found, key=lambda p: p[0]))


def rule_names(source: str) -> tuple[RuleName, ...]:
    """Every rule name ``source`` declares, in declaration order, without duplicates.

    A duplicated name is a compiler error. This function reports it once.
    """
    seen: dict[RuleName, None] = {}
    for declaration in declared_rules(source):
        for name in declaration.names:
            seen.setdefault(name, None)
    return tuple(seen)
