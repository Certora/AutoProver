from typing import Literal, Optional, TypeVar
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

StatusCodes = Literal["VERIFIED", "VIOLATED", "TIMEOUT", "ERROR", "SANITY_FAILED", "SKIPPED"]

class _Missing:
    pass

_MISSING = _Missing()

_T = TypeVar('_T')

def _default_or(
    curr: _T,
    update: _Missing | _T
) -> _T:
    if isinstance(update, _Missing):
        return curr
    else:
        return update

@dataclass(frozen=True)
class RulePath:
    rule: str
    contract: Optional[str] = None
    method: Optional[str] = None
    sanity: bool = False

    def copy(
            self,
            rule : str | _Missing = _MISSING,
            contract : str | None | _Missing = _MISSING,
            method : str | None | _Missing = _MISSING,
            sanity : bool | _Missing = _MISSING
    ) -> 'RulePath':
        return RulePath(
            rule=_default_or(self.rule, rule),
            contract=_default_or(self.contract, contract),
            method=_default_or(self.method, method),
            sanity=_default_or(self.sanity, sanity)
        )
    def pprint(self) -> str:
        if self.contract is not None:
            if self.method is None:
                return f"{self.rule} in contract {self.contract}"

        if self.method is not None:
            return f"{self.rule} for {self.method}"
        else:
            return self.rule



@dataclass(frozen=True)
class SourceSpan:
    file: str
    line: int

    def pprint(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class Counterexample:
    """One violated rule's counterexample: the failing execution, what it broke, and where.

    ``trace`` arrives already rendered, because how much of a call trace is worth showing is a
    per-chain question the parser answers (:class:`composer.prover.results.TraceShape`). The
    assertion and its location are kept as fields rather than only inside that rendering, because
    whether the violation says anything about the *program* is decided from the assertion — see
    :func:`classify_violation` — and re-reading it out of the rendered string would be the same fact
    stored twice.
    """

    trace: str
    assertion: str | None = None
    source: SourceSpan | None = None

    def render(self) -> str:
        """The counterexample as an analyzer prompt reads it."""
        parts: list[str] = []
        if self.assertion:
            parts.append(f"<assert>{self.assertion}</assert>")
        if self.source is not None:
            parts.append(f"<source>{self.source.pprint()}</source>")
        parts.append(self.trace)
        return "<counterexample>" + "".join(parts) + "</counterexample>"


@dataclass(frozen=True)
class PropertyViolation:
    """The rule's own assertion failed, so the counterexample describes the program."""


@dataclass(frozen=True)
class IncompleteCheck:
    """The prover reported an assertion *it* generated, not the rule's.

    The check did not finish, so nothing about the program follows from it — an unwound loop bound
    is the prover saying it stopped, not the program misbehaving. Still worth showing an author,
    who can raise the bound or constrain the loop; not worth writing up as a finding, which is the
    distinction this type exists to make. ``assertion`` is the prover's own message, which carries
    its own recommendation.
    """

    assertion: str


type ViolationKind = PropertyViolation | IncompleteCheck

#: Assertions the prover generates for itself when an analysis bound is reached rather than a
#: property broken, by prefix — the tail carries advice that has changed between versions.
#:
#: The list is a filter that fails safe. An assertion it does not recognize is treated as the rule's
#: own, so a stale entry costs a spuriously reported finding — the state of things before this
#: existed — and can never suppress a real one.
_GENERATED_ASSERTIONS = ("Unwinding condition in a loop",)


def classify_violation(cex: Counterexample | None) -> ViolationKind:
    """Whether a violated rule found a bug or ran into the prover's own limits."""
    if cex is not None and cex.assertion is not None:
        if cex.assertion.startswith(_GENERATED_ASSERTIONS):
            return IncompleteCheck(cex.assertion)
    return PropertyViolation()


@dataclass
class RuleResult:
    """
    Rule result parsed out of SandboxedRunResult.
    name is the name of the rule, status is the status of the rule.
    If status == VIOLATED, then counterexample is non-null.

    If status == ERROR, error_msg is non-none
    """
    path: RulePath
    counterexample: Counterexample | None
    status: StatusCodes

    error_messages: list[str] = field(default_factory=list)

    live_check_info : str | None = field(default=None)

    @property
    def name(self) -> str:
        return self.path.pprint()

    @property
    def cex_dump(self) -> str | None:
        """The rendered counterexample every analysis prompt and report capture reads."""
        return None if self.counterexample is None else self.counterexample.render()


class AnalyzedDiagnosis(BaseModel):
    """A single root cause shared by one or more failing rule instances.

    Used internally by handlers (and by the codegen-side report store
    that ``cex_remediation`` looks up against). NOT part of the
    ``CexHandler`` interface — handlers that don't mint keyed
    diagnoses don't need to construct these.

    BaseModel rather than a frozen dataclass so that the report store
    can round-trip via ``model_dump()`` / ``model_validate()``. Pydantic
    v2 handles stdlib-dataclass fields like ``RulePath`` transparently
    inside a BaseModel.
    """

    model_config = ConfigDict(frozen=True)

    report_key: str
    diagnosis: str
    attributed_rules: list[RulePath]