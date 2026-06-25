#!/usr/bin/env python3
"""
Sanity Phase - Two-stage parallel optimization of loop bounds and hashing parameters.

High-level approach:
1. Stage 1: Find minimum sufficient loop_iter using very large hashing bounds (parallel testing)
2. Stage 2: Use optimal loop_iter to detect precise hashing bounds via bound detection
"""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sanity_analyzer.analysis import SANITY_DEFAULT_CONNECTION, SanityAnalysisResult, analyze as _sanity_analyze  # type: ignore[import-not-found]
from prover_output_utility import JobReport  # type: ignore[import-untyped]
from prover_output_utility.models import CheckResult, JobStatus as ProverJobStatus, NodeStatus  # type: ignore[import-untyped]

from certora_autosetup.utils.constants import DIR_CERTORA_INTERNAL, DIR_INTERNAL_CONFS, DIR_SANITY_ANALYSIS
from certora_autosetup.utils.enhanced_config_manager import ConfigManager, ProverJobSpec
from certora_autosetup.utils.logger import log_with_contract, logger
from certora_autosetup.utils.prover_runner import ProverRunner
from certora_autosetup.utils.runner_types import JobStatus, ProverResult

# The sanity rule emits one per-method "reaching the end of the method" satisfy assertion;
# its leaf assertion message contains SANITY_LEAF_MARKER. A method passes sanity iff that
# leaf VERIFIED.
SANITY_RULE_NAME = "sanity"
SANITY_LEAF_MARKER = "Satisfy_sanity_check_failed"


@dataclass
class _SanityArgs:
    """Arguments for a single sanity analysis run, satisfying SanityAnalysisArgs protocol."""

    unsat_core_txt_path: str
    thread_id: Optional[str]
    rule: Optional[str] = None
    method: Optional[str] = None
    quiet: bool = True
    recursion_limit: int = 30
    checkpoint_id: Optional[str] = None
    thinking_tokens: int = 2048
    tokens: int = 4096
    rag_db: str = field(default_factory=lambda: SANITY_DEFAULT_CONNECTION)
    model: str = "claude-sonnet-4-5-20250929"
    memory_tool: bool = True
    interleaved_thinking: bool = True
    prover_capture_output: bool = False
    prover_keep_folders: bool = False
    local_prover: bool = False
    prover_extra_args: Optional[str] = None
    debug_prompt_override: Optional[str] = None
    audit_db: str = ""
    summarization_threshold: Optional[int] = None
    requirements_oracle: List[str] = field(default_factory=list)
    set_reqs: Optional[str] = None
    skip_reqs: bool = False


@dataclass
class SanityAnalysis:
    """Result of one sanity verification job: which methods passed and whether the run is trustworthy.

    ``methods_all`` is the full roster of methods that ran a sanity leaf. A method is in
    ``methods_passed`` iff its leaf VERIFIED and in ``methods_failed`` iff its leaf VIOLATED (a
    genuine sanity failure: the method is unreachable / only reverts). ``methods_unverified`` is
    everything else, computed as ``methods_all`` minus ``methods_passed``. The run ``passed`` only
    when the prover job ran to completion (SUCCEEDED) and every method VERIFIED.
    """

    methods_passed: List[str] = field(default_factory=list)
    methods_failed: List[str] = field(default_factory=list)
    methods_all: List[str] = field(default_factory=list)
    job_status: ProverJobStatus = ProverJobStatus.UNKNOWN

    @property
    def methods_unverified(self) -> List[str]:
        """Every method not positively confirmed VERIFIED (all minus passed)."""
        passed = set(self.methods_passed)
        return [m for m in self.methods_all if m not in passed]

    @property
    def methods_inconclusive(self) -> List[str]:
        """Unverified methods that are not genuine sanity failures (timeout/unknown/error/running)."""
        failed = set(self.methods_failed)
        return [m for m in self.methods_unverified if m not in failed]

    @property
    def total_methods(self) -> int:
        return len(self.methods_all)

    @property
    def job_completed(self) -> bool:
        """Whether the prover job ran to completion (vs HALTED/FAILED/CANCELED/...)."""
        return self.job_status == ProverJobStatus.SUCCEEDED

    @property
    def passed(self) -> bool:
        """Sanity passes only on a completed job where every method leaf VERIFIED."""
        return self.job_completed and self.total_methods > 0 and not self.methods_unverified

    @property
    def summary(self) -> str:
        return f"{len(self.methods_passed)}/{self.total_methods} methods passed"

    def status_summary(self) -> str:
        """Human-readable one-line verdict for reports: ``PASS`` or ``FAIL ...`` with the reason."""
        if self.passed:
            return "PASS"
        prefix = "FAIL" if self.job_completed else f"FAIL (job {self.job_status.value})"
        if self.total_methods == 0:
            return f"{prefix} — no sanity results"
        parts = [self.summary]
        if self.methods_failed:
            parts.append(f"{len(self.methods_failed)} violated")
        if self.methods_inconclusive:
            parts.append(f"{len(self.methods_inconclusive)} inconclusive")
        return f"{prefix} — {', '.join(parts)}"

    @classmethod
    def from_checks(cls, checks: List[CheckResult], job_status: ProverJobStatus) -> "SanityAnalysis":
        methods_all: List[str] = []
        methods_passed: List[str] = []
        methods_failed: List[str] = []
        for c in checks:
            if c.rule_name != SANITY_RULE_NAME or not c.assert_message:
                continue
            if SANITY_LEAF_MARKER not in c.assert_message:
                continue
            method = c.method_name
            if method in methods_all:
                logger.warning(f"Found multiple sanity leaves for method {method}, expected exactly 1")
                continue
            methods_all.append(method)
            if c.status == NodeStatus.VERIFIED:
                methods_passed.append(method)
            elif c.status == NodeStatus.VIOLATED:
                methods_failed.append(method)
        return cls(
            methods_passed=methods_passed,
            methods_failed=methods_failed,
            methods_all=methods_all,
            job_status=job_status,
        )

    @classmethod
    def from_job_report(cls, job_report: JobReport) -> "SanityAnalysis":
        return cls.from_checks(job_report.checks, job_report.job_status)


