"""Violated rules that stopped on an assertion the Solana Prover generated for itself.

Such a check did not finish, so nothing about the program follows from it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from composer.prover.ptypes import (
    GENERATED_ASSERTIONS,
    IncompleteCheck,
    LoopUnwound,
    PropertyViolation,
    RulePath,
    classify_assertion,
)


@dataclass(frozen=True)
class OverflowUnproven(IncompleteCheck):
    """The sound-signed-math pass could not show that an expression it treats as a pointer stays below ``2^64``.

    With ``-solanaTACSoundSignedMath``, the prover treats pointer arithmetic,
    byte-map indexes, and inferred pointers as unable to exceed 64 bits, then
    asserts that. The assertion fails when the expression uses a value read
    from account data that the rule left free near ``u64::MAX``. Bounding that
    input gets past it.
    """


type GeneratedAssertion = LoopUnwound | OverflowUnproven

_GENERATED: tuple[tuple[str, type[GeneratedAssertion]], ...] = (
    *GENERATED_ASSERTIONS,
    ("Cannot overflow", OverflowUnproven),
)


def classify(assertion: str) -> GeneratedAssertion | PropertyViolation:
    return classify_assertion(assertion, _GENERATED)


def incomplete_checks(violated: Mapping[RulePath, str]) -> dict[str, GeneratedAssertion]:
    """The rules with an instance that stopped on a generated assertion, keyed by rule name.

    ``violated`` is each violated instance's assertion
    (:attr:`composer.prover.core.ProverReport.violated_assertions`).
    """
    found: dict[str, GeneratedAssertion] = {}
    for path, assertion in violated.items():
        match classify(assertion):
            case PropertyViolation():
                continue
            case generated:
                found[path.rule] = generated
    return found
