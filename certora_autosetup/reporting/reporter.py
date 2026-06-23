
import json
import json5
import sys
import os
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import required dependencies
from prover_output_utility import ProverOutputAPI  # type: ignore[import-untyped]
from prover_output_utility.models import CheckResult # type: ignore[import-untyped]

from certora_autosetup.reporting.markdown_reporter import MarkdownReporter
from certora_autosetup.setup.sanity import SanityAnalysis, SanityFailureResult
from sanity_analyzer.analysis import SanityAnalysisResult  # type: ignore[import-not-found]
from certora_autosetup.setup.setup_completeness_checker import SetupCompletenessReport, SetupIssueType
from prover_output_utility import JobReport, ProverOutputAPI
from certora_autosetup.utils.constants import DIR_CERTORA_INTERNAL
from certora_autosetup.utils.runner_types import ProverResult
from certora_autosetup.utils.types import ContractHandle


@dataclass
class JobRuleStats:
    """Statistics for a verification job's rules."""
    verified: int = 0
    violations: int = 0
    timeout: int = 0
    unknown: int = 0
    violation_rules: List[CheckResult] = field(default_factory=list)
    job_url: str = "Unknown Job URL"




@dataclass
class _SanityRow:
    """Raw per-contract data for one row of the sanity summary table."""
    contract_name: str
    job_url: str
    sanity_status: str  # "PASS" or "FAIL"
    method_failures: Dict[str, "SanityFailureResult"]
    unresolved_count: int
    storage_extension: bool
    global_warnings: int
    runtime: str
    optimistic_loop: bool
    loop_iter: int | str
    optimistic_hashing: bool
    hashing_bounds: int | str




