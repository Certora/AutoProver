#!/usr/bin/env python3
"""
Setup Completeness Checker for PreAudit.

Analyzes JobReport objects to identify setup issues that may affect verification quality.
Checks for: unresolved calls, timeouts, errors/unknown results, sanity failures, and PTA issues.
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from prover_output_utility import AlertType, JobReport, ParsedAlert
from prover_output_utility.models import CallResolutionInfo, CheckResult

from certora_autosetup.setup.sanity import SanityAnalysis


class SetupIssueType(Enum):
    """Types of setup completeness issues."""

    UNRESOLVED_CALL = "Unresolved Calls"
    TIMEOUT = "Timeouts"
    ERROR_OR_UNKNOWN = "Errors/Unknown"
    SANITY_FAILURE = "Sanity Failures"
    ALERT = "Alerts"  # Prover alerts; specific type stored in SetupIssue.alert_type


# Alert types relevant for setup completeness checking.
# Other alert types (e.g., GENERAL, CVL, CACHE) are informational and not setup issues.
SETUP_RELEVANT_ALERT_TYPES: set[AlertType] = {
    AlertType.STORAGE_ANALYSIS,
    AlertType.STORAGE_SPLITTING,
    AlertType.CALL_GRAPH,
    AlertType.PTA_FOR_OPTIMIZATIONS,
    AlertType.MEMORY_PARTITIONING,
    AlertType.INTERNAL_FUNCTION_ANALYSIS,
    AlertType.ANALYSIS,
    AlertType.OUT_OF_RESOURCES,
}


class SetupIssueSeverity(Enum):
    """Severity levels for setup issues."""

    WARNING = "warning"
    ERROR = "error"
    INFO = "info"


@dataclass
class SetupIssue:
    """Represents a single setup completeness issue."""

    issue_type: SetupIssueType
    severity: SetupIssueSeverity
    message: str
    source_location: Optional[str] = None
    contract: Optional[str] = None
    function: Optional[str] = None
    caller: Optional[str] = None  # For unresolved calls: the function making the call
    rule_name: Optional[str] = None
    job_url: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    count: int = 1  # Number of occurrences of this issue
    alert_type: Optional[AlertType] = None  # For ALERT issues: the specific alert type

    def dedup_key(self) -> tuple:
        """Return a key for deduplication. Issues with the same key are considered duplicates."""
        return (self.issue_type, self.alert_type, self.contract, self.function, self.caller, self.rule_name, self.message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: Dict[str, Any] = {
            "type": self.issue_type.name.lower(),
            "severity": self.severity.value,
            "message": self.message,
        }
        if self.alert_type:
            result["alert_type"] = self.alert_type.value
        if self.source_location:
            result["source_location"] = self.source_location
        if self.contract:
            result["contract"] = self.contract
        if self.function:
            result["function"] = self.function
        if self.caller:
            result["caller"] = self.caller
        if self.rule_name:
            result["rule_name"] = self.rule_name
        if self.job_url:
            result["job_url"] = self.job_url
        if self.count > 1:
            result["count"] = self.count
        return result


@dataclass
class SetupCompletenessReport:
    """Aggregated setup completeness report for one or more prover runs."""

    reports_dir: Path
    issues_by_type: Dict[SetupIssueType, List[SetupIssue]] = field(default_factory=dict)
    filename_prefix: str = "setup_completeness"

    @property
    def md_path(self) -> Path:
        return self.reports_dir / f"{self.filename_prefix}_report.md"

    @property
    def json_path(self) -> Path:
        return self.reports_dir / f"{self.filename_prefix}_report.json"

    def get_issues(self, issue_type: SetupIssueType) -> List[SetupIssue]:
        """Get issues of a specific type."""
        return self.issues_by_type.get(issue_type, [])

    def add_issues(self, issues: Sequence[SetupIssue]) -> None:
        """Add issues to the report, deduplicating by incrementing count for duplicates."""
        for issue in issues:
            issue_list = self.issues_by_type.setdefault(issue.issue_type, [])
            key = issue.dedup_key()
            # Check if duplicate exists
            for existing in issue_list:
                if existing.dedup_key() == key:
                    existing.count += issue.count
                    break
            else:
                issue_list.append(issue)

    @property
    def total_issues(self) -> int:
        """Total count of all issues."""
        return sum(len(issues) for issues in self.issues_by_type.values())

    @property
    def has_issues(self) -> bool:
        """Whether any issues were found."""
        return self.total_issues > 0

    def to_markdown(self) -> str:
        """Generate Markdown report content."""
        lines = [
            "# Setup Completeness Report",
            "",
            f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## Summary",
            "",
        ]
        for issue_type in SetupIssueType:
            count = len(self.get_issues(issue_type))
            anchor = issue_type.value.lower().replace(" ", "-").replace(":", "").replace("/", "")
            lines.append(f"- **[{issue_type.value}](#{anchor}):** {count}")
        lines.append("")

        if not self.has_issues:
            lines.append("**No setup completeness issues found.**")
            return "\n".join(lines)

        # Detailed sections
        for issue_type in SetupIssueType:
            issues = self.get_issues(issue_type)
            if issues:
                lines.extend(self._format_markdown_section(issue_type.value, issues))

        return "\n".join(lines)

    def _format_markdown_section(self, title: str, issues: List[SetupIssue]) -> List[str]:
        """Format a section of issues for Markdown output."""
        lines = [f"## {title}", ""]

        for i, issue in enumerate(issues, 1):
            header_msg = issue.message[:80] + ("..." if len(issue.message) > 80 else "")
            count_suffix = f" (x{issue.count})" if issue.count > 1 else ""
            lines.append(f"### {i}. {header_msg}{count_suffix}")
            lines.append("")

            if issue.count > 1:
                lines.append(f"- **Occurrences:** {issue.count}")
            if issue.alert_type:
                lines.append(f"- **Alert Type:** {issue.alert_type.value}")
            if issue.contract:
                lines.append(f"- **Contract:** {issue.contract}")
            if issue.function:
                lines.append(f"- **Function:** {issue.function}")
            if issue.caller:
                lines.append(f"- **Called from:** {issue.caller}")
            if issue.rule_name:
                lines.append(f"- **Rule:** {issue.rule_name}")
            if issue.source_location:
                lines.append(f"- **Location:** `{issue.source_location}`")
            if issue.job_url:
                lines.append(f"- **Job:** [{issue.job_url}]({issue.job_url})")
            lines.append("")

        return lines

    def to_json(self) -> Dict[str, Any]:
        """Generate JSON report data."""
        summary: Dict[str, Any] = {"total_issues": self.total_issues}
        issues_dict: Dict[str, Any] = {}

        for issue_type in SetupIssueType:
            type_issues = self.get_issues(issue_type)
            key = issue_type.name.lower()
            summary[key] = len(type_issues)
            issues_dict[key] = [i.to_dict() for i in type_issues]

        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "issues": issues_dict,
        }

    def save(self) -> None:
        """Save both Markdown and JSON reports."""
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        with open(self.md_path, "w") as f:
            f.write(self.to_markdown())

        with open(self.json_path, "w") as f:
            json.dump(self.to_json(), f, indent=2)


class SetupCompletenessChecker:
    """Analyzes JobReport objects to identify setup completeness issues."""

    def analyze_job_report(self, job_report: JobReport) -> List[SetupIssue]:
        """Analyze a JobReport for setup issues."""
        job_url = job_report.job_url
        issues: List[SetupIssue] = []
        issues.extend(SetupCompletenessChecker._convert_unresolved_calls(job_report.unresolved_calls, job_url))
        issues.extend(SetupCompletenessChecker._convert_checks(job_report.timeout_rules, SetupIssueType.TIMEOUT, job_url))
        issues.extend(SetupCompletenessChecker._convert_checks(job_report.error_rules, SetupIssueType.ERROR_OR_UNKNOWN, job_url))
        sanity = SanityAnalysis.from_job_report(job_report)
        # One issue per method that failed sanity (VIOLATED). Inconclusive methods
        # (ERROR/TIMEOUT) are covered by the error/timeout conversions above.
        for method in sanity.methods_failed:
            issues.append(
                SetupIssue(
                    issue_type=SetupIssueType.SANITY_FAILURE,
                    severity=SetupIssueSeverity.WARNING,
                    message=f"Sanity check failed for method {method}",
                    function=method,
                    rule_name="sanity",
                    job_url=job_url,
                )
            )
        # Flag a run that did not complete (HALTED/FAILED/...); this has no per-method analog.
        if not sanity.job_completed:
            issues.append(
                SetupIssue(
                    issue_type=SetupIssueType.SANITY_FAILURE,
                    severity=SetupIssueSeverity.WARNING,
                    message=f"Sanity run did not complete: {sanity.status_summary()}",
                    rule_name="sanity",
                    job_url=job_url,
                )
            )
        issues.extend(SetupCompletenessChecker._convert_alerts(job_report.alerts_by_type, job_url))
        return issues

    @staticmethod
    def _convert_unresolved_calls(calls: List[CallResolutionInfo], job_url: Optional[str]) -> List[SetupIssue]:
        """Convert unresolved calls to SetupIssue objects."""
        issues = []
        for call in calls:
            msg = f"Unresolved call to {call.callee_name} from {call.caller_name}"
            if call.call_site_snippet:
                msg += f": {call.call_site_snippet[:100]}"
            issues.append(
                SetupIssue(
                    issue_type=SetupIssueType.UNRESOLVED_CALL,
                    severity=SetupIssueSeverity.WARNING,
                    message=msg,
                    source_location=call.source_location,
                    function=call.callee_name,
                    caller=call.caller_name,
                    job_url=job_url,
                )
            )
        return issues

    @staticmethod
    def _convert_checks(checks: List[CheckResult], issue_type: SetupIssueType, job_url: Optional[str]) -> List[SetupIssue]:
        """Convert CheckResult objects to SetupIssue objects."""
        issues = []
        for c in checks:
            if issue_type == SetupIssueType.TIMEOUT:
                msg = f"Rule '{c.rule_name}' timed out for method {c.method_name or 'unknown'}"
            elif issue_type == SetupIssueType.ERROR_OR_UNKNOWN:
                msg = f"Rule '{c.rule_name}' has status {c.status} for method {c.method_name or 'unknown'}"
            elif issue_type == SetupIssueType.SANITY_FAILURE:
                msg = f"Sanity check failed for method {c.method_name or 'unknown'}"
                if c.assert_message:
                    msg += f": {c.assert_message}"
            else:
                msg = f"Rule '{c.rule_name}' with status {c.status}"
            issues.append(
                SetupIssue(
                    issue_type=issue_type,
                    severity=SetupIssueSeverity.WARNING,
                    message=msg,
                    contract=c.contract_name,
                    function=c.method_name,
                    rule_name=c.rule_name,
                    job_url=job_url,
                )
            )
        return issues

    @staticmethod
    def _convert_alerts(alerts_by_type: Dict[AlertType, List[ParsedAlert]], job_url: Optional[str]) -> List[SetupIssue]:
        """Convert alerts to SetupIssue objects, filtering to setup-relevant types only."""
        issues = []

        for alert_type, parsed_alerts in alerts_by_type.items():
            # Only include alerts relevant to setup completeness
            if alert_type not in SETUP_RELEVANT_ALERT_TYPES:
                continue

            severity_map = {
                "ERROR": SetupIssueSeverity.ERROR,
                "WARNING": SetupIssueSeverity.WARNING,
                "INFO": SetupIssueSeverity.INFO,
            }

            for parsed_alert in parsed_alerts:
                severity = severity_map.get(parsed_alert.severity, SetupIssueSeverity.WARNING)

                issues.append(
                    SetupIssue(
                        issue_type=SetupIssueType.ALERT,
                        severity=severity,
                        message=parsed_alert.message,
                        source_location=parsed_alert.jump_to_definition,
                        job_url=job_url,
                        alert_type=alert_type,
                    )
                )

        return issues
