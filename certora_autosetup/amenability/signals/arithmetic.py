"""Arithmetic signals: unchecked nonlinear math (S6) and mixed
bitvector+nonlinear theory in a single function (S7).

Prover impact: nonlinear integer arithmetic (mul/div chains with symbolic
operands) is the classic SMT blowup; combining it with bitvector operations in
the same function forces the solver to reason across theories with no clean cut,
and prevents proving each part modularly via internal-function summaries.
"""

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal
from certora_autosetup.solidity_ast import (
    BinaryOperation,
    FunctionCall,
    Identifier,
    InlineAssembly,
    Literal,
    UnaryOperation,
    UncheckedBlock,
    YulFunctionCall,
    find_all,
)

NONLINEAR_OPS = {"*", "/", "%", "**"}
BITVECTOR_OPS = {"&", "|", "^", "<<", ">>"}
YUL_NONLINEAR = {"mul", "div", "sdiv", "mod", "smod", "mulmod", "exp"}
YUL_BITVECTOR = {"and", "or", "xor", "not", "shl", "shr", "sar", "byte"}
EVIDENCE_CAP = 10


def _is_constant_operand(expr) -> bool:
    """Cheap constness: literals and SCREAMING_CASE identifiers (constants by
    convention). Full referencedDeclaration resolution is a later refinement."""
    if isinstance(expr, Literal):
        return True
    if isinstance(expr, Identifier):
        name = expr.name
        return bool(name) and name.upper() == name and any(c.isalpha() for c in name)
    return False


@signal("unchecked_nonlinear")
def unchecked_nonlinear(ctx: AnalysisContext) -> SignalResult:
    sites = 0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        for block in find_all(fn, UncheckedBlock):
            for op in find_all(block, BinaryOperation):
                if op.operator not in NONLINEAR_OPS:
                    continue
                if _is_constant_operand(op.leftExpression) or _is_constant_operand(op.rightExpression):
                    continue
                sites += 1
                if len(evidence) < EVIDENCE_CAP:
                    evidence.append(make_evidence(
                        ctx, "unchecked_nonlinear", Severity.MEDIUM, path,
                        op.src_location.offset,
                        f"unchecked nonlinear `{op.operator}` with symbolic operands",
                        function=f"{contract.name}.{fn.name or fn.kind}",
                    ))
    # Unchecked nonlinear arithmetic makes the SMT harder but is config/summary
    # solvable (medium), not a rewrite trigger — floor at 0.35.
    return SignalResult(
        signal_id="unchecked_nonlinear",
        score=clamp(1.0 - sites / 25.0) * 0.65 + 0.35,
        evidence=evidence,
        raw={"sites": sites},
    )


def _count_ops(fn) -> tuple[int, int]:
    bv = 0
    nl = 0
    for op in find_all(fn, BinaryOperation):
        if op.operator in BITVECTOR_OPS:
            bv += 1
        elif op.operator in NONLINEAR_OPS:
            nl += 1
    for op in find_all(fn, UnaryOperation):
        if op.operator == "~":
            bv += 1
    for call in find_all(fn, FunctionCall):
        callee = call.expression
        if isinstance(callee, Identifier) and callee.name in ("mulmod", "addmod"):
            nl += 1
    for block in find_all(fn, InlineAssembly):
        if block.AST is None:
            continue
        for ycall in find_all(block.AST, YulFunctionCall):
            name = ycall.functionName.name
            if name in YUL_BITVECTOR:
                bv += 1
            elif name in YUL_NONLINEAR:
                nl += 1
    return bv, nl


@signal("mixed_theory")
def mixed_theory(ctx: AnalysisContext) -> SignalResult:
    flagged = 0
    evidence = []
    per_fn = {}
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        bv, nl = _count_ops(fn)
        if min(bv, nl) >= 3:
            flagged += 1
            label = f"{contract.name}.{fn.name or fn.kind}"
            per_fn[label] = {"bitvector_ops": bv, "nonlinear_ops": nl}
            if len(evidence) < EVIDENCE_CAP:
                evidence.append(make_evidence(
                    ctx, "mixed_theory", Severity.HIGH, path, fn.src_location.offset,
                    f"{bv} bitvector + {nl} nonlinear ops interleaved in one function "
                    "(no internal-function seam to separate the theories)",
                    function=label,
                ))
    # Mixed bitvector+nonlinear theory in one function is a real SMT hazard, but
    # only pervasive mixing is disqualifying; a handful of such functions is
    # medium. Floor at 0.3.
    return SignalResult(
        signal_id="mixed_theory",
        score=clamp(1.0 - flagged / 12.0) * 0.7 + 0.3,
        evidence=evidence,
        raw={"flagged_functions": flagged, "per_function": per_fn},
    )
