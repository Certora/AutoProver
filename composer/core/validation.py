"""Codegen's completion-validation key domain.

``ValidationKey`` (composer/chassis/validation.py) is a Protocol so each
workflow brings its own key type — codegen's gates are distinct from spec-gen's
or foundry's. These are codegen's: a sum of frozen dataclasses (one per gate),
so the prover gate can grow a per-spec field for multi-spec without disturbing
the others.
"""

from dataclasses import dataclass

@dataclass(frozen=True)
class ProverValidation:
    """Completion gate: the Certora Prover verified the generated code against
    the spec."""

    def to_key(self) -> str:
        return "prover"

    def description(self) -> str:
        return "prover verification"


@dataclass(frozen=True)
class ReqsValidation:
    """Completion gate: the implementation satisfies the extracted
    natural-language requirements (stamped by the requirements judge)."""

    def to_key(self) -> str:
        return "natural language requirements"

    def description(self) -> str:
        return "satisfaction of the natural-language requirements"


type CodegenValidation = ProverValidation | ReqsValidation
