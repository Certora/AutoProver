from dataclasses import dataclass

@dataclass(frozen=True)
class ProverValidation:
    """Completion gate: the Certora Prover verified a committed spec against the
    generated code. One gate per registered spec, keyed by its VFS path."""

    spec: str

    def to_key(self) -> str:
        return f"prover:{self.spec}"

    def description(self) -> str:
        return f"prover verification of {self.spec}"


@dataclass(frozen=True)
class ReqsValidation:
    """Completion gate: the implementation satisfies the extracted
    natural-language requirements (stamped by the requirements judge)."""

    def to_key(self) -> str:
        return "natural language requirements"

    def description(self) -> str:
        return "satisfaction of the natural-language requirements"


type CodegenValidation = ProverValidation | ReqsValidation
