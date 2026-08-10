"""Function-shape signals: length distribution (S5) and surface-shape
normalizers (S12).

Prover impact: a 500-line function is one giant TAC body — no divide-and-conquer
via internal-function summaries, worst-case pattern-matching and SMT splitting.
S12 carries scene-size counters used to normalize other signals; it does not
judge on its own (weight 0 by default).
"""

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal

LINES_FLAG = 150
LINES_FLOOR = 600  # at/above this, the length score bottoms out
EVIDENCE_CAP = 10


@signal("function_length")
def function_length(ctx: AnalysisContext) -> SignalResult:
    spans = []
    flagged = []
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        loc = fn.src_location
        span = ctx.line_span(path, loc.offset, loc.length)
        if span == 0:
            continue  # source text unavailable
        label = f"{contract.name}.{fn.name or fn.kind}"
        spans.append(span)
        if span > LINES_FLAG:
            flagged.append({"function": label, "lines": span})
            if len(evidence) < EVIDENCE_CAP:
                evidence.append(make_evidence(
                    ctx, "function_length",
                    Severity.HIGH if span >= LINES_FLOOR / 2 else Severity.MEDIUM,
                    path, loc.offset, f"{span}-line function", function=label,
                ))
    if not spans:
        return SignalResult("function_length", 1.0, [], {"functions": 0})
    longest = max(spans)
    spans.sort()
    p90 = spans[int(0.9 * (len(spans) - 1))]
    # 1.0 while the longest fits the flag bound, 0.0 at LINES_FLOOR.
    score = clamp(1.0 - (longest - LINES_FLAG) / (LINES_FLOOR - LINES_FLAG))
    return SignalResult(
        signal_id="function_length",
        score=score,
        evidence=evidence,
        raw={"functions": len(spans), "max_lines": longest, "p90_lines": p90,
             "flagged": flagged[:20]},
    )


@signal("surface_shape")
def surface_shape(ctx: AnalysisContext) -> SignalResult:
    contracts = 0
    external_fns = 0
    max_inheritance = 0
    for _, contract in ctx.iter_contracts():
        if contract.contractKind != "contract":
            continue
        contracts += 1
        max_inheritance = max(max_inheritance, len(contract.linearizedBaseContracts))
    for _, _, fn in ctx.iter_functions():
        if fn.visibility in ("external", "public"):
            external_fns += 1
    return SignalResult(
        signal_id="surface_shape",
        score=1.0,  # informational normalizer; weight 0 in weights.yaml
        evidence=[],
        raw={"contracts": contracts, "external_or_public_functions": external_fns,
             "max_inheritance_chain": max_inheritance},
    )
