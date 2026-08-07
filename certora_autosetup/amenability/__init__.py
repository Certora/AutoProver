"""certora-fv-amenability: score how amenable a Solidity project is to automatic
formal verification (autosetup) — low / medium / high — from deterministic AST
signals (phase 1) plus an LLM judge over a versioned rubric (phase 2).

See report.py for the output contract and signals/ for the individual metrics.
"""

from certora_autosetup.amenability.report import AmenabilityReport, Level

__all__ = ["AmenabilityReport", "Level"]
