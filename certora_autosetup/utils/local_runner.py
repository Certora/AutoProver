#!/usr/bin/env python3
"""
LocalProverRunner - Local Certora prover execution using direct certoraRun commands.

Executes prover jobs locally using `certoraRun <conf_file.conf>` with
caching and resume support.
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .enhanced_config_manager import ConfigManager, ProverJobSpec
from .prover_runner import EarlyTerminationCallback, ProverRunner, ResultTransformer
from .runner_types import JobHandle, JobStatus, ProverResult, RunnerType, SubmissionResult
from .logger import log_with_contract


class LocalProverRunner(ProverRunner):
    """
    Local prover runner using direct certoraRun execution.

    Executes prover jobs locally with:
    - Direct `certoraRun.py <conf_file.conf>` execution
    - Content-based caching for resume support
    - Process management and timeout handling
    - Concurrency limiting via semaphore (each prover run uses many CPU cores)
    """

    def __init__(
        self,
        project_root: Path,
        config_manager: ConfigManager,
        certora_run_path: str = "certoraRun.py",
        disable_cache: bool = False,
        max_concurrent_jobs: int = 1,
    ):
        """
        Initialize local prover runner.

        Args:
            project_root: Root directory of the project
            config_manager: Configuration manager for dependency tracking
            certora_run_path: Path to certoraRun executable (default: "certoraRun.py")
            disable_cache: If True, disable caching (useful for tests)
            max_concurrent_jobs: Maximum number of prover jobs to run concurrently (default: 1).
                                 Each certoraRun spawns many z3 processes, so limiting concurrency
                                 prevents CPU contention.
        """
        super().__init__(project_root, config_manager, use_local_api=True)
        self.component = "LocalRunner"
        self.certora_run_path = certora_run_path
        self.disable_cache = disable_cache
        self.max_concurrent_jobs = max_concurrent_jobs
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def get_runner_type(self) -> RunnerType:
        """Get the type of this runner."""
        return RunnerType.LOCAL

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Get or create the concurrency-limiting semaphore."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent_jobs)
            self.log(f"Initialized semaphore with max_concurrent_jobs={self.max_concurrent_jobs}")
        return self._semaphore

    async def _check_with_prover_impl(self, job_spec: ProverJobSpec) -> ProverResult:
        """
        Execute prover locally with automatic resume support and caching.

        Args:
            job_spec: Complete job specification

        Returns:
            ProverResult with job status and results
        """
        # Use existing cache key functionality from config_manager
        cache_key = self._get_cache_key(job_spec)

        log_with_contract(
            self.component, "info", job_spec.contract_name,
            f"Local prover request for {job_spec.phase} (cache_key: {cache_key[:16]}...)"
        )

        # Step 1: Check cache for completed results (unless disabled)
        # Cache check doesn't need semaphore - it's just reading from disk
        if not self.disable_cache:
            cached_result = await self._check_cache(cache_key, job_spec)
            if cached_result:
                self.log(
                    f"Using cached result for {job_spec.contract_name}:{job_spec.phase}"
                )
                return cached_result

        # Step 2: Execute local prover run (with semaphore to limit concurrency)
        # Each certoraRun spawns many z3 processes, so we limit how many run at once
        semaphore = self._get_semaphore()
        async with semaphore:
            log_with_contract(
                self.component, "debug", job_spec.contract_name,
                f"Acquired semaphore, executing local prover for {job_spec.phase}"
            )
            result = await self._execute_local_prover(job_spec, cache_key)

            # Fresh run (cache hits returned above) — record its wall-clock runtime.
            # Local runs are serialized (one prover at a time, no queueing), so
            # wall-time is the run time.
            self._record_prover_runtime_seconds(result.duration)

            # Step 3: Cache successful results (unless disabled)
            if result.success and not self.disable_cache:
                await self._cache_result(cache_key, result)

        return result

    async def _execute_local_prover(self, job_spec: ProverJobSpec, cache_key: str) -> ProverResult:
        """Execute local certoraRun command."""
        # Create job handle for result tracking (job_id will be set to emv-* folder path after execution)
        job_handle = JobHandle(
            job_id="",  # Will be updated with emv-* folder path after execution
            config_file=str(job_spec.config_file.path),
            config_content_hash=cache_key,
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=RunnerType.LOCAL,
            status=JobStatus.RUNNING,
        )

        start_time = time.time()

        try:
            # Build certoraRun command
            cmd = [self.certora_run_path, str(job_spec.config_file.path)]

            # Append extra_args if present
            if job_spec.extra_args:
                cmd.extend(job_spec.extra_args)

            # Append msg if present
            if job_spec.msg:
                cmd.extend(["--msg", job_spec.msg])

            self.log(f"Executing: {' '.join(cmd)}")

            # Execute command with timeout
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root,
            )

            timeout: int = 3600
            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                process.kill()
                await process.wait()

                duration = time.time() - start_time
                self.log(f"Local prover timed out after {duration:.1f}s", "ERROR")

                job_handle.status = JobStatus.FAILED
                return ProverResult(
                    job_handle=job_handle,
                    success=False,
                    report_path=None,
                    output_data={},
                    job_spec=job_spec,
                    error_message=f"Prover execution timed out after {timeout} seconds",
                    duration=duration,
                    transformed_result=None,
                )

            duration = time.time() - start_time
            return_code = process.returncode or -1

            # Parse output and determine success
            stdout_str = stdout.decode("utf-8") if stdout else ""
            stderr_str = stderr.decode("utf-8") if stderr else ""

            # Parse output to extract emv-* folder path and use ProverOutputUtility
            emv_folder_path, output_data = self._parse_successful_output(stdout_str, stderr_str)

            # Update job_handle with emv-* folder path as job_id for ProverOutputUtility
            if emv_folder_path:
                job_handle.job_id = emv_folder_path
                # Extract unresolved calls using ProverOutputUtility for consistency with cloud runner
                output_data["unresolved_calls"] = self.extract_unresolved_calls(emv_folder_path)

            report_path = output_data.get("report_path")

            # Don't rely solely on return code as warnings can cause non-zero exit codes
            prover_success = self._check_prover_success(
                return_code, stdout_str, stderr_str, report_path
            )

            if prover_success:
                job_handle.status = JobStatus.COMPLETED
                emv_path_msg = f" (emv folder: {emv_folder_path})" if emv_folder_path else ""
                log_with_contract(
                    self.component, "info", job_spec.contract_name,
                    f"Local prover completed successfully in {duration:.1f}s{emv_path_msg}"
                )

                return ProverResult(
                    job_handle=job_handle,
                    success=True,
                    report_path=report_path,
                    output_data=output_data,
                    job_spec=job_spec,
                    duration=duration,
                    transformed_result=None,
                )
            else:
                # Failure - extract error message
                error_msg = self._extract_error_message(stdout_str, stderr_str, return_code)

                job_handle.status = JobStatus.FAILED
                self.log(f"Local prover failed after {duration:.1f}s: {error_msg}", "ERROR")

                return ProverResult(
                    job_handle=job_handle,
                    success=False,
                    report_path=report_path,  # Include report path even on failure (might have partial results)
                    output_data={
                        "stdout": stdout_str,
                        "stderr": stderr_str,
                        "return_code": return_code,
                    },
                    job_spec=job_spec,
                    error_message=error_msg,
                    duration=duration,
                    transformed_result=None,
                )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Local prover execution failed: {str(e)}"

            job_handle.status = JobStatus.FAILED
            self.log(error_msg)

            return ProverResult(
                job_handle=job_handle,
                success=False,
                report_path=None,
                output_data={},
                job_spec=job_spec,
                error_message=error_msg,
                duration=duration,
                transformed_result=None,
            )

    def _check_prover_success(
        self, return_code: int, stdout: str, stderr: str, report_path: Optional[str]
    ) -> bool:
        """
        Check if prover execution was successful using Brain's classification logic.

        Based on Brain's classification: timeouts and unknown results are NOT failures.
        Only genuine fatal errors are considered failures.

        Args:
            return_code: Process return code
            stdout: Standard output from prover
            stderr: Standard error from prover
            report_path: Path to JSON report file if found

        Returns:
            True if prover execution was successful (including timeouts/unknown results)
        """
        output = stdout + stderr

        # First check for JSON report existence - most reliable indicator
        if report_path:
            report_file = Path(report_path)
            if not report_file.is_absolute():
                report_file = self.project_root / report_path

            if report_file.exists():
                self.log(
                    f"Found JSON report at {report_file}, considering execution successful"
                )
                return True

        # Return code 0 is always success
        if return_code == 0:
            return True

        # Look for "Reports in" pattern which indicates successful report generation
        if "Reports in" in output:
            return True

        # Memory partitioning warnings are not failures
        if "Memory partitioning failed" in output and "Warning:" in output:
            return True

        # Timeouts are NOT failures (Brain's approach)
        timeout_patterns = [
            "Reached global timeout. Hard stopping.",
        ]
        if any(pattern in output for pattern in timeout_patterns):
            return True  # Timeout is successful execution, just incomplete
        if "TIMEOUT" in output and "|Timeout" in output:
            return True

        # Unknown results are NOT failures
        if "all events were sent without errors" in output:
            return True

        # Only these patterns indicate genuine failures
        fatal_error_patterns = [
            "has no bytecode.",
            "ERROR FUNCTION_BUILDER",
            "java.lang.OutOfMemoryError",
            "no candidate functions to instantiate parametric rule",
            "compilation failed",
            "syntax error",
            "file not found",
            "permission denied",
        ]

        for pattern in fatal_error_patterns:
            if pattern in output:
                return False

        # For everything else with non-zero return code, check if it's just warnings
        # If we don't see fatal errors, consider it successful
        return True

    def _parse_successful_output(
        self, stdout: str, stderr: str
    ) -> tuple[Optional[str], Dict[str, Any]]:
        """Parse successful prover output to extract emv-* folder path for ProverOutputUtility."""
        emv_folder_path = None
        output_data: Dict[str, Any] = {}

        # Look for Certora output patterns in stdout to find emv-* folder
        for line in stdout.split("\n"):
            line = line.strip()

            # Look for "Reports in file:///path/to/emv-X-certora-date--time/Reports"
            if line.startswith("Reports in file://"):
                import re

                # Extract the directory path from file:// URL
                path_match = re.search(r"file://([^/].*/emv-[^/]+)", line)
                if path_match:
                    emv_folder_path = path_match.group(1)

                    # Set report_path for compatibility
                    reports_dir = Path(emv_folder_path) / "Reports"
                    output_json = reports_dir / "output.json"
                    if output_json.exists():
                        output_data["report_path"] = str(output_json)

            # Also look for "Final report in emv-X-certora-date--time/Reports/FinalResults.html"
            elif line.startswith("Final report in ") and "emv-" in line:
                import re

                # Extract the path relative to project root
                path_match = re.search(r"Final report in ([^/]*emv-[^/]+)/Reports/", line)
                if path_match:
                    relative_output_dir = path_match.group(1)
                    emv_folder_path = str(self.project_root / relative_output_dir)

                    # Set report_path for compatibility
                    reports_dir = Path(emv_folder_path) / "Reports"
                    output_json = reports_dir / "output.json"
                    if output_json.exists():
                        output_data["report_path"] = str(output_json)

            # Look for verification results summary
            if "verified" in line.lower() or "failed" in line.lower():
                if "verification_summary" not in output_data:
                    output_data["verification_summary"] = []
                output_data["verification_summary"].append(line)

        # Store raw output for debugging
        output_data["stdout"] = stdout
        output_data["raw_stdout"] = stdout
        if stderr:
            output_data["stderr"] = stderr

        return emv_folder_path, output_data

    def _extract_error_message(self, stdout: str, stderr: str, return_code: int) -> str:
        """Extract meaningful error message from failed execution."""
        # Try to find specific error messages in stderr first
        if stderr:
            error_lines = []
            for line in stderr.split("\n"):
                line = line.strip()
                if line and (
                    "error" in line.lower()
                    or "failed" in line.lower()
                    or "exception" in line.lower()
                ):
                    error_lines.append(line)

            if error_lines:
                return "; ".join(error_lines[:3])  # Take first 3 error lines

        # Try stdout for error messages
        if stdout:
            error_lines = []
            for line in stdout.split("\n"):
                line = line.strip()
                if line and ("error" in line.lower() or "failed" in line.lower()):
                    error_lines.append(line)

            if error_lines:
                return "; ".join(error_lines[:3])

        # Fallback to generic error with return code
        return f"Local prover failed with return code {return_code}"

    async def cleanup_completed_jobs(self) -> None:
        """Clean up tracking data for completed jobs (no-op for local runner)."""
        # Local runner doesn't maintain persistent job tracking
        # All cleanup happens automatically after execution
        self.log("Local runner cleanup completed (no persistent jobs)")
        pass

    async def submit_jobs(
        self, job_specs: List[ProverJobSpec], pre_execute_callback=None
    ) -> List[SubmissionResult]:
        """Submit multiple jobs - for local runner, this executes jobs immediately."""
        results = []
        for job_spec in job_specs:
            if pre_execute_callback:
                try:
                    pre_execute_callback(job_spec)
                except Exception as e:
                    results.append(SubmissionResult(
                        job_url=None,
                        output=str(e),
                        return_code=-1,
                        error_message=f"Pre-execute callback failed: {e}",
                    ))
                    continue

            # Execute the job immediately with check_with_prover
            prover_result = await self.check_with_prover(job_spec)
            job_id = prover_result.job_handle.job_id if prover_result.job_handle else None
            results.append(SubmissionResult(
                job_url=job_id,
                output=prover_result.output_data.get("stdout", ""),
                return_code=prover_result.output_data.get("return_code", 0 if prover_result.success else -1),
                error_message=prover_result.error_message,
            ))
        return results

    async def submit_and_wait_for_jobs_with_transformer(
        self,
        job_specs: List[ProverJobSpec],
        completion_callback=None,
        pre_execute_callback=None,
        early_termination_callback: Optional[EarlyTerminationCallback] = None,
        result_transformer: Optional[ResultTransformer] = None,
        use_queue: bool = False,
    ) -> List[ProverResult]:
        """
        Submit and wait for jobs with transformer support.

        For local runner, executes jobs in parallel using check_with_prover.
        Supports early termination and result transformation.
        """
        if not job_specs:
            return []

        self.log(f"🚀 Executing {len(job_specs)} verification jobs locally...")

        async def execute_single_job(job_spec: ProverJobSpec) -> ProverResult:
            """Execute a single job with callbacks and transformation."""
            # Apply pre-execute callback if provided
            if pre_execute_callback:
                try:
                    pre_execute_callback(job_spec)
                except Exception as e:
                    self.log(f"[{job_spec.contract_name}] Pre-execute callback failed: {e}")
                    return self._create_failed_result(job_spec, f"Pre-execute callback failed: {e}")

            # Execute job
            result = await self.check_with_prover(job_spec)

            # Apply result transformer if provided
            if result_transformer and result.success:
                try:
                    result.transformed_result = result_transformer(result)
                except Exception as e:
                    self.log(f"Failed to transform result for {job_spec.contract_name}: {e}")

            # Call completion callback if provided
            if completion_callback:
                try:
                    completion_callback(result)
                except Exception as e:
                    self.log(f"[{job_spec.contract_name}] Completion callback failed: {e}")

            return result

        # Create tasks for all jobs
        tasks = [
            asyncio.create_task(execute_single_job(job_spec))
            for job_spec in job_specs
        ]

        # Wait for all jobs with optional early termination
        if early_termination_callback is None:
            # Simple case: wait for all
            results = list(await asyncio.gather(*tasks, return_exceptions=False))
        else:
            # Complex case: wait with early termination support
            results = await self._wait_for_tasks_with_early_termination(
                tasks, job_specs, early_termination_callback
            )

        # Log completion results
        completed_count = sum(1 for result in results if result.success)
        self.log(f"✅ Completed {completed_count}/{len(job_specs)} jobs successfully")

        return results

    async def _wait_for_tasks_with_early_termination(
        self,
        tasks: List[asyncio.Task],
        job_specs: List[ProverJobSpec],
        early_termination_callback: EarlyTerminationCallback,
    ) -> List[ProverResult]:
        """Wait for tasks with early termination support."""
        # Create mapping from task to job_spec
        task_to_job_spec = {tasks[i]: job_specs[i] for i in range(len(tasks))}

        completed_results: List[ProverResult] = []
        pending_tasks = set(tasks)

        self.log(f"Starting parallel execution for {len(tasks)} jobs with early termination")

        # Main loop: wait for tasks to complete one by one
        while pending_tasks:
            # Wait for at least one task to complete
            done, pending_tasks = await asyncio.wait(
                pending_tasks, return_when=asyncio.FIRST_COMPLETED
            )

            # Process all completed tasks to collect their results
            newly_completed_results: List[ProverResult] = []
            for task in done:
                try:
                    result = await task
                    completed_results.append(result)
                    newly_completed_results.append(result)
                except Exception as e:
                    self.log(f"Failed to process a prover result: {e}")
                    # Create a failed result for the exception
                    job_spec = task_to_job_spec.get(task)
                    if job_spec:
                        failed_result = self._create_failed_result(job_spec, str(e))
                        completed_results.append(failed_result)
                        newly_completed_results.append(failed_result)

            # Check for early termination
            early_termination_triggered = False
            for result in newly_completed_results:
                if early_termination_callback.should_terminate(result, completed_results):
                    early_termination_triggered = True
                    break

            if early_termination_triggered:
                self.log(f"Early termination triggered. Cancelling {len(pending_tasks)} remaining jobs.")

                # Cancel all remaining tasks and create cancelled results
                for pending_task in list(pending_tasks):
                    pending_task.cancel()
                    pending_job_spec = task_to_job_spec[pending_task]

                    # Create cancelled result
                    cancelled_result = ProverResult.create_cancelled_result(
                        pending_job_spec, "", RunnerType.LOCAL
                    )
                    completed_results.append(cancelled_result)

                # Early termination - return immediately
                return completed_results

        return completed_results

    def _create_failed_result(self, job_spec: ProverJobSpec, error_msg: str) -> ProverResult:
        """Create a failed ProverResult for a job spec."""
        import time

        job_handle = JobHandle(
            job_id="failed",
            config_file=str(job_spec.config_file.path),
            config_content_hash="failed",
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=RunnerType.LOCAL,
            status=JobStatus.FAILED,
        )

        return ProverResult(
            job_handle=job_handle,
            success=False,
            report_path=None,
            output_data={},
            job_spec=job_spec,
            error_message=error_msg,
            transformed_result=None,
        )
