"""Summarization-target detector — a standalone AutoProver tool.

From ONE prover run it ranks the functions worth summarizing and says HOW (per-function over-approx vs
whole-contract symbolic model), so autosetup can summarize the expensive ones before paying for them. It
is a SEPARATE tool from smtool — it decides WHAT to summarize; smtool then generates the summaries — and
only reuses AutoProver code (`smtool.difficulty`, `certora_autosetup`). Invoke via `detect()` or the CLI
(`python -m summarization_detector` / `detect-summaries`).
"""
from .detect import (
    Candidate,
    DetectionReport,
    HashSignal,
    detect,
    detect_from,
    scan_ast,
    reachable_from_main,
    cone_weights,
)
from .sources import detect_url, cut_from_conf, find_run_conf

__all__ = [
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
]
