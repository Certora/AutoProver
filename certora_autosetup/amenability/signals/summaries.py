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
# Names generic enough that a same-named project function is not evidence of a
# hand-rolled math kernel.
NONINDICATIVE_NAMES = {"toString"}
EVIDENCE_CAP = 10


@lru_cache(maxsize=1)
def _registry() -> dict:
    with open(SUMMARIES_REGISTRY) as f:
        return json.load(f)


@signal("curated_summary_hits")
def curated_summary_hits(ctx: AnalysisContext) -> SignalResult:
    registry = _registry()
    curated_libraries: dict[str, set[str]] = {}
    curated_fn_names: set[str] = set()
    for entry in registry.values():
        for lib in entry.get("library_names", []):
            curated_libraries.setdefault(lib, set()).update(entry.get("names", []))
        curated_fn_names.update(entry.get("names", []))
    curated_fn_names -= NONINDICATIVE_NAMES

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

    # Negative: curated-name functions implemented in PROJECT code outside any
    # curated library — a hand-rolled equivalent.
    for path, contract, fn in ctx.iter_functions():
        if is_dependency_path(path):
            continue
        if fn.name in curated_fn_names and contract.name not in curated_libraries:
            hand_rolled.append(f"{contract.name}.{fn.name}")
            if len(evidence) < EVIDENCE_CAP:
                evidence.append(make_evidence(
                    ctx, "curated_summary_hits", Severity.MEDIUM, path,
                    fn.src_location.offset,
                    f"hand-rolled `{fn.name}` — a curated summary exists for the "
                    "standard-library version, but not for this implementation",
                    function=f"{contract.name}.{fn.name}",
                ))

    if hand_rolled:
        score = 0.3
    elif hits:
        score = 1.0
    else:
        score = 0.7  # neither: neutral — the project may simply not need math libs
    return SignalResult(
        signal_id="curated_summary_hits",
        score=score,
        evidence=evidence,
        raw={"curated_hits": hits, "hand_rolled": hand_rolled},
    )