@dataclass
class SanityFailureResult:
    """Analysis result for a method that failed sanity checking.

    Populated from the advanced coverage rerun for this method.
    Will be extended with more fields as analysis capabilities grow.
    """

    job_url: Optional[str]
    sanity_analysis: Optional[SanityAnalysisResult] = field(default=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_url": self.job_url,
            "sanity_analysis": self.sanity_analysis.model_dump() if self.sanity_analysis is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SanityFailureResult":
        raw = data.get("sanity_analysis")
        return cls(
            job_url=data.get("job_url"),
            sanity_analysis=SanityAnalysisResult.model_validate(raw) if raw is not None else None,
        )


class SanityResult(Enum):
    """Results from sanity checking runs."""

    SUCCESS = "success"
    SANITY_FAIL = "sanity_fail"
    TIMEOUT = "timeout"
    MEMOUT = "memout"
    UNKNOWN_FAIL = "unknown_fail"


@dataclass
class BoundConfiguration:
    """Configuration for a specific bound combination."""

    loop_iter: int
    hashing_bound: Optional[int]
    optimistic_hashing: bool # currently we always set the optimistic version
    optimistic_loop: bool # currently we always set the optimistic version

    def get_config_properties(self) -> Dict[str, Any]:
        """Get config properties (not command line args) for this bound configuration."""
        properties = {
            "loop_iter": self.loop_iter,
            "optimistic_loop": self.optimistic_loop,
            "optimistic_hashing": self.optimistic_hashing,
            # Bound detection only reads the `sanity` rule's results (vacuity / rule_not_vacuous is
            # excluded from the analysis), so the rule_sanity sub-checks are pure wasted runtime here.
            "rule_sanity": "none",
        }
        if self.hashing_bound is not None:
            properties["hashing_length_bound"] = self.hashing_bound
        return properties

    def __str__(self) -> str:
        """String representation for logging."""
        parts = [f"loop_iter={self.loop_iter}"]
        parts.append(f"optimistic_hashing={self.optimistic_hashing}")
        parts.append(f"optimistic_loop={self.optimistic_loop}")
        if self.hashing_bound:
            parts.append(f"hashing_bound={self.hashing_bound}")
        return f"BoundConfig({', '.join(parts)})"


@dataclass
class SanityTestResult:
    """Result from testing a specific bound configuration."""

    config: BoundConfiguration
    result: SanityResult
    duration: float
    report_path: Optional[str]
    sanity_analysis: Optional[SanityAnalysis] = None

    @property
    def success(self) -> bool:
        """Whether this configuration was successful."""
        return self.result == SanityResult.SUCCESS


class SanityEarlyTerminationCallback:
    """Early termination callback for sanity loop iteration optimization."""

    def __init__(self, all_job_specs: List["ProverJobSpec"]):
        self.all_job_specs = all_job_specs

    def should_terminate(
        self,
        completed_result: ProverResult,
        all_completed_results: List[ProverResult],
    ) -> bool:
        """
        Early termination logic for sanity testing:
        If a job with loop_iter=N succeeds (i.e. all methods passed sanity) and
        all jobs with loop_iter < N have finished,
        then N is the optimal solution and we can terminate remaining higher loop_iter jobs.
        """
        # Only consider successful results that pass sanity checks for early termination
        if (
            not completed_result.success
            or not completed_result.job_spec.context
        ):
            return False

        # Check if sanity tests passed
        if (
            not completed_result.transformed_result
            or not completed_result.transformed_result.success
        ):
            return False

        current_loop_iter = completed_result.job_spec.context.loop_iter

        # Get list of completed job specs for comparison
        completed_job_specs = [
            result.job_spec for result in all_completed_results
        ]

        # Check if all job specs with lower loop_iter have finished
        for job_spec in self.all_job_specs:
            if job_spec.context and job_spec.context.loop_iter < current_loop_iter:
                if job_spec not in completed_job_specs:
                    # Found a job with lower loop_iter that hasn't finished yet
                    return False

        # All jobs with lower loop_iter have finished
        log_with_contract(
            "Sanity",
            "info",
            completed_result.job_spec.contract_name,
            f"Found optimal solution with loop_iter={current_loop_iter}. "
            f"All lower loop_iter jobs have finished and sanity checks passed. Triggering early termination.",
        )
        return True


class SanityPhase:
    """
    Two-stage sanity phase with parallel bound optimization.

    Stage 1: Find minimum loop_iter with large hashing bounds (parallel)
    Stage 2: Use optimal loop_iter for precise hashing bound detection
    """

    def __init__(
        self,
        contract_name: str,
        config_file: Path,
        prover_runner: ProverRunner,
        config_manager: ConfigManager,
        orchestration_timestamp: str,
        extra_args: Optional[List[str]] = None,
        skip_hashing_bound_detection: int | None = None,
        min_loop_iter: int = 3,
        max_loop_iter: int = 5,
        skip_coverage_analysis: bool = False,
    ):
        """
        Initialize sanity phase.

        Args:
            contract_name: Name of the contract being processed
            config_file: Path to the configuration file to optimize
            prover_runner: Runner for prover execution (local or cloud)
            config_manager: Configuration manager for handling .conf files
            extra_args: Optional extra arguments to pass to prover
            orchestration_timestamp: Timestamp for job msg (format: YYYYMMDD_HHMMSS)
            skip_hashing_bound_detection: If set, skip hashing bound detection and use this value
            min_loop_iter: Lower bound (inclusive) of the loop_iter search range (default: 3)
            max_loop_iter: Upper bound (inclusive) of the loop_iter search range (default: 5)
            skip_coverage_analysis: If True, skip the per-method coverage rerun jobs and the
                AIComposer-backed sanity_analyzer vacuity analysis. Loop-iter and hashing-bound
                detection still run. Used by callers (e.g. PreAudit) that don't consume the
                advanced analysis and want to avoid the extra prover jobs + LLM calls.
        """
        self.contract_name = contract_name
        self.config_file = config_file
        self.prover_runner = prover_runner
        self.config_manager = config_manager
        self.extra_args = extra_args or []
        self.orchestration_timestamp = orchestration_timestamp
        self.skip_hashing_bound_detection = skip_hashing_bound_detection
        self.min_loop_iter = min_loop_iter
        self.max_loop_iter = max_loop_iter
        self.skip_coverage_analysis = skip_coverage_analysis

        # Use very large hashing bound for stage 1 (sufficient for most cases)
        self.LARGE_HASHING_BOUND = 1024

    @property
    def _sanity_analysis_dir(self) -> Path:
        """Base directory for sanity analysis artifacts (tar extractions and vacuity cache)."""
        return self.prover_runner.project_root / DIR_CERTORA_INTERNAL / DIR_SANITY_ANALYSIS

    @property
    def _internal_confs_dir(self) -> Path:
        """Directory for transient conf copies, kept out of the user-facing certora/confs/ tree."""
        return self.prover_runner.project_root / DIR_INTERNAL_CONFS

    def _build_job_msg(self, conf_file: Path) -> Optional[str]:
        """Build the msg string for a prover job.

        Format: "ProverLite <timestamp> <ContractName>: <conf_name>"
        Returns None if no orchestration_timestamp is set.
        """
        return ProverJobSpec.build_job_msg(self.orchestration_timestamp, self.contract_name, conf_file)

    def log(self, level: str, message: str):
        """Simple contract logging utility."""
        log_with_contract("Sanity", level, self.contract_name, message)

    async def execute(self) -> Dict[str, Dict[str, SanityFailureResult]]:
        """
        Execute the two-stage sanity optimization process.

        The resultant loop_iter and hashing_bound values are written to self.config_file.

        Returns:
            Structured analysis of methods that failed sanity: contract -> method -> SanityFailureResult.
            Empty dict if all methods pass. Built from the advanced coverage rerun results.
        """
        self.log("info", "Starting two-stage sanity optimization")
        start_time = time.time()

        # Stage 1: Find optimal loop_iter with large hashing bounds
        self.log("info", "Stage 1: Finding optimal loop_iter")
        optimal_loop_iter, chosen_analysis = await self._find_optimal_loop_iter()

        if optimal_loop_iter is None:
            duration = time.time() - start_time
            self.log("error", f"Stage 1 failed - no sufficient loop_iter found. Duration: {duration}")
            return {}

        self.log("info", f"Stage 1 completed: optimal loop_iter = {optimal_loop_iter}")

        # Stage 2: Detect precise hashing bounds using optimal loop_iter
        if self.skip_hashing_bound_detection is not None:
            optimal_hashing_bound = self.skip_hashing_bound_detection
            self.log(
                "info",
                f"Stage 2: Skipping hashing bound detection, using provided value {optimal_hashing_bound}",
            )
        else:
            self.log(
                "info",
                f"Stage 2: Detecting hashing bounds with loop_iter={optimal_loop_iter}",
            )

            optimal_hashing_bound = await self._detect_optimal_hashing_bounds()

            # If we failed to detect an optimal hashing bound with the prover auto
            # detection mode, we use the self.LARGE_HASHING_BOUND instead
            if optimal_hashing_bound is None:
                optimal_hashing_bound = self.LARGE_HASHING_BOUND

        self.config_manager.update_config_with_properties(
            self.config_file, {
                "hashing_length_bound": optimal_hashing_bound,
                "optimistic_hashing": True # always enabled for now
                }
        )

        duration = time.time() - start_time

        self.log(
            "info",
            f"Two-stage sanity optimization completed in {duration:.1f}s - "
            f"loop_iter={optimal_loop_iter}, hashing_bound={optimal_hashing_bound}",
        )

        # Run coverage reruns for methods that failed sanity, using the now-optimized config
        failing_methods = chosen_analysis.methods_failed if chosen_analysis else []

        analysis: Dict[str, Dict[str, SanityFailureResult]] = {}

        # The coverage reruns exist solely to produce UnsatCoreTAC files for the
        # AIComposer-backed sanity_analyzer. When coverage analysis is disabled we skip
        # both the rerun jobs and the analyzer call. loop_iter/hashing_bound were already
        # written to the config.
        if self.skip_coverage_analysis:
            if failing_methods:
                self.log(
                    "info",
                    f"Skipping sanity coverage analysis for {len(failing_methods)} failing method(s) "
                    "(skip_coverage_analysis=True)",
                )
            return analysis

        if failing_methods:
            analysis_tasks: List[asyncio.Task] = []

            def on_coverage_complete(result: ProverResult) -> None:
                analysis_tasks.append(asyncio.create_task(self.analyze_sanity_advanced(result)))

            await self._run_sanity_coverage_rerun(failing_methods, completion_callback=on_coverage_complete)
            per_result = await asyncio.gather(*analysis_tasks)
            for item in per_result:
                if item is not None:
                    contract_name, method_name, sanity_failure_result = item
                    analysis.setdefault(contract_name, {})[method_name] = sanity_failure_result
            return analysis
        return analysis

    def _extract_unsat_core_files(self, job_url: str, job_id: str) -> List[Path]:
        """Extract UnsatCoreTAC files (excluding rule_not_vacuous) and the .certora_sources tree.

        Extraction target: {certora_internal}/sanity_analysis/{job_id}/
          Reports/UnsatCoreTAC*.txt          <- for vacuity analysis
          inputs/.certora_sources/**          <- for VFS access during analysis

        Returns list of extracted UnsatCoreTAC file paths (empty on failure).
        """
        try:
            base_dir = self._sanity_analysis_dir / job_id
            reports_dir = base_dir / "Reports"
            sources_dir = base_dir / "inputs"
            prover_api = self.prover_runner.prover_api
            txt_paths = prover_api.extract_unsat_core_files(job_url, reports_dir)
            prover_api.extract_certora_sources(job_url, sources_dir)
            filtered = [p for p in txt_paths if "rule_not_vacuous" not in p.name]
            self.log("info", f"Extracted {len(filtered)} UnsatCoreTAC file(s) to {reports_dir}")
            return filtered
        except Exception as e:
            self.log("warning", f"Failed to extract tar for {job_url}: {e}")
            return []

    def _run_sanity_analyzer(self, txt_path: Path) -> Optional[SanityAnalysisResult]:
        """Run the sanity analyzer on a single UnsatCoreTAC file.

        Returns the full SanityAnalysisResult, or None on failure.
        """
        thread_id = f"sanity-analysis-{uuid.uuid4().hex}"
        args = _SanityArgs(unsat_core_txt_path=str(txt_path), thread_id=thread_id)
        try:
            result = _sanity_analyze(args)
            if result is not None:
                return result
            self.log("warning", f"sanity-analyzer returned None for {txt_path.name}")
            return None
        except Exception as e:
            self.log("warning", f"Failed to run sanity-analyzer for {txt_path.name}: {e}")
            return None

    async def analyze_sanity_advanced(
        self, result: ProverResult
    ) -> Optional[Tuple[str, str, SanityFailureResult]]:
        """Analyze a single sanity coverage rerun result.

        Downloads UnsatCoreTAC files from the job and runs the sanity analyzer.
        Returns (contract_name, method_name, SanityFailureResult), or None if this
        result is not a coverage rerun.
        """

        contract_name = result.job_spec.contract_name
        job_url = result.job_url
        method_names = list({r.method for r in result.rule_results if r.method is not None})
        if len(method_names) != 1:
            self.log("warning", f"Expected exactly one method name in ProverResult for run {job_url}, got {method_names}")
            self.log("warning", f"{result.rule_results}")
            return None
        method_name = method_names[0]

        sfr = SanityFailureResult(job_url=job_url)
        if job_url:
            prover_api = self.prover_runner.prover_api
            job_id = prover_api._extract_job_identifier(job_url)
            cache_file = self._sanity_analysis_dir / job_id / "sanity_result.json"

            # Cache hit: skip tar download and LLM call entirely
            if cache_file.exists():
                try:
                    sfr = SanityFailureResult.from_dict(json.loads(cache_file.read_text()))
                    self.log("info", f"Loaded cached sanity analysis for {contract_name}.{method_name}")
                    return contract_name, method_name, sfr
                except Exception as e:
                    self.log("warning", f"Failed to load sanity cache for {method_name}, re-running: {e}")

            # Cache miss: download, analyze, then save
            txt_paths = self._extract_unsat_core_files(job_url, job_id)
            if txt_paths:
                if len(txt_paths) > 1:
                    self.log(
                        "warning",
                        f"Multiple UnsatCoreTAC files for {method_name}: {[p.name for p in txt_paths]}",
                    )
                sfr.sanity_analysis = await asyncio.to_thread(self._run_sanity_analyzer, txt_paths[0])
                self.log("info", f"Sanity analysis complete for {contract_name}.{method_name}")

            # Save to cache only when analysis succeeded, so transient failures
            # (network issues, analyzer errors) are retried on the next run
            if sfr.sanity_analysis is not None:
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(sfr.to_dict()))
                except Exception as e:
                    self.log("warning", f"Failed to save sanity cache for {method_name}: {e}")

        return contract_name, method_name, sfr

    async def _run_sanity_coverage_rerun(self, failing_methods: List[str], completion_callback=None) -> List[ProverResult]:
        """Run a separate coverage rerun job for each method that failed sanity.

        Each job covers exactly one method, so any UnsatCoreTAC files in the resulting
        tar unambiguously belong to that method. All jobs are submitted in parallel.
        """
        MAX_RERUNS = 9
        if len(failing_methods) > MAX_RERUNS:
            skipped = failing_methods[MAX_RERUNS:]
            self.log("warning", f"Too many methods failed sanity ({len(failing_methods)}), running reruns only for the first {MAX_RERUNS}. Skipped: {skipped}")
            failing_methods = failing_methods[:MAX_RERUNS]
        
        self.log(
            "info",
            f"Running sanity coverage reruns for {len(failing_methods)} failing method(s): {failing_methods}",
        )
        job_specs = []
        for i, method in enumerate(failing_methods):
            coverage_config = self.config_manager.create_copy_with_config_properties(
                self.config_file,
                {"coverage_info": "advanced", "method": [method], "rule_sanity": "none"},
                f"_coverage_rerun_{i}",
                target_dir=self._internal_confs_dir,
            )
            job_specs.append(ProverJobSpec(
                contract_name=self.contract_name,
                phase=f"Sanity Coverage Rerun - {self.contract_name} - {method}",
                config_file=coverage_config,
                extra_args=self.extra_args,
                msg=self._build_job_msg(coverage_config.path),
            ))
        return await self.prover_runner.submit_and_wait_for_jobs(job_specs, completion_callback=completion_callback)

    async def _find_optimal_loop_iter(self) -> Tuple[Optional[int], Optional[SanityAnalysis]]:
        """
        Stage 1: Find the minimum sufficient loop_iter using large hashing bounds.

        Tests multiple loop_iter values in parallel with very large hashing bounds
        to ensure hashing is not the limiting factor.

        Args:
            state: Contract state

        Returns:
            Tuple of (minimum sufficient loop_iter or None, sanity analysis of the chosen run)
        """
        self.log("debug", "Testing loop_iter values in parallel")

        # Generate loop_iter configurations to test in parallel
        loop_iter_configs = []
        loop_iter_values = range(self.min_loop_iter, self.max_loop_iter + 1)

        for loop_iter in loop_iter_values:
            config = BoundConfiguration(
                loop_iter=loop_iter,
                hashing_bound=self.LARGE_HASHING_BOUND,  # Large enough to not be limiting
                optimistic_hashing=True,
                optimistic_loop=True,
            )
            loop_iter_configs.append(config)

        self.log(
            "info",
            f"Testing {len(loop_iter_configs)} loop_iter configurations in parallel "
            f"with hashing_bound={self.LARGE_HASHING_BOUND}",
        )

        # Test all configurations in parallel
        test_results = await self._test_configurations(
            loop_iter_configs, "loop_iter_optimization"
        )

        successful_results = [r for r in test_results if r.success]

        if successful_results:
            # Return minimum loop_iter that succeeded
            optimal_result = min(successful_results, key=lambda r: r.config.loop_iter)
            optimal_loop_iter = optimal_result.config.loop_iter

            self.log(
                "info",
                f"Found optimal loop_iter = {optimal_loop_iter} "
                f"(tested {len(successful_results)}/{len(test_results)} successful)",
            )

            # Apply the optimal configuration to the main config immediately
            self._apply_loop_iter_and_flags_to_main_config(optimal_loop_iter)
            return optimal_loop_iter, optimal_result.sanity_analysis

        # No successful results - analyze failures for potential fixes
        self.log("warning", "No successful loop_iter configurations found")

        best_effort_result = self._select_best_effort_configuration(test_results)

        if best_effort_result:
            # Apply best-effort configuration to main config
            self._apply_loop_iter_and_flags_to_main_config(best_effort_result.config.loop_iter)
            return best_effort_result.config.loop_iter, best_effort_result.sanity_analysis

        return None, None

    def _apply_loop_iter_and_flags_to_main_config(self, loop_iter: int) -> None:
        """
        Apply the optimal loop_iter to the main config immediately.

        Args:
            loop_iter: Optimal loop_iter to apply
        """
        try:
            # Create config properties to apply
            config_properties = {
                "loop_iter": loop_iter,
                "optimistic_loop": True,  # TODO: either never touch it or pass it to the function
            }

            # Update the main config file
            self.config_manager.update_config_with_properties(
                self.config_file, config_properties
            )

            self.log("info", f"Applied loop_iter={loop_iter} to main config")

        except Exception as e:
            self.log("error", f"Failed to apply configuration to main config: {e}")

    async def _detect_optimal_hashing_bounds(self) -> Optional[int]:
        """
        Stage 2: Detect precise hashing bounds using the optimal loop_iter.

        Now that we have sufficient loop unrolling, we can accurately detect
        the minimum required hashing bounds.

        Returns:
            Maximum of the detected hashing bounds, or None if none found
        """
        self.log("info", "Detecting hashing bounds")

        try:
            # hashingBoundDetection is a prover arg
            detection_args = {"hashingBoundDetection": "true"}

            bound_detection_config = self.config_manager.create_copy_with_prover_args(
                self.config_file,
                detection_args,
                "_bound_detection",
                target_dir=self._internal_confs_dir,
            )

            # Submit bound detection job
            job_spec: ProverJobSpec = ProverJobSpec(
                contract_name=self.contract_name,
                phase="hashing_bound_detection",
                config_file=bound_detection_config,
                extra_args=self.extra_args,
                msg=self._build_job_msg(bound_detection_config.path),
            )

            result = await self.prover_runner.check_with_prover(job_spec)

            if result.success and result.job_handle and result.job_handle.job_id:
                # Analyze hashing bounds from the alerts field in ProverResult
                max_bound = self._analyze_hashing_bounds_from_result(result)

                if max_bound:
                    return max_bound
                else:
                    self.log(
                        "info",
                        "No specific hashing bounds detected",
                    )
                    return None
            else:
                self.log(
                    "warning", f"Hashing bound detection failed: {result.error_message}"
                )
                return None

        except Exception as e:
            self.log("error", f"Hashing bound detection failed: {e}")
            return None

    def _analyze_hashing_bounds_from_result(
        self, result: ProverResult
    ) -> Optional[int]:
        """
        Comprehensive hashing bound detection from ProverResult.

        Analyzes all methods to determine:
        1. Which methods use hashing (have hashing_bound_assert_* subrules)
        2. Which methods have successful bound detection (alert messages)
        3. Which methods have failed bound detection (subrules verified but no alert)

        Args:
            result: ProverResult containing rule results and alert report

        Returns:
            Maximum detected bound, or None if no bounds found
        """
        try:
            job_id = result.job_handle.job_id
            self.log("debug", f"Analyzing hashing bounds for job: {job_id}")

            # Get all checks from the result
            all_checks = result.rule_results

            # Get alerts from the result
            alerts = result.alerts

            # Find all sanity rule checks
            # TODO: we assume that there there exist a rule called sanity (currently
            # we create it in the base config's spec). Might be better to check
            # within the sanity phase that the rule actually exist, and also to
            # run only this rule.
            sanity_checks = [c for c in all_checks if c.rule_name == "sanity"]

            # Group by method
            methods_with_hashing: Dict[str, List] = {}

            for check in sanity_checks:
                method_name = check.method
                # TODO: "unknown_method" might be claude hallucinating?
                if not method_name or method_name == "unknown_method":
                    continue

                # Check if this is a hashing bound assertion subrule
                if (
                    check.assert_message
                    and "hashing_bound_assert_" in check.assert_message
                ):
                    if method_name not in methods_with_hashing:
                        methods_with_hashing[method_name] = []
                    methods_with_hashing[method_name].append(check)

            # Parse alert messages to find successful bound detections
            successful_bounds = {}
            pattern = r"The suggested minimal hashing bound for (.+?) is (\d+)"

            for alert in alerts:
                matches = re.findall(pattern, alert.message)
                for method_match, bound_match in matches:
                    try:
                        bound = int(bound_match)
                        bound = min(bound, self.LARGE_HASHING_BOUND)
                        successful_bounds[method_match] = bound
                    except ValueError:
                        continue

            # Analyze results
            self.log(
                "info",
                f"Hashing bound detection analysis:\n"
                f"  Methods with hashing: {len(methods_with_hashing)}\n"
                f"  Successful bound detections: {len(successful_bounds)}",
            )

            # Collect methods that failed bound detection
            failed_methods = []
            for method_name, hashing_checks in methods_with_hashing.items():
                if method_name not in successful_bounds:
                    failed_methods.append(method_name)

            # Issue warnings for failed methods
            if failed_methods:
                self.log(
                    "warning",
                    f"Failed to detect hashing bounds for {len(failed_methods)} methods: {failed_methods}. "
                    "Consider manually setting hashing_length_bound or investigating the methods.",
                )

            # Return the maximum bound found, or None if no bounds detected
            if successful_bounds:
                max_bound = max(successful_bounds.values())
                detected_methods = list(successful_bounds.keys())
                self.log(
                    "info",
                    f"Using maximum detected bound: {max_bound} (from methods: {detected_methods})",
                )
                return max_bound
            else:
                self.log(
                    "info",
                    "No hashing bounds detected - will use predefined bound",
                )
                return None

        except Exception as e:
            self.log("error", f"Failed to analyze hashing bounds from result: {e}")
            return None

    async def _test_configurations(
        self, configs: List[BoundConfiguration], stage: str
    ) -> List[SanityTestResult]:
        """
        Test all bound configurations in parallel using prover_runner.submit_and_wait_for_jobs.

        Args:
            configs: List of bound configurations to test
            stage: Stage identifier for logging/naming
            additional_flags: Optional additional config properties to apply

        Returns:
            List of test results
        """

        self.log(
            "debug",
            f"Testing {len(configs)} configurations in parallel for {self.contract_name} ({stage}).",
        )

        # Create job specifications for all configurations
        job_specs = []
        for i, config in enumerate(configs):
            config_id = f"{stage}_{i}"

            # Create configuration with specific bounds and optional additional flags
            base_config = self.config_file
            config_properties = config.get_config_properties()

            test_config = self.config_manager.create_copy_with_config_properties(
                base_config, config_properties, f"_sanity_{config_id}",
                target_dir=self._internal_confs_dir,
            )

            # Create job specification with context
            job_spec = ProverJobSpec(
                contract_name=self.contract_name,
                phase=f"sanity_{config_id}",
                config_file=test_config,
                extra_args=self.extra_args,
                context=config,  # Store BoundConfiguration as context
                msg=self._build_job_msg(test_config.path),
            )
            job_specs.append(job_spec)

        # Create early termination callback and transformer
        early_termination_callback = SanityEarlyTerminationCallback(job_specs)
        sanity_transformer = self.create_sanity_transformer()

        # Submit all jobs and wait for completion using prover runner with early termination and transformer
        results = (
            await self.prover_runner.submit_and_wait_for_jobs_with_transformer(
                job_specs,
                early_termination_callback=early_termination_callback,
                result_transformer=sanity_transformer,
            )
        )

        # Extract sanity test results from results
        sanity_results: List[SanityTestResult] = []
        for result in results:
            try:
                # Skip cancelled results
                if (
                    result.job_handle
                    and result.job_handle.status
                    == JobStatus.CANCELLED
                ):
                    continue

                # Ensure transformed_result exists and is of correct type
                if result.transformed_result is not None:
                    sanity_results.append(result.transformed_result)
            except Exception as e:
                self.log(
                    "error",
                    f"Failed to process result for {result.job_spec.contract_name}: {e}",
                )
                continue

        success_count = len([r for r in sanity_results if r.success])
        self.log(
            "info",
            f"Parallel testing completed ({stage}): {success_count}/{len(sanity_results)} configurations succeeded",
        )
        return sanity_results

    def _select_best_effort_configuration(
        self, test_results: List[SanityTestResult]
    ) -> Optional[SanityTestResult]:
        """
        Select the best configuration from failed results based on:
        1. Primary: Configuration that solved the most sanity checks
        2. Secondary: Configuration with the lowest loop_iter

        Args:
            test_results: List of test results (all failed)

        Returns:
            Best-effort SanityTestResult, or None if no useful results
        """
        if not test_results:
            return None

        # Score each configuration based on sanity analysis
        scored_results = []
        for result in test_results:
            if result.sanity_analysis:
                # Score = methods passed (higher is better)
                score = len(result.sanity_analysis.methods_passed)
                scored_results.append((score, result))

        if not scored_results:
            # No sanity analysis available, just pick lowest loop_iter
            self.log(
                "warning",
                "No sanity analysis available for best-effort selection, choosing lowest loop_iter",
            )
            return min(test_results, key=lambda r: r.config.loop_iter)

        # Sort by score (descending), then by loop_iter (ascending)
        scored_results.sort(key=lambda x: (-x[0], x[1].config.loop_iter))

        best_score, best_result = scored_results[0]

        self.log(
            "info",
            f"Best-effort configuration selected: loop_iter={best_result.config.loop_iter}, "
            f"sanity_score={best_score}/{best_result.sanity_analysis.total_methods if best_result.sanity_analysis else 0}",
        )

        return best_result

    def create_sanity_transformer(self):
        """Create a transformer function for converting ProverResult to SanityTestResult."""

        def transform_prover_result_to_sanity_result(
            prover_result: ProverResult
        ) -> SanityTestResult:
            """Transform ProverResult to SanityTestResult using SanityPhase logic."""
            if prover_result.job_spec.context is None:
                raise ValueError("Job spec context is None")
            return self._convert_prover_result_to_sanity_result(
                prover_result, prover_result.job_spec.context, prover_result.job_spec.phase  # type: ignore[arg-type]
            )

        return transform_prover_result_to_sanity_result

    def _convert_prover_result_to_sanity_result(
        self, prover_result: ProverResult, config: BoundConfiguration, phase: str
    ) -> SanityTestResult:
        """
        Convert a ProverResult to a SanityTestResult.

        Args:
            prover_result: ProverResult from the prover execution
            config: BoundConfiguration that was tested
            phase: Phase identifier

        Returns:
            SanityTestResult with analysis
        """
        start_time = time.time()

        try:
            job_id = prover_result.job_handle.job_id if prover_result.job_handle else None
            if job_id:
                checks = self.prover_runner.prover_api.get_all_checks(job_id)
                job_status = self.prover_runner.prover_api.get_job_info(job_id).status
                sanity_analysis = SanityAnalysis.from_checks(checks, job_status)
            else:
                sanity_analysis = SanityAnalysis()
            self.log("info", f"Sanity analysis: {sanity_analysis.summary}")

            # Map the analysis onto a SanityResult: SUCCESS only when the run passed; otherwise
            # classify a completed run as a sanity failure and an incomplete one by its error.
            if sanity_analysis.passed:
                sanity_result = SanityResult.SUCCESS
            elif "timeout" in (prover_result.error_message or "").lower():
                sanity_result = SanityResult.TIMEOUT
            elif "memory" in (prover_result.error_message or "").lower():
                sanity_result = SanityResult.MEMOUT
            elif sanity_analysis.job_completed:
                sanity_result = SanityResult.SANITY_FAIL
            else:
                sanity_result = SanityResult.UNKNOWN_FAIL

            duration = prover_result.duration or (time.time() - start_time)

            return SanityTestResult(
                config=config,
                result=sanity_result,
                duration=duration,
                report_path=prover_result.report_path,
                sanity_analysis=sanity_analysis,
            )

        except Exception as e:
            self.log("error", f"Failed to convert prover result: {e}")
            duration = time.time() - start_time

            return SanityTestResult(
                config=config,
                result=SanityResult.UNKNOWN_FAIL,
                duration=duration,
                report_path=None,
            )
