"""Which rules a CVLR harness declares, read from the source.

The authoring framework's publish gate validates the property→check mapping against ground truth
when the backend can supply it: ``composer.authoring.state.validate_check_mapping`` takes a ``ran``
set and then checks *both* directions — no claimed rule that does not exist, and no rule that exists
without being tied back to a property. Its docstring notes that "forge names every test it ran; a
backend whose checker does not is passed ``None``". The Solana prover does name every rule it ran,
but only *after* submission, and the gate has to answer before one. So the ground truth here is the
buffer itself.

That is exact rather than approximate, because both ways of declaring a rule name it deterministically:

* ``#[rule]`` adds nothing but ``#[no_mangle]`` (``cvlr-macros``), so the symbol *is* the function
  name, verbatim.
* ``cvlr_rules!`` and ``cvlr_invariant_rules!`` expand to one ``cvlr_rule_for_spec!`` per base, and
  that macro builds the name as ``snake_case(name) + "_" + base``, with a ``base_`` prefix stripped
  from the base. :func:`snake_case` is a port of the crate's own function, and the examples in the
  crate's documentation are what the tests check it against.

**The grouping is kept, not flattened.** A parametric invocation is one authored construct that
becomes several rules, which is open question 5 of the backend plan — rule granularity changes both
prover cost and counterexample attribution. A caller that just wants the names calls
:func:`rule_names`; a caller reporting on what the author wrote wants to know that six verdicts came
from one three-line invocation.

**A shape this does not recognize costs the author a publish.** That is the opposite risk from the
crate inventory's, where a miss is a gate nobody can satisfy; here a miss is a rejection nobody can
act on. So the known shapes are pinned by test, and the mapping gate's rejection names what was
found — a false rejection should be self-diagnosing rather than mysterious.
"""

import dataclasses
import re

from composer.spec.types import RuleName

#: ``#[rule]`` — possibly with other attributes, doc comments or ``pub``/``unsafe`` between it and
#: the name. Deliberately not anchored to the same line: an attribute above a doc comment above a
#: signature is ordinary Rust, and a line scan is what misses it.
_RULE_ATTR = re.compile(r"#\[\s*rule\s*\]")
_FN_NAME = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)")

#: The two parametric forms. Both take ``name:`` and ``bases:``; they differ only in how the spec is
#: given, which does not affect the names.
_PARAMETRIC = re.compile(r"\b(cvlr_rules|cvlr_invariant_rules)\s*!")

_NAME_FIELD = re.compile(r"\bname\s*:\s*\"([^\"]*)\"")
_BASES_FIELD = re.compile(r"\bbases\s*:\s*\[")

_OPENERS = {"{": "}", "(": ")", "[": "]"}


def snake_case(name: str) -> str:
    """The crate's own ``to_snake_case``, ported.

    Lowercase, every non-alphanumeric character to ``_``, then collapse runs and trim — so
    ``"Vault Solvency"`` and ``"vault-solvency"`` both become ``vault_solvency``. Ported rather than
    approximated because the generated symbol is what the prover reports, and a name we spell
    differently is a rule the report cannot match up.
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

    ``spec_name`` is the literal the author wrote, before snake-casing, because that is the string a
    reader will search the source for."""

    macro: str
    spec_name: str
    bases: tuple[str, ...]
    line: int

    @property
    def names(self) -> tuple[RuleName, ...]:
        return tuple(generated_name(self.spec_name, b) for b in self.bases)


type Declaration = DirectRule | ParametricRules


def _matching(text: str, opener_at: int) -> int:
    """The index just past the delimiter matching the one at ``opener_at``.

    Depth-counted rather than regex-matched because the spec expression between ``name:`` and
    ``bases:`` routinely contains its own brackets — ``cvlr_and(a, b)``, an array, a closure — and a
    non-greedy match for ``bases: [...]`` would happily find one of those instead."""
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
    """Every rule declaration in ``source``, in the order it appears.

    Comments are not stripped. A ``#[rule]`` inside a block comment would be counted, which is the
    kind of thing a Rust parser would get right — but the input here is a buffer an agent wrote to
    be compiled, and paying for a parser to handle commented-out rules would be paying for the wrong
    problem. The gate that follows this one is a compiler.
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
    """Every rule name ``source`` declares, deduplicated, in declaration order.

    Deduplicated because two declarations can legitimately collide only by mistake — and a
    duplicated name is the compiler's complaint to make, not this function's.
    """
    seen: dict[RuleName, None] = {}
    for declaration in declared_rules(source):
        for name in declaration.names:
            seen.setdefault(name, None)
    return tuple(seen)
