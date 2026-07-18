"""Assembly signals: density (S1), free-memory-pointer manipulation (S2),
delegatecall trampolines (S3).

Prover impact: inline assembly defeats memory partitioning and pointer analysis;
writes to the free-memory pointer (0x40) make scratch memory a first-class object
the analyses cannot model; delegatecall trampolines built from manual calldata +
returndatacopy leave every forwarded call unresolvable.
"""

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import (
    SignalResult,
    clamp,
    containing_function_penalty,
    make_evidence,
    signal,
)
from certora_autosetup.solidity_ast import (
    InlineAssembly,
    MemberAccess,
    YulFunctionCall,
    YulLiteralHexValue,
    YulLiteralValue,
    find_all,
)

FREE_MEMORY_POINTER = 0x40
SCRATCH_SLOTS = (0x00, 0x20)

EVIDENCE_CAP = 10  # per signal; raw counters always carry the full totals


def _yul_literal_int(node) -> int | None:
    if isinstance(node, (YulLiteralValue, YulLiteralHexValue)):
        v = getattr(node, "value", None) or getattr(node, "hexValue", None)
        if isinstance(v, str):
            try:
                return int(v, 0) if not v.startswith("0x") else int(v, 16)
            except ValueError:
                return None
    return None


@signal("asm_density")
def asm_density(ctx: AnalysisContext) -> SignalResult:
    total_fns = 0
    fns_with_asm = 0
    scoped_asm_fns = 0  # assembly living in a small internal/private helper
    total_blocks = 0
    untyped_blocks = 0  # solc < 0.6: no typed Yul AST, only the `operations` text
    weighted_asm_fns = 0.0  # functions-with-asm, weighted by how interwoven they are
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        total_fns += 1
        blocks = list(find_all(fn, InlineAssembly)) if fn.body is not None else []
        if not blocks:
            continue
        fns_with_asm += 1
        total_blocks += len(blocks)
        untyped_blocks += sum(1 for b in blocks if b.AST is None)
        penalty = containing_function_penalty(ctx, path, fn)
        weighted_asm_fns += penalty
        if penalty <= 0.2:
            scoped_asm_fns += 1
        elif len(evidence) < EVIDENCE_CAP:
            # only surface assembly that isn't a clean scoped helper
            evidence.append(make_evidence(
                ctx, "asm_density", Severity.LOW, path, fn.src_location.offset,
                f"{len(blocks)} inline-assembly block(s) interwoven with other code",
                function=f"{contract.name}.{fn.name or fn.kind}",
            ))
    # Density measured on the interwoven weight, not the raw count: assembly
    # confined to small internal/private helpers barely moves the score.
    ratio = weighted_asm_fns / total_fns if total_fns else 0.0
    return SignalResult(
        signal_id="asm_density",
        score=clamp(1.0 - 3.0 * ratio),
        evidence=evidence,
        raw={"functions": total_fns, "functions_with_asm": fns_with_asm,
             "scoped_asm_helpers": scoped_asm_fns,
             "weighted_asm_functions": round(weighted_asm_fns, 2),
             "asm_blocks": total_blocks, "untyped_asm_blocks": untyped_blocks},
    )


