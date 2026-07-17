"""Signal protocol: each signal is a pure function over the AnalysisContext.

Scores are normalized to [0, 1] with 1 = fully amenable, so aggregation is a
plain weighted mean and weights.yaml stays the only tuning surface.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Evidence, Severity


@dataclass
class SignalResult:
    signal_id: str
    score: float  # [0,1], 1 = amenable
    evidence: list[Evidence] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class Signal(Protocol):
    signal_id: str

    def __call__(self, ctx: AnalysisContext) -> SignalResult: ...


def make_evidence(
    ctx: AnalysisContext,
    signal_id: str,
    severity: Severity,
    source_path: str,
    byte_offset: int,
    detail: str,
    function: str | None = None,
) -> Evidence:
    return Evidence(
        signal=signal_id,
        severity=severity,
        file=ctx.display_path(source_path),
        line=ctx.offset_to_line(source_path, byte_offset),
        function=function,
        detail=detail,
    )


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def signal(signal_id: str) -> Callable[[Callable[[AnalysisContext], SignalResult]], Callable[[AnalysisContext], SignalResult]]:
    """Attach the id to the function so the registry can enumerate it."""

    def deco(fn: Callable[[AnalysisContext], SignalResult]) -> Callable[[AnalysisContext], SignalResult]:
        fn.signal_id = signal_id  # type: ignore[attr-defined]
        return fn

    return deco
