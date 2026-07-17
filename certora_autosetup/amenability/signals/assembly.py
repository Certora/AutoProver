"""Assembly signals: density (S1), free-memory-pointer manipulation (S2),
delegatecall trampolines (S3).

Prover impact: inline assembly defeats memory partitioning and pointer analysis;
writes to the free-memory pointer (0x40) make scratch memory a first-class object
the analyses cannot model; delegatecall trampolines built from manual calldata +
returndatacopy leave every forwarded call unresolvable.
"""

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal
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
    total_blocks = 0
    untyped_blocks = 0  # solc < 0.6: no typed Yul AST, only the `operations` text
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        total_fns += 1
        blocks = list(find_all(fn, InlineAssembly)) if fn.body is not None else []
        if not blocks:
            continue
        fns_with_asm += 1
        total_blocks += len(blocks)
        untyped_blocks += sum(1 for b in blocks if b.AST is None)
        if len(evidence) < EVIDENCE_CAP:
            evidence.append(make_evidence(
                ctx, "asm_density", Severity.LOW, path, fn.src_location.offset,
                f"{len(blocks)} inline-assembly block(s)",
                function=f"{contract.name}.{fn.name or fn.kind}",
            ))
    ratio = fns_with_asm / total_fns if total_fns else 0.0
    return SignalResult(
        signal_id="asm_density",
        score=clamp(1.0 - 3.0 * ratio),
        evidence=evidence,
        raw={"functions": total_fns, "functions_with_asm": fns_with_asm,
             "asm_blocks": total_blocks, "untyped_asm_blocks": untyped_blocks},
    )


@signal("asm_fp_manipulation")
def asm_fp_manipulation(ctx: AnalysisContext) -> SignalResult:
    fp_writes = 0
    scratch_writes = 0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        for block in find_all(fn, InlineAssembly):
            if block.AST is None:
                continue
            for call in find_all(block.AST, YulFunctionCall):
                if call.functionName.name != "mstore" or not call.arguments:
                    continue
                target = _yul_literal_int(call.arguments[0])
                if target == FREE_MEMORY_POINTER:
                    fp_writes += 1
                    if len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "asm_fp_manipulation", Severity.HIGH, path,
                            call.src_location.offset,
                            "mstore(0x40, ...) — free-memory pointer written by hand",
                            function=f"{contract.name}.{fn.name or fn.kind}",
                        ))
                elif target in SCRATCH_SLOTS:
                    scratch_writes += 1
                    if len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "asm_fp_manipulation", Severity.MEDIUM, path,
                            call.src_location.offset,
                            f"mstore({hex(target)}, ...) — scratch-space write",
                            function=f"{contract.name}.{fn.name or fn.kind}",
                        ))
    score = 1.0
    if scratch_writes:
        score = 0.6
    if fp_writes:
        score = 0.15
    return SignalResult(
        signal_id="asm_fp_manipulation",
        score=score,
        evidence=evidence,
        raw={"fp_writes": fp_writes, "scratch_writes": scratch_writes},
    )


@signal("asm_trampoline")
def asm_trampoline(ctx: AnalysisContext) -> SignalResult:
    asm_delegatecalls = 0
    asm_calls = 0
    solidity_delegatecalls = 0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        fn_label = f"{contract.name}.{fn.name or fn.kind}"
        for block in find_all(fn, InlineAssembly):
            if block.AST is None:
                continue
            for call in find_all(block.AST, YulFunctionCall):
                name = call.functionName.name
                if name in ("delegatecall", "callcode"):
                    asm_delegatecalls += 1
                    if len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "asm_trampoline", Severity.HIGH, path,
                            call.src_location.offset,
                            f"assembly {name} — manual call forwarding the prover cannot resolve",
                            function=fn_label,
                        ))
                elif name in ("call", "staticcall"):
                    asm_calls += 1
                    if len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "asm_trampoline", Severity.MEDIUM, path,
                            call.src_location.offset,
                            f"assembly {name}", function=fn_label,
                        ))
        for member in find_all(fn, MemberAccess):
            if member.memberName == "delegatecall":
                solidity_delegatecalls += 1
                if len(evidence) < EVIDENCE_CAP:
                    evidence.append(make_evidence(
                        ctx, "asm_trampoline", Severity.HIGH, path,
                        member.src_location.offset,
                        "low-level .delegatecall — proxied logic outside the verified scene",
                        function=fn_label,
                    ))
    hard = asm_delegatecalls + solidity_delegatecalls
    score = clamp(1.0 - 0.4 * hard - 0.1 * asm_calls)
    return SignalResult(
        signal_id="asm_trampoline",
        score=score,
        evidence=evidence,
        raw={"asm_delegatecalls": asm_delegatecalls, "asm_calls": asm_calls,
             "solidity_delegatecalls": solidity_delegatecalls},
    )
