"""
Autosetup package — compilation analysis, summarization, and base config generation.

This package handles the setup phase of the PreAudit pipeline:
- Build system detection and configuration
- Compilation analysis (certoraRun --compilation_steps_only)
- LLM-based function summarization
- Signature database generation
- Sanity detection (loop-iter, hashing bounds)
- Call resolution
- Base config and warmup spec creation
"""

from certora_autosetup.autosetup.autosetup import Autosetup
from certora_autosetup.autosetup.types import AutosetupConfig, AutosetupResult

__all__ = [
    "Autosetup",
    "AutosetupConfig",
    "AutosetupResult",
]
