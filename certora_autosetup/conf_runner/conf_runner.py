"""
ConfRunner — execute Certora verification configurations and generate reports.

Given a set of .conf files, submits them to the Certora Prover (cloud or local),
handles completion callbacks, and produces results JSON reports.
"""

import asyncio
import json
import shutil
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from certora_autosetup.conf_runner.callbacks import JobCallbacks
from certora_autosetup.conf_runner.types import ConfRunnerConfig
from certora_autosetup.reporting.reporter import Reporter
from certora_autosetup.reporting.json_reporter import JsonReporter
from certora_autosetup.setup.setup_completeness_checker import SetupCompletenessChecker, SetupCompletenessReport
from certora_autosetup.utils import logger
from certora_autosetup.utils.enhanced_config_manager import ConfigManager, FileContent, ProverJobSpec
from certora_autosetup.utils.prover_runner import ProverRunner
from certora_autosetup.utils.runner_types import ProverResult
from certora_autosetup.utils.types import ContractHandle

COMPONENT = "ConfRunner"


class ConfRunner:
    """Execute Certora verification configurations and generate reports.

    Takes ready-to-run .conf files, submits them, handles callbacks, and produces reports.
    """

    def __init__(
        self,
        config: ConfRunnerConfig,
        prover_runner: ProverRunner,
        config_manager: ConfigManager,
        reporter: Reporter,
        json_reporter: JsonReporter,
        reports_dir: Path,
        setup_checker: SetupCompletenessChecker,
        aggregated_setup_report: SetupCompletenessReport,
        orchestration_timestamp: str,
        project_root: Path,
        certora_dir: Path,
    ):
        self.config = config
        self.prover_runner = prover_runner
        self.config_manager = config_manager
        self.reporter = reporter
        self.json_reporter = json_reporter
        self.reports_dir = reports_dir
        self.orchestration_timestamp = orchestration_timestamp
        self.project_root = project_root
        self.certora_dir = certora_dir

        self.callbacks = JobCallbacks(
            reporter=reporter,
            json_reporter=json_reporter,
            setup_checker=setup_checker,
            aggregated_setup_report=aggregated_setup_report,
            prover_api=prover_runner.prover_api,
            retry_difficult=config.retry_difficult,
            skip_setup_check=config.skip_setup_check,
            orchestration_timestamp=orchestration_timestamp,
            verbose=config.verbose,
        )

        self.results_json_path: Optional[Path] = None

    def log(self, message: str, level: str = "INFO"):
        logger.log(message, level, COMPONENT)

    def run_confs(
        self,
        config_files: list[Path],
        test_run_specs: list[ProverJobSpec] | None = None,
        bytes_mappings: list[tuple[ContractHandle, list[str]]] | None = None,
        sanity_analysis: dict | None = None,
        execution_info: dict | None = None,
        llm_usage: list | None = None,
        setup_report: SetupCompletenessReport | None = None,
    ) -> tuple[list[ProverResult], Path | None]:
        """Execute conf files, generate reports.

        Returns:
            Tuple of (results list, path to results JSON or None).
        """
        # Run all configs
        verification_results = self._run_all_configs(config_files, test_run_specs or [])

        # Generate reports
        if verification_results:
            self._generate_final_reports(
                verification_results,
                bytes_mappings=bytes_mappings or [],
                sanity_analysis=sanity_analysis or {},
                execution_info=execution_info or {},
                llm_usage=llm_usage,
                setup_report=setup_report,
            )

        return verification_results, self.results_json_path

    def _run_all_configs(
        self,
        config_files: list[Path],
        test_run_specs: list[ProverJobSpec] | None = None,
    ) -> list[ProverResult]:
        """Run all configuration files and track results."""
        self.log("=== VERIFICATION PHASE ===")

        if not config_files and not test_run_specs:
            self.log("No configuration files found to run", "WARNING")
            return []

        # Apply max_configs limit if specified
        limited_config_files = config_files
        if self.config.max_configs and len(config_files) > self.config.max_configs:
            limited_config_files = config_files[: self.config.max_configs]
            self.log(
                f"🔢 Limiting to {self.config.max_configs} config files for testing "
                f"(out of {len(config_files)} total)"
            )

        job_specs: list[ProverJobSpec] = []

        for config_file in limited_config_files:
            tool_name = config_file.parent.name
            config_name = config_file.name
            contract_name = config_file.stem.split("-")[-1]
            try:
                job_spec: ProverJobSpec[Any] = ProverJobSpec(
                    config_file=FileContent.from_file(config_file),
                    contract_name=contract_name,
                    phase=f"{tool_name}-{config_name}",
                    extra_args=self.config.extra_args,
                    msg=ProverJobSpec.build_job_msg(self.orchestration_timestamp, contract_name, config_file),
                )
                job_specs.append(job_spec)
                self.log(
                    f"📝 Created job spec for {contract_name} with {tool_name}-{config_name}",
                    "DEBUG",
                )
            except Exception as e:
                self.log(
                    f"✗ Failed to create job spec for {contract_name}: {str(e)}",
                    "ERROR",
                )

        # Include test run specs
        if test_run_specs:
            self.log(f"📝 Adding {len(test_run_specs)} test run job(s) to verification phase")
            job_specs.extend(test_run_specs)

        # Execute all jobs
        self.log(f"🔍 Created {len(job_specs)} job specifications")
        results: list[ProverResult] = []
        if job_specs:
            self.log(f"🚀 About to submit and wait for {len(job_specs)} jobs...")
            results = asyncio.run(
                self.prover_runner.submit_and_wait_for_jobs(
                    job_specs,
                    completion_callback=self.callbacks.on_job_complete,
                    pre_execute_callback=self._pre_execute_typechecker_fix,
                    use_queue=True,
                )
            )
            self.log(f"✅ Received {len(results)} results from prover runner")
        else:
            self.log(
                "⚠️ No job specifications created - skipping verification phase",
                "WARNING",
            )

        return results

    def _pre_execute_typechecker_fix(self, job_spec) -> None:
        """Pre-execute callback to fix config with typechecker before job submission."""
        config_path = job_spec.config_file.path
        contract_name = job_spec.contract_name

        if not self._fix_config_with_typechecker(
            config_path, f"{contract_name} {job_spec.phase}"
        ):
            raise Exception(f"Typechecker fix failed for {config_path.name}")

        self.log(
            f"✓ Pre-execute typechecker fix completed for {contract_name}: {config_path.name}",
            "DEBUG",
        )

    def _fix_config_with_typechecker(self, config_file: Path, description: str = "") -> bool:
        """Fix a config file using the TypecheckerLoop.

        Returns True if the config was successfully fixed or didn't need fixing.
        """
        try:
            from certora_autosetup.typechecker_loop import TypecheckerLoop  # type: ignore[import-not-found]

            cmd = [self.config.certora_run_command, str(config_file)]
            cmd.extend(self.config.extra_args)

            typechecker = TypecheckerLoop(
                certora_dir=self.certora_dir,
                verbose=False,
                keep_intermediate_files=self.config.keep_intermediate_typechecker_files,
            )

            desc_text = f" for {description}" if description else ""
            self.log(f"🔧 Running typechecker fixes{desc_text}: {config_file.name}")
            success, final_cmd = typechecker.run_typechecker_loop(cmd, max_rounds=10)

            if success:
                if len(final_cmd) > 1 and final_cmd[1] != str(config_file):
                    fixed_config_file = Path(final_cmd[1])
                    shutil.copy2(fixed_config_file, config_file)
                    self.log(f"✓ Config fixed{desc_text}: {config_file.name}")
                else:
                    self.log(
                        f"✓ Config OK{desc_text}: {config_file.name} (no fixes needed)"
                    )
                return True
            else:
                self.log(
                    f"✗ Failed to fix config{desc_text}: {config_file.name}", "ERROR"
                )
                return False

        except Exception as e:
            desc_text = f" for {description}" if description else ""
            self.log(
                f"✗ Error fixing config{desc_text} {config_file.name}: {e}", "ERROR"
            )
            return False

    def _generate_final_reports(
        self,
        verification_results: list[ProverResult],
        bytes_mappings: list[tuple[ContractHandle, list[str]]],
        sanity_analysis: dict,
        execution_info: dict,
        llm_usage: list | None = None,
        setup_report: SetupCompletenessReport | None = None,
    ) -> None:
        """Generate comprehensive reports from verification results."""
        try:
            with self.callbacks.report_stats_lock:
                all_report_stats = self.callbacks.report_stats.copy()

            self.reporter.generate_comprehensive_report(
                verification_results, all_report_stats, setup_report, bytes_mappings
            )
            self.reporter.generate_summarized_reports(verification_results, all_report_stats)
            self.reporter.generate_sanity_summary(verification_results, sanity_analysis, bytes_mappings)

            if setup_report and setup_report.has_issues and setup_report.md_path:
                self.log(f"⚠️ Setup completeness report: {setup_report.md_path}")

            self.log("=== GENERATING JSON RESULT ===")
            json_data = self.json_reporter.generate_results_json(
                verification_results,
                all_report_stats,
                self.callbacks.get_report_name_from_config_type,
                execution_info,
                llm_usage=llm_usage or None,
            )
            json_file = self.json_reporter.save_results_json(json_data)
            self.results_json_path = json_file
            self.log(f"📄 JSON test results saved to: {json_file}")
        except Exception as e:
            self.log(f"❌ Failed to generate final reports: {e}", "ERROR")
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")

    def generate_partial_reports(self) -> None:
        """Generate reports from whatever jobs have completed so far (for Ctrl+C handling)."""
        with self.callbacks.job_results_lock:
            results = self.callbacks.job_results.copy()

        if results:
            self._generate_final_reports(
                results,
                bytes_mappings=[],
                sanity_analysis={},
                execution_info={},
            )

    def print_summary(
        self,
        warmup_success: bool,
        verification_results: list[ProverResult],
        bytes_mappings: list[tuple[ContractHandle, list[str]]] | None = None,
    ):
        """Print execution summary."""
        self.log("=== EXECUTION SUMMARY ===")

        warmup_status = "✓ PASSED" if warmup_success else "✗ FAILED"
        self.log(f"Cache Warmup: {warmup_status}")

        if verification_results:
            passed = sum(1 for result in verification_results if result.success)
            total = len(verification_results)
            self.log(f"Verification Results: {passed}/{total} configurations passed")

            try:
                cache_stats = self.prover_runner.get_cache_status(self.project_root)
                if cache_stats.get("cache_exists", False):
                    total_entries = cache_stats.get("total_entries", 0)
                    self.log(
                        f"Cache Performance: {total_entries} cached entries available"
                    )
            except Exception as e:
                self.log(f"Could not retrieve cache statistics: {e}", "DEBUG")

            for result in verification_results:
                status = "✓ PASSED" if result.success else "✗ FAILED"
                config_name = Path(result.job_handle.config_file).name
                self.log(f"  {config_name}: {status}")
        else:
            self.log("No verification configurations were run")

        if bytes_mappings:
            for line in self.reporter.bytes_mapping_warning(bytes_mappings):
                self.log(line, "WARNING")

    def cleanup_running_jobs(self):
        """Cancel all running jobs to save cloud costs."""
        try:
            try:
                asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        self.prover_runner.cleanup_all_running_jobs()
                    )
                    cancelled_count = future.result(timeout=30)
            except RuntimeError:
                cancelled_count = asyncio.run(self.prover_runner.cleanup_all_running_jobs())

            if cancelled_count > 0:
                self.log(f"✅ Cancelled {cancelled_count} running jobs")
            else:
                self.log("No running jobs to cancel", "DEBUG")
        except Exception as e:
            self.log(f"❌ Error during job cleanup: {e}", "ERROR")
