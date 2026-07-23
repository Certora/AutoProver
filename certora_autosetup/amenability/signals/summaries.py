"""Curated-summary signal (S8): does the project use math libraries autosetup
already has curated CVL summaries for — or hand-rolled equivalents?

Prover impact: a curated library (OZ Math, FullMath, prb-math, ...) gets its
nonlinear kernels summarized away automatically; a hand-rolled mulDiv/sqrt is
raw nonlinear SMT with no ready summary.
"""

import json
from functools import lru_cache
from pathlib import Path

from certora_autosetup.amenability.context import AnalysisContext, is_dependency_path
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, make_evidence, signal
from certora_autosetup.solidity_ast import FunctionDefinition

SUMMARIES_REGISTRY = (
    Path(__file__).resolve().parents[2] / "setup" / "function_summaries.json"
)
# The only names whose hand-rolled reimplementation is a genuine amenability
# signal: nonlinear fixed-point / full-precision math primitives that the prover
# summarizes away when they come from a curated library but must reason about
# fully when reimplemented. Everything else in the registry (toString, safeTransfer,
# toUint*, get/set, extsload, ...) collides with unrelated helpers and is NOT
# evidence of a hard-to-verify math kernel.
MATH_KERNEL_NAMES = {
    "mulDiv", "fullMulDiv", "mulDivUp", "fullMulDivUp",
    "mulWad", "divWad", "mulWadUp", "divWadUp", "sMulWad", "sDivWad",
    "powWad", "expWad", "lnWad", "log2", "log10", "log2Up",
    "rpow", "rmul", "rdiv", "sqrt", "cbrt", "fixedPointMul",
}
EVIDENCE_CAP = 10


@lru_cache(maxsize=1)
def _registry() -> dict:
    with open(SUMMARIES_REGISTRY) as f:
        return json.load(f)


@signal("curated_summary_hits")
def curated_summary_hits(ctx: AnalysisContext) -> SignalResult:
    registry = _registry()
    curated_libraries: dict[str, set[str]] = {}
    for entry in registry.values():
        for lib in entry.get("library_names", []):
            curated_libraries.setdefault(lib, set()).update(entry.get("names", []))

    hits: list[str] = []
    hand_rolled: list[str] = []
    evidence = []

    # Positive: any library in the scene (dependencies included — that is where
    # they live) matching a curated entry by name + function overlap.
    for path, contract in ctx.iter_contracts(include_dependencies=True):
        if contract.contractKind != "library" or contract.name not in curated_libraries:
            continue
        member_names = {
            n.name for n in contract.nodes if isinstance(n, FunctionDefinition)
        }
        if member_names & curated_libraries[contract.name]:
            hits.append(f"{contract.name} ({path})")

    # Negative: a genuine nonlinear-math kernel (mulDiv/sqrt/mulWad/...) hand-rolled
    # in PROJECT code, outside any curated library. Generic-named helpers do not count.
    for path, contract, fn in ctx.iter_functions():
        if is_dependency_path(path):
            continue
        if fn.name in MATH_KERNEL_NAMES and contract.name not in curated_libraries:
            hand_rolled.append(f"{contract.name}.{fn.name}")
            if len(evidence) < EVIDENCE_CAP:
                evidence.append(make_evidence(
                    ctx, "curated_summary_hits", Severity.MEDIUM, path,
                    fn.src_location.offset,
                    f"hand-rolled `{fn.name}` — a curated summary exists for the "
                    "standard-library version, but not for this implementation",
                    function=f"{contract.name}.{fn.name}",
                ))

    # Using a curated math library is a positive; a hand-rolled kernel with no
    # curated counterpart is a mild negative (needs a written summary — medium,
    # not disqualifying); the common case (no heavy math) is neutral.
    if hits:
        score = 1.0
    elif hand_rolled:
        score = 0.55
    else:
        score = 0.85
    return SignalResult(
        signal_id="curated_summary_hits",
        score=score,
        evidence=evidence,
        raw={"curated_hits": hits, "hand_rolled": hand_rolled},
    )
