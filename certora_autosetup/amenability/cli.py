"""certora-fv-amenability: score how amenable a Solidity project is to
automatic formal verification with autosetup.

JSON in/out, no side effects: clients (SaaS, CI, humans) call this and read the
report from stdout. Exit 0 = scored (any level); exit 1 = could not score
(most importantly: the project does not compile).
"""

import argparse
import subprocess
import sys
from pathlib import Path

from certora_autosetup.amenability.compile import CannotScoreError, resolve_dumps
from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import (
    AmenabilityReport,
    Level,
    Recommendation,
    ScoringErrorReport,
)
from certora_autosetup.amenability.scoring import DEFAULT_WEIGHTS, ScoringConfig, aggregate
from certora_autosetup.amenability.signals import ALL_SIGNALS
from certora_autosetup.solidity_ast import ContractDefinition, find_all

BASE_CONFIDENCE = 0.6  # static-only scoring; the phase-2 judge raises/lowers this


def _tool_version() -> str:
    try:
        return subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _recommendations(report_evidence) -> list[Recommendation]:
    kinds = {e.signal for e in report_evidence}
    recs = []
    if "curated_summary_hits" in kinds:
        recs.append(Recommendation(
            kind="summary",
            detail="Replace hand-rolled math kernels with a standard library "
                   "(OZ Math / FullMath / prb-math) for which autosetup ships curated summaries.",
        ))
    if "asm_trampoline" in kinds:
        recs.append(Recommendation(
            kind="reference-impl",
            detail="Replace manual calldata/delegatecall trampolines with direct calls or a "
                   "reference implementation without the forwarding layer.",
        ))
    if "asm_fp_manipulation" in kinds or "storage_packing" in kinds:
        recs.append(Recommendation(
            kind="munge",
            detail="Move hand-rolled memory/storage layouts behind standard declarations or "
                   "sanctioned slot patterns (ERC-7201/StorageSlot) so the prover's storage "
                   "and memory analyses can model them.",
        ))
    if "bitmask_style" in kinds or "mixed_theory" in kinds:
        recs.append(Recommendation(
            kind="harness",
            detail="Extract packed-field decoding and nonlinear math into small internal pure "
                   "functions — each becomes a summarization seam for modular proofs.",
        ))
    return recs


def main() -> int:
    parser = argparse.ArgumentParser(prog="certora-fv-amenability", description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("--contract", action="append", default=[],
                        help="restrict the contracts_analyzed listing (repeatable)")
    parser.add_argument("--ast-dump", action="append", default=[], type=Path,
                        help="existing certoraRun --dump_asts output (repeatable)")
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument("--judge", action="store_true",
                        help="run the LLM judge over the static report (requires "
                             "Anthropic credentials); default is static-only")
    parser.add_argument("--rubric-version", default=None,
                        help="pin a specific judge rubric version (default: latest)")
    parser.add_argument("--no-llm", action="store_true",
                        help="explicitly static-only (the default; kept for interface stability)")
    parser.add_argument("--output", type=Path, default=None, help="write report here instead of stdout")
    args = parser.parse_args()

    project_root = args.project_root.resolve()

    try:
        resolution = resolve_dumps(project_root, args.ast_dump)
    except CannotScoreError as e:
        error = ScoringErrorReport(project=str(project_root), error=e.error, detail=e.detail)
        print(error.model_dump_json(indent=2))
        return 1

    ctx = AnalysisContext(project_root=project_root, dumps=resolution.dumps)

    config = ScoringConfig.load(args.weights)
    results = [sig(ctx) for sig in ALL_SIGNALS]
    static = aggregate(results, config)
    evidence = [e for r in results for e in r.evidence]

    contracts = sorted({
        c.name for _, root in ctx.iter_sources()
        for c in find_all(root, ContractDefinition)
        if c.contractKind == "contract" and not c.abstract
    })
    if args.contract:
        requested = set(args.contract)
        contracts = [c for c in contracts if c in requested]

    confidence = BASE_CONFIDENCE
    if ctx.unparsed_source_count:
        confidence = max(0.3, confidence - 0.05 * ctx.unparsed_source_count)

    report = AmenabilityReport(
        tool_version=_tool_version(),
        project=str(project_root),
        contracts_analyzed=contracts,
        level=static.provisional_level,
        confidence=round(confidence, 2),
        static=static,
        evidence=evidence,
        recommendations=_recommendations(evidence),
    )

    if args.judge and not args.no_llm:
        from certora_autosetup.amenability.judge import JudgeError, judge_report
        try:
            report.judge = judge_report(report, ctx, rubric_version=args.rubric_version)
            report.level = Level(report.judge["level"])
            report.confidence = round(
                (confidence + report.judge["confidence"]) / 2, 2
            )
        except JudgeError as e:
            print(f"judge failed, keeping static verdict: {e}", file=sys.stderr)

    payload = report.model_dump_json(indent=2)
    if args.output:
        args.output.write_text(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