@signal("asm_fp_manipulation")
def asm_fp_manipulation(ctx: AnalysisContext) -> SignalResult:
    fp_writes = 0
    scratch_writes = 0
    interwoven_fp_writes = 0   # fp writes NOT confined to a small scoped helper
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        penalty = containing_function_penalty(ctx, path, fn)
        for block in find_all(fn, InlineAssembly):
            if block.AST is None:
                continue
            for call in find_all(block.AST, YulFunctionCall):
                if call.functionName.name != "mstore" or not call.arguments:
                    continue
                target = _yul_literal_int(call.arguments[0])
                if target == FREE_MEMORY_POINTER:
                    fp_writes += 1
                    if penalty > 0.2:
                        interwoven_fp_writes += 1
                        if len(evidence) < EVIDENCE_CAP:
                            evidence.append(make_evidence(
                                ctx, "asm_fp_manipulation", Severity.HIGH, path,
                                call.src_location.offset,
                                "mstore(0x40, ...) — free-memory pointer written by hand, "
                                "interwoven with other code",
                                function=f"{contract.name}.{fn.name or fn.kind}",
                            ))
                elif target in SCRATCH_SLOTS and penalty > 0.2:
                    scratch_writes += 1
                    if len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "asm_fp_manipulation", Severity.MEDIUM, path,
                            call.src_location.offset,
                            f"mstore({hex(target)}, ...) — scratch-space write",
                            function=f"{contract.name}.{fn.name or fn.kind}",
                        ))
    # Only free-memory-pointer manipulation OUTSIDE a small scoped helper is a
    # real hazard — the same mstore(0x40) inside a focused internal helper (the
    # common OZ/solady pattern) is tractable. Scale with how much of it there is:
    # a single interwoven write is medium-ish; pervasive FP surgery (the Crystal
    # profile) craters the score.
    score = 1.0
    if scratch_writes:
        score = 0.7
    if fp_writes and not interwoven_fp_writes:
        score = 0.7   # fp writes exist but only in scoped helpers
    if interwoven_fp_writes:
        score = clamp(0.45 - 0.08 * (interwoven_fp_writes - 1))
    return SignalResult(
        signal_id="asm_fp_manipulation",
        score=score,
        evidence=evidence,
        raw={"fp_writes": fp_writes, "interwoven_fp_writes": interwoven_fp_writes,
             "scratch_writes": scratch_writes},
    )


@signal("asm_trampoline")
def asm_trampoline(ctx: AnalysisContext) -> SignalResult:
    asm_delegatecalls = 0
    asm_calls = 0
    solidity_delegatecalls = 0
    # Weighted "hard forwarding": a delegatecall in a small dedicated proxy helper
    # (the standard EIP-1967 pattern) is far more tractable than manual
    # calldata/delegatecall interwoven with business logic in a big function.
    weighted_hard = 0.0
    weighted_asm_calls = 0.0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        fn_label = f"{contract.name}.{fn.name or fn.kind}"
        penalty = containing_function_penalty(ctx, path, fn)
        for block in find_all(fn, InlineAssembly):
            if block.AST is None:
                continue
            for call in find_all(block.AST, YulFunctionCall):
                name = call.functionName.name
                if name in ("delegatecall", "callcode"):
                    asm_delegatecalls += 1
                    weighted_hard += penalty
                    if penalty > 0.2 and len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "asm_trampoline", Severity.HIGH, path,
                            call.src_location.offset,
                            f"assembly {name} — manual call forwarding interwoven with "
                            "other code, unresolvable by the prover",
                            function=fn_label,
                        ))
                elif name in ("call", "staticcall"):
                    asm_calls += 1
                    weighted_asm_calls += penalty
                    if penalty > 0.2 and len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "asm_trampoline", Severity.MEDIUM, path,
                            call.src_location.offset,
                            f"assembly {name}", function=fn_label,
                        ))
        for member in find_all(fn, MemberAccess):
            if member.memberName == "delegatecall":
                solidity_delegatecalls += 1
                weighted_hard += penalty
                if penalty > 0.2 and len(evidence) < EVIDENCE_CAP:
                    evidence.append(make_evidence(
                        ctx, "asm_trampoline", Severity.HIGH, path,
                        member.src_location.offset,
                        "low-level .delegatecall — proxied logic outside the verified scene",
                        function=fn_label,
                    ))
    score = clamp(1.0 - 0.4 * weighted_hard - 0.1 * weighted_asm_calls)
    return SignalResult(
        signal_id="asm_trampoline",
        score=score,
        evidence=evidence,
        raw={"asm_delegatecalls": asm_delegatecalls, "asm_calls": asm_calls,
             "solidity_delegatecalls": solidity_delegatecalls,
             "weighted_hard_forwarding": round(weighted_hard, 2)},
    )
