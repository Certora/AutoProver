from enum import StrEnum
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


class GeneratedKind(StrEnum):
    """The kinds of assertion the Prover generates for itself, as ``assertOrigin`` names them."""

    LOOP_UNWINDING = "loopUnwinding"
    CANNOT_OVERFLOW = "cannotOverflow"
    UNRESOLVED_CPI = "unresolvedCpi"
    PANIC = "panic"
    HASHING_BOUND = "hashingBound"
    RECURSION_LIMIT = "recursionLimit"
    STACK_SIZE_LIMIT = "stackSizeLimit"
    UNRESOLVED_JUMP = "unresolvedJump"
    CONTRACT_CREATION_BOUND = "contractCreationBound"
    MEMCPY_OVERLAP = "memcpyOverlap"
    UNSUPPORTED_HOST_FUNCTION = "unsupportedHostFunction"


@dataclass(frozen=True)
class UserAssertion:
    """An assertion the rule wrote."""


@dataclass(frozen=True)
class UnknownKind:
    """A kind of generated assertion that a newer Prover names and :class:`GeneratedKind` does not."""

    name: str


@dataclass(frozen=True)
class GeneratedAssertion:
    """An assertion the Prover inserted."""

    kind: GeneratedKind | UnknownKind


@dataclass(frozen=True)
class UnclassifiedAssertion:
    """The Prover did not say where the assertion came from.

    The built-in rule checkers' assertions are reported this way, as are generated assertions the
    Prover has not given a kind.
    """


type AssertOrigin = UserAssertion | GeneratedAssertion | UnclassifiedAssertion


@dataclass(frozen=True)
class Counterexample:
    """One violated rule's counterexample: the failing execution, what it broke, and where.

    ``trace`` arrives already rendered, because how much of a call trace is worth showing is a
    per-chain question the parser answers (:class:`composer.prover.results.TraceShape`).
    ``origin`` is what :func:`classify_violation` decides from: whether the violation says anything
    about the *program* at all.
    """

    trace: str
    origin: AssertOrigin
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
    """The counterexample describes the program: the rule's own assertion failed, or the program
    did something the Prover checks on its behalf, such as panicking."""


@dataclass(frozen=True)
class IncompleteCheck:
    """An assertion the Prover generated for itself, because it reached a limit of its own.

    The check did not finish, so nothing about the program follows from it. Show it to the author.
    Do not record it as a finding. ``assertion`` is the Prover's message, which carries its own
    advice.
    """

    kind: GeneratedKind | UnknownKind
    assertion: str | None


#: Generated assertions that check the program itself rather than mark where the Prover gave up.
_PROGRAM_CHECKS = frozenset({GeneratedKind.PANIC, GeneratedKind.MEMCPY_OVERLAP})


def classify_violation(cex: Counterexample) -> PropertyViolation | IncompleteCheck:
    """Whether a violated rule found a bug or ran into the Prover's own limits.

    An unclassified assertion is treated as the rule's own. If the Prover leaves one of its own
    assertions untagged, the rule is reported as a finding when it is really an incomplete check.
    The opposite mistake, setting a real violation aside as an incomplete check, cannot come from
    a missing tag.

    A kind this version does not know is an incomplete check: the Prover says it generated the
    assertion, and most kinds are its limits. If the new kind checks the program instead, the rule
    cannot be accounted for until it is added to :data:`_PROGRAM_CHECKS`, rather than becoming a
    finding no one looked at.
    """
    match cex.origin:
        case GeneratedAssertion(kind=kind) if kind not in _PROGRAM_CHECKS:
            return IncompleteCheck(kind=kind, assertion=cex.assertion)
        case _:
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
        """The counterexample as analysis prompts and report captures read it."""
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