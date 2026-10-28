from collections.abc import Sequence
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
    assertion stays a field of its own because :func:`classify_assertion` decides from it whether
    the violation says anything about the *program* at all.
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
    """An assertion the prover generated for itself.

    The check did not finish, so nothing about the program follows from it. Show
    it to the author. Do not record it as a finding. ``assertion`` is the
    prover's message. The subclass names which kind it was.
    """

    assertion: str


@dataclass(frozen=True)
class LoopUnwound(IncompleteCheck):
    """A loop needed more iterations than the bound unrolls: the prover stopped, the program did
    not misbehave. The author can bound what sets the trip count, or raise the bound."""


#: Assertions the prover generates for itself on every platform, matched by
#: prefix. The tail carries advice that changes between versions. A platform
#: that generates more extends this.
#:
#: An unrecognized assertion is treated as the rule's own. A stale entry can
#: report a finding that is not one, and cannot hide a real one.
GENERATED_ASSERTIONS: tuple[tuple[str, type[LoopUnwound]], ...] = (
    ("Unwinding condition in a loop", LoopUnwound),
)


def classify_assertion[K: IncompleteCheck](
    assertion: str, generated: Sequence[tuple[str, type[K]]]
) -> K | PropertyViolation:
    """Whether a violated assertion found a bug or is one of ``generated``, the prover's own limits."""
    for prefix, kind in generated:
        if assertion.startswith(prefix):
            return kind(assertion)
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