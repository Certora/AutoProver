"""Storage-packing style signal (S9): standard declarations vs sanctioned slot
patterns vs opaque hand-rolled layouts.

Prover impact: the storage analysis infers a storage tree from standard
declarations; computed-slot assembly access (hand-rolled mappings, custom
packing behind raw sstore/sload) breaks stride inference — empirically a whole
preprocessing-phase blowup, not just imprecision.
"""

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal
from certora_autosetup.solidity_ast import (
    InlineAssembly,
    YulAssignment,
    YulFunctionCall,
    YulIdentifier,
    YulLiteralHexValue,
    YulLiteralValue,
    YulVariableDeclaration,
    find_all,
)

EVIDENCE_CAP = 10


def _computed_yul_vars(block_ast) -> set[str]:
    """Names of Yul variables whose (last) assigned value is a function call —
    i.e. computed slots (keccak256, arithmetic), as opposed to `x.slot`
    external references or plain literals."""
    computed: set[str] = set()
    for decl in find_all(block_ast, YulVariableDeclaration):
        if isinstance(getattr(decl, "value", None), YulFunctionCall):
            for var in decl.variables:
                computed.add(var.name)
    for assign in find_all(block_ast, YulAssignment):
        if isinstance(assign.value, YulFunctionCall):
            for target in assign.variableNames:
                computed.add(target.name)
    return computed


@signal("storage_packing")
def storage_packing(ctx: AnalysisContext) -> SignalResult:
    computed_slot_accesses = 0
    literal_slot_accesses = 0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        for block in find_all(fn, InlineAssembly):
            if block.AST is None:
                continue
            computed_vars = _computed_yul_vars(block.AST)
            for call in find_all(block.AST, YulFunctionCall):
                name = call.functionName.name
                if name not in ("sstore", "sload", "tstore", "tload"):
                    continue
                slot_arg = call.arguments[0] if call.arguments else None
                if isinstance(slot_arg, (YulLiteralValue, YulLiteralHexValue)):
                    literal_slot_accesses += 1
                    continue
                # Identifier slots referencing a declared var (`x.slot` external
                # reference) are the sanctioned pattern; anything computed
                # (keccak output, arithmetic) — directly or via a local Yul
                # variable — is opaque.
                computed = isinstance(slot_arg, YulFunctionCall) or (
                    isinstance(slot_arg, YulIdentifier) and slot_arg.name in computed_vars
                )
                if computed:
                    computed_slot_accesses += 1
                    if len(evidence) < EVIDENCE_CAP:
                        evidence.append(make_evidence(
                            ctx, "storage_packing", Severity.HIGH, path,
                            call.src_location.offset,
                            f"{name} with a computed slot expression — hand-rolled "
                            "storage layout the storage analysis cannot model",
                            function=f"{contract.name}.{fn.name or fn.kind}",
                        ))
    score = clamp(1.0 - 0.25 * computed_slot_accesses)
    return SignalResult(
        signal_id="storage_packing",
        score=score,
        evidence=evidence,
        raw={"computed_slot_accesses": computed_slot_accesses,
             "literal_slot_accesses": literal_slot_accesses},
    )
