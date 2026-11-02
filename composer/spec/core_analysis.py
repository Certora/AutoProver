"""The report an unsat-core analyst returns, rendered for the agent that asked for it.

Shared by CVL's sanity analysis (:mod:`sanity_analyzer`) and CVLR's vacuity analysis
(:mod:`composer.spec.cvlr.vacuity`). Each keeps its own result model, whose field descriptions name
its own language and flags; this is only how either one reads once filled in.
"""

from collections.abc import Sequence
from typing import Protocol


class CoreMitigation(Protocol):
    @property
    def config_changes(self) -> Sequence[tuple[str, str]]: ...


class CoreAnalysis(Protocol):
    @property
    def issue_type(self) -> str: ...
    @property
    def mitigation_options(self) -> Sequence[CoreMitigation]: ...
    @property
    def short_summary(self) -> str: ...
    @property
    def root_cause(self) -> str: ...
    @property
    def detailed_analysis(self) -> str: ...
    @property
    def solution(self) -> str: ...


def format_core_analysis(title: str, analysis: CoreAnalysis) -> str:
    lines = [
        f"# {title}",
        "",
        f"**Issue Type:** {analysis.issue_type}",
        "",
        "## Summary",
        "",
        analysis.short_summary,
        "",
        "## Root Cause",
        "",
        analysis.root_cause,
        "",
        "## Detailed Analysis",
        "",
        analysis.detailed_analysis,
        "",
        analysis.solution,
    ]
    if any(opt.config_changes for opt in analysis.mitigation_options):
        lines += ["", "## Mitigation Options", ""]
        for i, opt in enumerate(analysis.mitigation_options, 1):
            if opt.config_changes:
                lines.append(f"**Option {i}:**")
                lines.append("```")
                lines.extend(f"{key} = {value}" for key, value in opt.config_changes)
                lines.append("```")
    return "\n".join(lines)
