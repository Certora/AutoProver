"""Bit-mask style signal (S4): opaque mask constants and inline bit-surgery
versus encapsulated accessor functions.

Prover impact: packed fields decoded inline with wide hex masks produce deep
bitvector terms at every use site; the same packing behind small internal pure
accessors gives the prover (and a human summarizer) one seam per field.
"""

import re

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal
from certora_autosetup.solidity_ast import (
    BinaryOperation,
    Literal,
    UnaryOperation,
    walk,
    find_all,
)

BIT_OPS = {"&", "|", "^", "<<", ">>"}
# Mask-like: >= 16 hex digits and dominated by f/0 nibbles (bit-range masks decode
# to runs of f/0 with at most a few partial nibbles at the boundaries).
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{16,}$")
ACCESSOR_MAX_NODES = 60  # node-count bound for "small accessor" classification
EVIDENCE_CAP = 10


def _is_mask_literal(lit: Literal) -> bool:
    v = lit.value or ""
    if not _HEX_RE.match(v):
        return False
    digits = v[2:].lower()
    f0 = sum(1 for c in digits if c in "f0")
    return f0 / len(digits) >= 0.7


@signal("bitmask_style")
def bitmask_style(ctx: AnalysisContext) -> SignalResult:
    mask_literals = 0
    accessor_bitops = 0
    inline_bitops = 0
    evidence = []
    for path, contract, fn in ctx.iter_functions():
        if fn.body is None:
            continue
        bitops = [op for op in find_all(fn, BinaryOperation) if op.operator in BIT_OPS]
        bitops += [op for op in find_all(fn, UnaryOperation) if op.operator == "~"]
        if not bitops:
            continue
        for op in find_all(fn, BinaryOperation):
            for side in (op.leftExpression, op.rightExpression):
                if isinstance(side, Literal) and _is_mask_literal(side):
                    mask_literals += 1
        is_accessor = (
            fn.visibility in ("internal", "private")
            and fn.stateMutability in ("pure", "view")
            and sum(1 for _ in walk(fn.body)) <= ACCESSOR_MAX_NODES
        )
        if is_accessor:
            accessor_bitops += len(bitops)
        else:
            inline_bitops += len(bitops)
            if len(evidence) < EVIDENCE_CAP and len(bitops) >= 5:
                evidence.append(make_evidence(
                    ctx, "bitmask_style", Severity.MEDIUM, path, fn.src_location.offset,
                    f"{len(bitops)} bit operations inline (not behind a small internal accessor)",
                    function=f"{contract.name}.{fn.name or fn.kind}",
                ))
    total = accessor_bitops + inline_bitops
    inline_ratio = inline_bitops / total if total else 0.0
    # Bit operations are bitvector-theory work — harder but tractable (medium),
    # even in volume, unless they're also unencapsulated. Penalize volume *
    # inline-ness but floor at 0.35 so bit-heavy code doesn't read as a rewrite.
    volume_factor = clamp(total / 120.0)
    score = clamp(1.0 - inline_ratio * volume_factor) * 0.65 + 0.35
    return SignalResult(
        signal_id="bitmask_style",
        score=score,
        evidence=evidence,
        raw={"bit_ops": total, "inline_bit_ops": inline_bitops,
             "accessor_bit_ops": accessor_bitops, "mask_literals": mask_literals,
             "inline_ratio": round(inline_ratio, 3)},
    )
