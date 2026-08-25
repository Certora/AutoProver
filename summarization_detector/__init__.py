"""Summarization-target detector — a standalone AutoProver tool.

From ONE prover run it ranks the functions worth summarizing and says HOW (per-function over-approx vs
whole-contract symbolic model), so a caller can summarize the expensive ones before paying for them. It
is a SEPARATE, self-contained tool — it decides WHAT to summarize; a downstream generator (curated
summaries, a symbolic-model tool, or the CVL_GEN agent) produces the actual summaries. It reads the
prover's output via its own helpers and reuses `certora_autosetup` for the AST. Invoke via `detect()` or
the CLI (`python -m summarization_detector` / `detect-summaries`).
"""
from .detect import (
    Boundary,
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
from .surviving import (
    HostileCandidate,
    SurvivingReport,
    scan_surviving,
    classify,
    detect_surviving,
    fetch_surviving_postoptimize,
)
from .difficulty_profile import (
    ProfileReport,
    SlowRule,
    Hotspot,
    profile_job,
    profile_jobs,
    aggregate_by_class,
)

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
    "ProfileReport",
    "SlowRule",
    "Hotspot",
    "profile_job",
    "profile_jobs",
    "aggregate_by_class",
    "HostileCandidate",
    "SurvivingReport",
    "scan_surviving",
    "classify",
    "detect_surviving",
    "fetch_surviving_postoptimize",
]
