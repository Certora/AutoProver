"""External-call surface signal (S10): low-level calls and try/call dispatch.

Prover impact: each low-level call is an unresolved callee the call-resolution
loop must chase (or havoc); address-indexed dispatch multiplies that. Complements
S3, which scores the delegatecall-trampoline shape specifically.
"""

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal
from certora_autosetup.solidity_ast import MemberAccess, TryStatement, find_all

LOW_LEVEL = {"call", "delegatecall", "staticcall"}
EVIDENCE_CAP = 10


@signal("external_call_surface")
def external_call_surface(ctx: AnalysisContext) -> SignalResult:
    low_level_calls = 0
    try_statements = 0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        for member in find_all(fn, MemberAccess):
            # Low-level members appear as MemberAccess on an address expression.
            if member.memberName in LOW_LEVEL:
                low_level_calls += 1
                if len(evidence) < EVIDENCE_CAP:
                    evidence.append(make_evidence(
                        ctx, "external_call_surface", Severity.LOW, path,
                        member.src_location.offset,
                        f"low-level .{member.memberName}",
                        function=f"{contract.name}.{fn.name or fn.kind}",
                    ))
        try_statements += sum(1 for _ in find_all(fn, TryStatement))
    score = clamp(1.0 - low_level_calls / 20.0)
    return SignalResult(
        signal_id="external_call_surface",
        score=score,
        evidence=evidence,
        raw={"low_level_calls": low_level_calls, "try_statements": try_statements},
    )
