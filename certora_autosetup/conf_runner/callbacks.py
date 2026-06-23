"""
Job completion callbacks for the conf runner.

Handles post-job actions: report generation, multi-assert follow-ups,
difficult retry creation, and stat merging.
"""

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Set

import json5

from prover_output_utility import ProverOutputAPI
from certora_autosetup.reporting.reporter import JobRuleStats
from certora_autosetup.setup.setup_completeness_checker import SetupCompletenessChecker, SetupCompletenessReport
from certora_autosetup.utils.enhanced_config_manager import FileContent, ProverJobSpec
from certora_autosetup.utils.paths import internal_difficult_retry_dir, internal_multi_assert_dir
from certora_autosetup.utils.runner_types import ProverResult
from certora_autosetup.utils import logger


COMPONENT = "ConfRunner"


class JobCallbacks:
    """Encapsulates all job completion callback logic.

    Manages report generation, follow-up job creation (multi-assert, difficult retry),
    stat merging, and setup completeness checking.
    """

    def __init__(
        self,
        reporter,
        json_reporter,
        setup_checker: SetupCompletenessChecker,
        aggregated_setup_report: SetupCompletenessReport,
        prover_api: ProverOutputAPI,
        retry_difficult: bool,
        skip_setup_check: bool,
        orchestration_timestamp: str,
        verbose: int = 0,
    ):
        self.reporter = reporter
        self.json_reporter = json_reporter
        self.setup_checker = setup_checker
        self.aggregated_setup_report = aggregated_setup_report
        self.prover_api = prover_api
        self.retry_difficult = retry_difficult
        self.skip_setup_check = skip_setup_check
        self.orchestration_timestamp = orchestration_timestamp
        self.verbose = verbose

        # Thread-safe state
        self.job_results: List[ProverResult] = []
        self.job_results_lock = threading.Lock()
        self.report_stats: Dict[str, Any] = {}
        self.report_stats_lock = threading.Lock()

    def log(self, message: str, level: str = "INFO"):
        logger.log(message, level, COMPONENT)

    def on_job_complete(self, result, job_queue=None) -> None:
        """Called immediately when each job completes to generate reports.

        If job_queue is provided, may add new jobs for multi_assert_check optimization.
        """
        with self.job_results_lock:
            self.job_results.append(result)

        if (
            result.success
            and hasattr(result.job_handle, "job_id")
            and result.job_handle.job_id
            and not result.job_handle.phase.startswith("Sanity Test Run")
        ):
            if result.job_handle.job_id.startswith("http"):
                try:
                    report_name = self.get_report_name_from_config_type(
                        result.job_handle.phase
                    )

                    stats: JobRuleStats = self.reporter.generate_report(
                        result.job_handle.job_id, report_name
                    )
                    with self.report_stats_lock:
                        if report_name in self.report_stats:
                            self._merge_job_stats(self.report_stats[report_name], stats)
                        else:
                            self.report_stats[report_name] = stats
                except Exception as e:
                    self.log(
                        f"⚠️ Failed to generate report for {result.job_handle.phase}: {e}",
                        "WARNING",
                    )

        if job_queue is not None:
            multi_assert_jobs = self._create_multi_assert_jobs_if_needed(result)
            multi_assert_processed = len(multi_assert_jobs) > 0

            for new_job in multi_assert_jobs:
                job_queue.put_nowait(new_job)
            if multi_assert_jobs:
                self.log(
                    f"📥 Added {len(multi_assert_jobs)} multi_assert_check job(s) for {result.job_spec.contract_name}",
                )

            difficult_retry_jobs = self._create_difficult_retry_jobs_if_needed(
                result, multi_assert_processed
            )
            for new_job in difficult_retry_jobs:
                job_queue.put_nowait(new_job)
            if difficult_retry_jobs:
                self.log(
                    f"🔄 Added {len(difficult_retry_jobs)} difficult retry job(s) for {result.job_spec.contract_name}",
                )

        if not self.skip_setup_check and result.success:
            try:
                if not result.job_url:
                    return
                job_report = self.prover_api.get_job_report(result.job_url)
                issues = self.setup_checker.analyze_job_report(job_report)
                if issues:
                    with self.report_stats_lock:
                        self.aggregated_setup_report.add_issues(issues)
                        self.log(
                            f"⚠️ Setup completeness: Found {len(issues)} issue(s) in {result.job_handle.phase}",
                            "WARNING",
                        )
                        self._save_setup_completeness_report()
            except Exception as e:
                self.log(f"Setup completeness check failed: {e}", "WARNING")

    def _create_multi_assert_jobs_if_needed(self, result: ProverResult) -> List[ProverJobSpec]:
        """Create follow-up jobs with multi_assert_check=True if conditions are met."""
        if not result.success:
            return []

        config_path = result.job_spec.config_file.path
        if not config_path.exists():
            return []

        try:
            with open(config_path) as f:
                config_data = json5.load(f)

            multi_assert_check = config_data.get("multi_assert_check", None)
            if multi_assert_check is None or multi_assert_check is True:
                return []

            rule_to_methods: Dict[str, Set[str]] = {}
            for rule_result in result.rule_results:
                if rule_result.failed:
                    rule_name = rule_result.rule_name
                    if rule_name not in rule_to_methods:
                        rule_to_methods[rule_name] = set()
                    if rule_result.method and not rule_result.method == "unknown_method":
                        rule_to_methods[rule_name].add(rule_result.method)

            if not rule_to_methods:
                return []

            self.log(
                f"🔍 Found {len(rule_to_methods)} failed rule(s) for {result.job_spec.contract_name}: "
                f"{list(rule_to_methods.keys())}",
                "DEBUG",
            )

            temp_dir = internal_multi_assert_dir(Path.cwd())
            temp_dir.mkdir(parents=True, exist_ok=True)

            new_jobs = []
            for rule_name, methods in rule_to_methods.items():
                new_config = config_data.copy()
                new_config["multi_assert_check"] = True
                new_config["rule"] = [rule_name]
                if methods:
                    new_config["method"] = list(methods)

                new_config_path = temp_dir / f"{config_path.stem}_{rule_name}_multi_assert.conf"
                with open(new_config_path, "w") as f:
                    json.dump(new_config, f, indent=4)

                contract_name = result.job_spec.contract_name
                new_job_spec = ProverJobSpec(
                    config_file=FileContent.from_file(new_config_path),
                    contract_name=contract_name,
                    phase=f"{result.job_spec.phase}_{rule_name}_multi_assert",
                    extra_args=result.job_spec.extra_args,
                    context=result.job_spec.context,
                    msg=ProverJobSpec.build_job_msg(self.orchestration_timestamp, contract_name, new_config_path),
                )
                new_jobs.append(new_job_spec)

            return new_jobs

        except Exception as e:
            self.log(
                f"⚠️ Failed to create multi_assert_check jobs for {result.job_spec.contract_name}: {e}",
                "WARNING",
            )
            return []

    def _create_difficult_retry_jobs_if_needed(
        self, result: ProverResult, multi_assert_processed: bool
    ) -> List[ProverJobSpec]:
        """Create retry jobs for rules with timeout/unknown methods when job shows promise.

        LOOP PREVENTION: Will NOT create retry jobs if the original job was itself
        a difficult retry (phase contains "_difficult_retry").
        """
        if not self.retry_difficult:
            return []

        if "_difficult_retry" in result.job_spec.phase:
            self.log(
                f"Skipping difficult retry for {result.job_spec.contract_name} - "
                f"job is already a retry (phase: {result.job_spec.phase})",
                "DEBUG"
            )
            return []

        if multi_assert_processed:
            self.log(
                f"Skipping difficult retry for {result.job_spec.contract_name} - "
                f"already processed by multi_assert",
                "DEBUG"
            )
            return []

        if not result.success:
            return []

        config_path = result.job_spec.config_file.path
        if not config_path.exists():
            return []

        try:
            has_hope = False
            for rule_result in result.rule_results:
                if rule_result.rule_name == "envfreeFuncsStaticCheck":
                    continue
                if rule_result.status in ["VERIFIED", "VIOLATED"]:
                    has_hope = True
                    break

            if not has_hope:
                self.log(
                    f"No hope for {result.job_spec.contract_name} - "
                    f"no non-envfreeFuncsStaticCheck rules with real results",
                    "DEBUG"
                )
                return []

            rule_to_inconclusive_methods: Dict[str, Set[str]] = {}
            for rule_result in result.rule_results:
                if rule_result.status in ["TIMEOUT", "UNKNOWN"]:
                    rule_name = rule_result.rule_name
                    if rule_name not in rule_to_inconclusive_methods:
                        rule_to_inconclusive_methods[rule_name] = set()
                    if rule_result.method and not rule_result.method == "unknown_method":
                        rule_to_inconclusive_methods[rule_name].add(rule_result.method)

            if not rule_to_inconclusive_methods:
                return []

            temp_dir = internal_difficult_retry_dir(Path.cwd())
            temp_dir.mkdir(parents=True, exist_ok=True)

            new_jobs = []
            total_methods = sum(len(methods) for methods in rule_to_inconclusive_methods.values())

            with open(config_path) as f:
                config_data = json5.load(f)

            for rule_name, methods in rule_to_inconclusive_methods.items():
                new_config = config_data.copy()
                new_config["rule"] = [rule_name]
                if methods:
                    new_config["method"] = list(methods)

                new_config_path = temp_dir / f"{config_path.stem}_{rule_name}_difficult_retry.conf"
                with open(new_config_path, "w") as f:
                    json.dump(new_config, f, indent=4)

                contract_name = result.job_spec.contract_name
                new_job_spec = ProverJobSpec(
                    config_file=FileContent.from_file(new_config_path),
                    contract_name=contract_name,
                    phase=f"{result.job_spec.phase}_{rule_name}_difficult_retry",
                    extra_args=result.job_spec.extra_args,
                    context=result.job_spec.context,
                    msg=ProverJobSpec.build_job_msg(self.orchestration_timestamp, contract_name, new_config_path),
                )
                new_jobs.append(new_job_spec)

            if new_jobs:
                self.log(
                    f"🔍 Creating {len(new_jobs)} difficult retry job(s) for {contract_name} "
                    f"({total_methods} timeout/unknown methods across {len(rule_to_inconclusive_methods)} rules)",
                )

            return new_jobs

        except Exception as e:
            self.log(
                f"⚠️ Failed to create difficult retry jobs for {result.job_spec.contract_name}: {e}",
                "WARNING"
            )
            return []

    def _merge_job_stats(self, parent_stats: JobRuleStats, retry_stats: JobRuleStats) -> None:
        """Merge retry job stats into parent stats to avoid double-counting."""
        retried_count = (retry_stats.verified + retry_stats.violations +
                        retry_stats.timeout + retry_stats.unknown)

        remaining_to_reduce = retried_count
        reduction_from_unknown = min(parent_stats.unknown, remaining_to_reduce)
        parent_stats.unknown -= reduction_from_unknown
        remaining_to_reduce -= reduction_from_unknown

        reduction_from_timeout = min(parent_stats.timeout, remaining_to_reduce)
        parent_stats.timeout -= reduction_from_timeout

        parent_stats.verified += retry_stats.verified
        parent_stats.violations += retry_stats.violations
        parent_stats.timeout += retry_stats.timeout
        parent_stats.unknown += retry_stats.unknown

        parent_stats.violation_rules.extend(retry_stats.violation_rules)

    def get_report_name_from_config_type(self, config_or_type: str) -> str:
        """Extract a clean report name from config_or_type."""
        if config_or_type == "sanity":
            return "sanity"
        else:
            if "#" in config_or_type:
                config_path_str, contract_name = config_or_type.split("#", 1)
                config_path = Path(config_path_str)
                return f"{config_path.parent.name}_{config_path.stem}_{contract_name}"
            else:
                config_path = Path(config_or_type)
                return f"{config_path.parent.name}_{config_path.stem}"

    def _save_setup_completeness_report(self) -> None:
        """Save the setup completeness report to disk incrementally."""
        try:
            self.aggregated_setup_report.save()
        except Exception as e:
            self.log(f"Failed to save setup completeness report: {e}", "WARNING")