class Reporter:
    def __init__(self, log, verbose, skip_breadcrumbs, reports_dir, prover_api: ProverOutputAPI):
        self.log = log
        self.verbose = verbose
        self.skip_breadcrumbs = skip_breadcrumbs
        self.reports_dir = Path(reports_dir)
        self._prover_api = prover_api

    def ensure_reports_dir(self) -> None:
        """Create the reports directory if it doesn't exist yet."""
        if not self.reports_dir.exists():
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            self.log(f"Created reports directory: {self.reports_dir}")

    def generate_report(self, job_url: str, report_name: str) -> JobRuleStats:
        """Generate a markdown report for a specific job and return statistics."""
        # Guard against recursion
        if not hasattr(self, '_generating_reports'):
            self._generating_reports: set[str] = set()

        report_key = f"{job_url}:{report_name}"
        if report_key in self._generating_reports:
            self.log(f"⚠️ Avoiding recursive report generation for {report_name}", "WARNING")
            return JobRuleStats()

        self._generating_reports.add(report_key)

        report_stats = JobRuleStats()

        if not job_url:
            self.log(f"No job URL available for {report_name} report generation", "WARNING")
            self._generating_reports.discard(report_key)
            return report_stats
        report_stats.job_url = job_url

        self.log(f"=== GENERATING {report_name.upper()} REPORT ===")

        try:
            # Fetch job data once and reuse it with timeout protection
            import signal
            from contextlib import contextmanager

            @contextmanager
            def timeout(seconds):
                def timeout_handler(signum, frame):
                    raise TimeoutError(f"Operation timed out after {seconds} seconds")

                # Set the timeout handler
                old_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(seconds)
                try:
                    yield
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)

            api = ProverOutputAPI()

            # NOTE: we deliberately do NOT bulk-download the whole zipOutput here.
            # download_job_outputs() pulls the entire job tar.gz into memory
            # (response.content + an io.BytesIO copy) and re-serializes every JSON file —
            # hundreds of MB for large jobs — which OOM-kills small machines. The report
            # only needs treeViewStatus.json (via get_leaf_checks) plus the per-violation
            # rule_output_*.json / breadcrumb files, which get_rule_hierarchy /
            # get_breadcrumbs fetch individually below through cheap, per-file POU calls
            # (each small JSON, cached to disk per file). Same data, same report output,
            # bounded memory.
            all_checks = None
            # Try to fetch with retries and exponential backoff
            # Use fewer retries in CI environments to fail faster
            max_retries = 3 if os.getenv('CI') else 10
            base_timeout = 60

            for retry in range(max_retries):
                try:
                    # Exponential backoff for timeout: 60s, 120s, 240s
                    current_timeout = base_timeout * (2 ** retry)

                    with timeout(current_timeout):
                        if retry > 0:
                            # Wait before retry with exponential backoff: 2s, 4s, 8s
                            import time
                            wait_time = 2 ** retry
                            self.log(f"Retry {retry}/{max_retries} after {wait_time}s delay...")
                            time.sleep(wait_time)

                        self.log(f"Fetching checks from {job_url} (timeout: {current_timeout}s, attempt {retry + 1}/{max_retries})...")
                        all_checks = MarkdownReporter.get_validated_checks(api, job_url)
                        self.log(f"Successfully fetched {len(all_checks) if all_checks else 0} checks")
                        break  # Success, exit retry loop

                except TimeoutError as e:
                    self.log(f"⚠️ Timeout on attempt {retry + 1}/{max_retries} for {report_name}: {e}", "WARNING")
                    if retry == max_retries - 1:
                        self.log(f"❌ Failed to fetch checks after {max_retries} attempts", "ERROR")
                        all_checks = None
                except Exception as e:
                    self.log(f"⚠️ Error on attempt {retry + 1}/{max_retries} for {report_name}: {str(e)}", "WARNING")
                    if retry == max_retries - 1:
                        self.log(f"❌ Failed to fetch checks after {max_retries} attempts", "ERROR")
                        all_checks = None

            # Calculate statistics from the fetched data
            if all_checks:
                for i, check in enumerate(all_checks):
                    if hasattr(check, 'status'):
                        status = check.status.upper()

                        if 'VERIFIED' in status or 'PASSED' in status:
                            report_stats.verified += 1
                        elif 'VIOLATED' in status or 'FAILED' in status:
                            report_stats.violations += 1
                            report_stats.violation_rules.append(check)
                        elif 'TIMEOUT' in status:
                            report_stats.timeout += 1
                        else:
                            report_stats.unknown += 1

            # Generate the report using the pre-fetched data
            reporter = MarkdownReporter(verbose=self.verbose, skip_breadcrumbs=self.skip_breadcrumbs)
            self.ensure_reports_dir()  # Create directory if needed
            output_file = self.reports_dir / f"{report_name}_report.md"

            # Add timeout protection for report generation (5 minutes max)
            try:
                with timeout(300):
                    self.log("Generating markdown report (300s timeout)...")
                    reporter.generate_report_with_data(job_url, str(output_file), all_checks)
            except TimeoutError as e:
                self.log(f"⚠️ Timeout generating markdown for {report_name}: {e}", "WARNING")
                # Write a minimal report indicating timeout
                with open(output_file, 'w') as f:
                    f.write(f"# Report: {report_name}\n\n")
                    f.write(f"**Job URL:** {job_url}\n\n")
                    f.write("⚠️ **Report generation timed out after 5 minutes**\n\n")
                    f.write("This typically happens with very large verification jobs.\n")
                    f.write(f"Please visit the job URL directly to view results: {job_url}\n")

            self.log(f"✅ {report_name.title()} report complete - check {output_file}")

        except Exception as e:
            self.log(f"❌ Failed to generate {report_name} report: {e}", "ERROR")
            # Print traceback for debugging
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
        finally:
            # Always clean up the recursion guard
            self._generating_reports.discard(report_key)

        return report_stats

    def generate_comprehensive_report(
        self,
        verification_results: List[ProverResult],
        all_report_stats: dict[str, Any],
        setup_report: Optional[SetupCompletenessReport] = None,
        bytes_mappings: list[tuple[ContractHandle, list[str]]] = []):
        """Generate comprehensive reports for all completed orchestrator runs."""
        self.log("=== GENERATING COMPREHENSIVE REPORTS FOR ALL COMPLETED JOBS ===")
        try:
            # Extract data from verification results using three categories
            successful_results = [
                result
                for result in verification_results
                if result.is_success()
            ]
            skipped_results = [
                result
                for result in verification_results
                if result.is_skipped()
            ]
            failed_results = [
                result
                for result in verification_results
                if result.is_failure()
            ]

            # Create a combined report
            certora_internal = Path(DIR_CERTORA_INTERNAL)
            certora_internal.mkdir(parents=True, exist_ok=True)
            output_file = certora_internal / "orchestrator_comprehensive_report.md"

            with open(output_file, "w") as f:
                f.write("# Orchestrator Comprehensive Report\n\n")
                f.write(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("## Overview\n\n")
                f.write(f"- **Successful submissions:** {len(successful_results)}\n")
                f.write(f"- **Skipped submissions:** {len(skipped_results)}\n")
                f.write(f"- **Failed submissions:** {len(failed_results)}\n")
                f.write(f"- **Total attempts:** {len(verification_results)}\n\n")

                bytes_mapping_warning = self.bytes_mapping_warning(bytes_mappings)
                if bytes_mapping_warning:
                    f.write("## WARNING\n\n")
                    for line in bytes_mapping_warning:
                        f.write(f"{line}\n")
                    f.write("\n")

                # Add failed jobs section if any
                if failed_results:
                    f.write("## Failed Submissions\n\n")
                    f.write("The following jobs failed to submit:\n\n")
                    for result in failed_results:
                        f.write(f"### {result.job_handle.phase}\n\n")

                        # Use actual certoraRun output if available, fallback to error_message
                        actual_output = (
                            result.output_data.get("output", "")
                            if result.output_data
                            else ""
                        )
                        display_error = (
                            actual_output.strip()
                            if actual_output.strip()
                            else (result.error_message or "Unknown error")
                        )

                        f.write(f"- **Error:** {display_error}\n\n")

                        # Show full error details in an expandable section
                        f.write("**Full Error Details:**\n\n")
                        f.write("<details>\n")
                        f.write("<summary>Click to expand error details</summary>\n\n")
                        f.write("```\n")
                        f.write(display_error)
                        f.write("\n```\n")
                        f.write("</details>\n\n")
                    f.write("\n")

                # Add skipped jobs section if any
                if skipped_results:
                    f.write("## Skipped Submissions\n\n")
                    f.write("The following jobs were skipped due to filtering:\n\n")
                    for result in skipped_results:
                        f.write(f"### {result.job_handle.phase}\n\n")

                        # Use actual certoraRun output for skipped results
                        actual_output = (
                            result.output_data.get("output", "")
                            if result.output_data
                            else ""
                        )
                        display_message = (
                            actual_output.strip()
                            if actual_output.strip()
                            else "No valid instantiations found"
                        )

                        f.write(f"- **Reason:** {display_message}\n\n")

                        # Show details in an expandable section
                        f.write("**Details:**\n\n")
                        f.write("<details>\n")
                        f.write("<summary>Click to expand output details</summary>\n\n")
                        f.write("```\n")
                        f.write(display_message)
                        f.write("\n```\n")
                        f.write("</details>\n\n")
                    f.write("\n")

                # Add links to all successful job URLs
                f.write("## Successful Job URLs\n\n")
                for result in successful_results:
                    job_id = result.job_handle.job_id
                    phase = result.job_handle.phase
                    if job_id.startswith("http"):
                        f.write(f"- **{phase}**: {job_id}\n")
                    else:
                        f.write(f"- **{phase}**: {job_id} (local)\n")
                f.write("\n")

            all_violations = []
            # Collect violations from stored stats
            for report_name, stats in all_report_stats.items():
                # Collect violations for summary section
                for rule in stats.violation_rules:
                    all_violations.append(
                        {"report": report_name, "rule": rule, "url": stats.job_url}
                    )

                # Append to comprehensive report with statistics
                with open(output_file, "a") as f:
                    f.write(f"\n## Report: {report_name}\n\n")
                    f.write("**Summary:** ")
                    if stats.verified > 0:
                        f.write(f"✅ {stats.verified} verified ")
                    if stats.violations > 0:
                        f.write(f"❌ {stats.violations} violations ")
                    if stats.timeout > 0:
                        f.write(f"⏱️ {stats.timeout} timeout ")
                    if stats.unknown > 0:
                        f.write(f"❓ {stats.unknown} unknown ")
                    f.write("\n\n")
                    f.write(
                        f"See detailed report: [{report_name}_report.md](./{report_name}_report.md)\n\n"
                    )

            # Add violations summary section at the end
            if all_violations:
                with open(output_file, "a") as f:
                    f.write("\n## All Violations Summary\n\n")
                    f.write(
                        "Quick links to all violations found across all reports:\n\n"
                    )
                    for violation in all_violations:
                        f.write(
                            f"- **{violation['report']}** - `{violation['rule']}`: [View on Prover]({violation['url']})\n"
                        )
                    f.write("\n")

            # Add setup completeness summary if provided
            if setup_report and setup_report.has_issues:
                with open(output_file, "a") as f:
                    f.write("\n## Setup Completeness Summary\n\n")
                    f.write(f"Found {setup_report.total_issues} setup issue(s):\n\n")
                    for issue_type in SetupIssueType:
                        count = len(setup_report.get_issues(issue_type))
                        if count > 0:
                            f.write(f"- **{issue_type.value}:** {count}\n")
                    if setup_report.md_path:
                        f.write(f"\nSee detailed report: [{setup_report.md_path.name}](./{setup_report.md_path.name})\n")
                    f.write("\n")

            self.log(f"✅ Comprehensive report complete - check {output_file}")
            self.log("Individual reports generated for each configuration")

        except Exception as e:
            self.log(f"❌ Failed to generate comprehensive report: {e}", "ERROR")
            import traceback

            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")

    def generate_specific_summarized_report(
        self,
        verification_results: List[ProverResult],
        all_report_stats: dict[str, Any],
        phase_filter: str,
        overview_text: str
    ) -> None:
        """Generate a summarized report for specific checker results.

        Args:
            verification_results: List of ProverResult objects to filter
            all_report_stats: dict of statistics from orchestrator
            phase_filter: String to filter job phases (e.g., "SafeCasts", "UncheckedOverflows")
            overview_text: Text to display in the overview section
        """
        self.log(f"=== GENERATING {phase_filter.upper()} SUMMARY REPORT ===")

        try:
            # Filter for specific phase results only
            filtered_results = [
                result
                for result in verification_results
                if phase_filter in result.job_handle.phase
                and result.is_success()
            ]

            if not filtered_results:
                self.log(f"No {phase_filter} results found - skipping {phase_filter} report", "WARNING")
                return

            # Create summary report
            self.ensure_reports_dir()
            output_filename = f"{phase_filter.lower().replace(' ', '_')}_summary_report.md"
            output_file = self.reports_dir / output_filename

            with open(output_file, "w") as f:
                f.write(f"# {phase_filter} Summary Report\n\n")
                f.write(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("## Overview\n\n")
                f.write(f"This report summarizes all {phase_filter} checker results across {len(filtered_results)} contract(s).\n\n")
                f.write(f"{overview_text}\n\n")
                f.write("Please let us know if any of the violations are uninteresting to you, so we may better filter them in the future.\n")
                f.write("Uninteresting here meaning, they are not only safe in the context of the project you are investigating, but you can explain 'An occurrence that follows this pattern will never be a real problem' in some way.\n")
                f.write("Please also let us know if there is information missing in this report that would help you triage the violations faster.\n")
                f.write("You can follow the prover links to try to get additional context on the problematic execution, but ideally, you should not need to.\n\n")

                total_verified = 0
                total_violations = 0
                total_timeout = 0
                total_unknown = 0
                all_violations = []

                # Filter stats for phase-specific reports
                phase_stats = {
                    name: stats
                    for name, stats in all_report_stats.items()
                    if phase_filter in name
                }

                # Aggregate statistics from report_stats
                for report_name, stats in phase_stats.items():
                    total_verified += stats.verified
                    total_violations += stats.violations
                    total_timeout += stats.timeout
                    total_unknown += stats.unknown

                    # Collect violations with context from violation_rules
                    for check_result in stats.violation_rules:
                        all_violations.append({
                            "contract": report_name.split("-")[-1],
                            "rule": check_result.rule_name,
                            "status": check_result.status,
                            "url": stats.job_url,
                            "method": check_result.method_name,
                            "assert_message": check_result.assert_message,
                            "location": check_result.source_location
                        })

                # Write aggregate statistics
                f.write("## Aggregate Statistics\n\n")
                f.write(f"- ✅ **Verified:** {total_verified}\n")
                f.write(f"- ❌ **Violations:** {total_violations}\n")
                f.write(f"- ⏱️ **Timeout:** {total_timeout}\n")
                f.write(f"- ❓ **Unknown:** {total_unknown} (These can be contracts with nothing to check, or further errors and timeouts)\n\n")

                # Add violations section if any
                if total_violations > 0:
                    f.write("## Violations Found\n\n")
                    f.write(f"Found {total_violations} violation(s) across {phase_filter} checks:\n\n")

                    # Group violations by contract
                    violations_by_contract = {}
                    for violation in all_violations:
                        contract = violation["contract"]
                        if contract not in violations_by_contract:
                            violations_by_contract[contract] = []
                        violations_by_contract[contract].append(violation)

                    for contract, violations in sorted(violations_by_contract.items()):
                        f.write(f"### {contract}\n\n")
                        f.write(f"Prover Run: [link]({violations[0]['url']})\n\n")
                        violations_by_assert = {}
                        for violation in violations:
                            assert_message = violation["assert_message"]
                            if assert_message not in violations_by_assert:
                                violations_by_assert[assert_message] = []
                            violations_by_assert[assert_message].append(violation)
                        for assert_message, occurrences in sorted(violations_by_assert.items()):
                            if occurrences[0]['location']:
                                # this will print the full path to the file with the violation if we manage to get jts for these rules
                                f.write(f"In {occurrences[0]['location']}:\n")
                            f.write(f"{assert_message}\n")
                            f.write(f"Reachable from external functions:\n")
                            for occurrence in occurrences:
                                f.write(f"- {occurrence['method']}\n")

                            f.write("\n")
                        f.write("\n")
                else:
                    f.write("## ✅ No Violations Found\n\n")

                # Add job URLs section
                f.write("## All Job URLs - Just in case, no need to look at these in general!\n\n")
                for result in filtered_results:
                    job_id = result.job_handle.job_id
                    phase = result.job_handle.phase
                    if job_id.startswith("http"):
                        f.write(f"- **{phase}**: {job_id}\n")
                    else:
                        f.write(f"- **{phase}**: {job_id} (local)\n")
                f.write("\n")

            self.log(f"✅ {phase_filter} summary report complete - check {output_file}")

        except Exception as e:
            self.log(f"❌ Failed to generate {phase_filter} report: {e}", "ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")

    def generate_safecast_report(
        self, verification_results: List[ProverResult], all_report_stats: dict[str, Any]
    ) -> None:
        """Generate a summarized report for SafeCasts checker results.

        Args:
            verification_results: List of ProverResult objects to filter for SafeCasts results
            all_report_stats: dict of statistics from orchestrator
        """
        overview_text = (
            "A SafeCast violation highlights a cast that is potentially unsafe and can be reached from an external function with a value outside the safe ranges.\n"
            "Typically, the same cast can be reached from different external functions, in which case we will list the entrypoints from which an execution with an unsafe value can be found."
        )
        self.generate_specific_summarized_report(
            verification_results,
            all_report_stats,
            "SafeCasts",
            overview_text
        )

    def generate_unchecked_overflow_report(
        self, verification_results: List[ProverResult], all_report_stats: dict[str, Any]
    ) -> None:
        """Generate a summarized report for UncheckedOverflows checker results.

        Args:
            verification_results: List of ProverResult objects to filter for UncheckedOverflows results
            all_report_stats: dict of statistics from orchestrator
        """
        overview_text = (
            "An UncheckedOverflows violation highlights a possibly overflowing operation within an `unchecked` code block that can be reached from an external function with values that lead to an overflow.\n"
            "Typically, the same operation can be reached from different external functions, in which case we will list the entrypoints from which an execution with an overflowing value can be found."
        )
        self.generate_specific_summarized_report(
            verification_results,
            all_report_stats,
            "UncheckedOverflows",
            overview_text
        )

    def generate_summarized_reports(
        self, verification_results: List[ProverResult], all_report_stats: dict[str, Any]
    ) -> None:
        """Generate summarized reports for individual checks like SafeCasts and UncheckedOverflows

        Args:
            verification_results: List of ProverResult objects to filter for UncheckedOverflows results
            all_report_stats: dict of statistics from orchestrator
        """
        self.generate_safecast_report(verification_results, all_report_stats)
        self.generate_unchecked_overflow_report(verification_results, all_report_stats)

    def _collect_sanity_rows(
        self,
        verification_results: List[ProverResult],
        sanity_advanced: Dict[str, Dict[str, SanityFailureResult]] | None,
    ) -> List[_SanityRow]:
        sanity_results: dict[str, ProverResult] = {}
        for result in verification_results:
            if result.job_handle.phase.startswith("Sanity Test Run"):
                sanity_results[result.job_spec.contract_name] = result

        rows: List[_SanityRow] = []
        for contract_name, result in sorted(sanity_results.items()):
            if not result.job_url:
                continue

            config: dict[str, Any] = {}
            try:
                with open(result.job_spec.config_file.path) as f:
                    config = json5.load(f)
            except Exception as e:
                self.log(f"Failed to read config for {contract_name}: {e}", "WARNING")

            jr = self._prover_api.get_job_report(result.job_url)
            job_report_path = self.reports_dir / f"job_report_{contract_name}.json"
            with open(job_report_path, "w") as f:
                json.dump(jr.to_dict(), f, indent=2)

            # Use prover job start/finish times for actual runtime
            prover_start = result.output_data.get("prover_start_time")
            prover_finish = result.output_data.get("prover_finish_time")
            if prover_start is not None and prover_finish is not None:
                delta = datetime.fromisoformat(prover_finish) - datetime.fromisoformat(prover_start)
                runtime = str(delta).split(".")[0]  # HH:MM:SS, drop microseconds
            else:
                runtime = "N/A"

            rows.append(_SanityRow(
                contract_name=contract_name,
                job_url=result.job_url,
                sanity_status=SanityAnalysis.from_job_report(jr).status_summary(),
                method_failures=sanity_advanced.get(contract_name, {}) if sanity_advanced else {},
                unresolved_count=len(jr.unresolved_calls),
                storage_extension=config.get("storage_extension_annotation", False),
                global_warnings=sum(len(a) for a in jr.alerts_by_type.values()),
                runtime=runtime,
                optimistic_loop=config.get("optimistic_loop", False),
                loop_iter=config.get("loop_iter", "N/A"),
                optimistic_hashing=config.get("optimistic_hashing", False),
                hashing_bounds=config.get("hashing_length_bound", "N/A"),
            ))
        return rows

    def _write_sanity_markdown(
        self,
        rows: List[_SanityRow],
        output_file: Path,
        bytes_mappings: list[tuple[ContractHandle, list[str]]] | None = None,
    ) -> None:
        lines = [
            "# Setup Summary",
            "",
            f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Reports directory:** `{self.reports_dir}`",
            "",
        ]
        warning_lines = self.bytes_mapping_warning(bytes_mappings or [])
        for row in rows:
            link = f"[job link]({row.job_url})" if row.job_url else "N/A"
            lines.append(f"## {row.contract_name}")
            lines.append("")
            lines.append(f"- **Sanity:** {row.sanity_status} | {link}")
            lines.append(f"- **Runtime:** {row.runtime}")
            lines.append(f"- **Unresolved Calls:** {row.unresolved_count}")
            lines.append(f"- **Storage Extension Annotation:** {row.storage_extension}")
            lines.append(f"- **Global Warnings:** {row.global_warnings}")
            lines.append(f"- **Optimistic Loops:** {row.optimistic_loop} | **Loop Iter:** {row.loop_iter}")
            lines.append(f"- **Optimistic Hashing:** {row.optimistic_hashing} | **Hashing Bounds:** {row.hashing_bounds}")
            if row.method_failures:
                lines.append("")
                lines.append("### Sanity Analysis")
                lines.append("")
                for method_name in sorted(row.method_failures):
                    sfr = row.method_failures[method_name]
                    if sfr.sanity_analysis is None:
                        continue
                    summary = sfr.sanity_analysis.short_summary
                    lines.append(f"**{method_name}**: {summary}")
            lines.append("")
        if warning_lines:
            lines.append("---")
            lines.append("")
            lines.append("## ⚠️ Bytes Mappings Warning")
            lines.append("")
            for wl in warning_lines:
                lines.append(wl)
                lines.append("")
        with open(output_file, "w") as f:
            f.write("\n".join(lines))

    def generate_sanity_summary(
        self,
        verification_results: List[ProverResult],
        sanity_advanced: Dict[str, Dict[str, SanityFailureResult]] | None = None,
        bytes_mappings: list[tuple[ContractHandle, list[str]]] | None = None,
    ) -> None:
        """Generate per-contract sanity summary as markdown (sanity_summary.md).

        Uses the "Sanity Test Run - {contract}" results as the per-contract anchor.
        """
        self.log("=== GENERATING SANITY SUMMARY TABLE ===")
        self.ensure_reports_dir()
        rows = self._collect_sanity_rows(verification_results, sanity_advanced)
        if not rows:
            self.log("No sanity test run results found - skipping setup summary table", "WARNING")
            return
        self._write_sanity_markdown(rows, self.reports_dir / "sanity_summary.md", bytes_mappings)
        self.log(f"Setup summary table written to {self.reports_dir}")

    def bytes_mapping_warning(self, bytes_mappings: list[tuple[ContractHandle, list[str]]]) -> list[str]:
        res = []
        if bytes_mappings:
            res.append("Found mappings with bytes keys in the project.")
            res.append("Be aware that reasoning about the following fields may require assuming that the keys accessed are word-aligned (i.e. requiring `key.length % 32 == 0`):")
            for contract, bytes_mapping_fields in bytes_mappings:
                res.append(f"In {contract.to_config_str()}: " + ", ".join(bytes_mapping_fields))
            res.append("For more details, see https://www.notion.so/certora/Prover-Limitations-and-Unexpected-Behaviors-1bcfe5c14fd380cf8180ee67d6e255c6?source=copy_link#2e6fe5c14fd380c6a244d625d02c5bd1")
        return res