"""Summarization-target detector — a standalone AutoProver tool.

From a prover run it ranks the functions worth summarizing: a candidate list with WHY each is
prover-hostile (the hard-op signal / category), WHERE it can be summarized (caller boundaries), and, for a
curated public-library match, a suggested summary. It suggests WHAT to summarize — not the summarization
strategy (per-function summary, whole-contract symbolic model, …) and not how the summary is written; the
consumer decides that. It fetches the prover's output via
`prover_output_utility` and parses the solc AST dump (from `certoraRun --dump_asts`) with
`certora_autosetup.solidity_ast`. Invoke via `detect()` or the CLI
(`python -m summarization_detector` / `detect-summaries`).
"""
from .detect import (
    Boundary,
    Candidate,
    DetectionReport,
    HashSignal,
    HostileCategory,
    CuratedEntry,
    HostileMatch,
    detect,
    detect_from,
    scan_ast,
    classify_hostile,
    surviving_hostile,
    reachable_from_main,
    cone_weights,
)
from .sources import detect_url, cut_from_conf, find_run_conf, fetch_surviving_graphs

__all__ = [
    "Boundary",
    "Candidate",
    "DetectionReport",
    "HashSignal",
    "detect",
    "detect_from",
    "detect_url",
    "scan_ast",
    "reachable_from_main",
    "cone_weights",
    "cut_from_conf",
    "find_run_conf",
    "HostileCategory",
    "CuratedEntry",
    "HostileMatch",
    "classify_hostile",
    "surviving_hostile",
    "fetch_surviving_graphs",
]
