"""Dynamic-loop signal (S11): loops whose bound is not a compile-time constant.

Prover impact: every dynamic loop is unrolled to loop_iter and either loses
soundness (optimistic) or explodes the formula; storage-length-bounded loops
additionally drag storage reasoning into every iteration.
"""

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal
from certora_autosetup.solidity_ast import (
    DoWhileStatement,
    ForStatement,
    Identifier,
    InlineAssembly,
    MemberAccess,
    WhileStatement,
    YulForLoop,
    find_all,
    walk,
)

EVIDENCE_CAP = 10


def _condition_is_dynamic(condition) -> tuple[bool, bool]:
    """(dynamic, storage_length_bound) for a loop condition subtree."""
    if condition is None:
        return True, False  # `for(;;)` / missing condition: bounded only by breaks
    dynamic = False
    length_bound = False
    for node in walk(condition):
        if isinstance(node, MemberAccess) and node.memberName == "length":
            dynamic = True
            length_bound = True
        elif isinstance(node, Identifier):
            name = node.name or ""
            if name.upper() != name:  # non-SCREAMING_CASE identifier = not a constant
                dynamic = True
        # Literals and constants keep the loop static.
    return dynamic, length_bound


@signal("dynamic_loops")
def dynamic_loops(ctx: AnalysisContext) -> SignalResult:
    dynamic = 0
    length_bounded = 0
    yul_loops = 0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        label = f"{contract.name}.{fn.name or fn.kind}"
        for loop in find_all(fn, (ForStatement, WhileStatement, DoWhileStatement)):
            cond = getattr(loop, "condition", None)
            is_dyn, is_len = _condition_is_dynamic(cond)
            if not is_dyn:
                continue
            dynamic += 1
            length_bounded += int(is_len)
            if len(evidence) < EVIDENCE_CAP:
                evidence.append(make_evidence(
                    ctx, "dynamic_loops",
                    Severity.MEDIUM if is_len else Severity.LOW,
                    path, loop.src_location.offset,
                    "storage-length-bounded loop" if is_len else "dynamically bounded loop",
                    function=label,
                ))
        for block in find_all(fn, InlineAssembly):
            if block.AST is not None:
                yul_loops += sum(1 for _ in find_all(block.AST, YulForLoop))
    return SignalResult(
        signal_id="dynamic_loops",
        score=clamp(1.0 - dynamic / 12.0),
        evidence=evidence,
        raw={"dynamic_loops": dynamic, "storage_length_bounded": length_bounded,
             "yul_for_loops": yul_loops},
    )
