#!/usr/bin/env python3
"""
Cloud Job Manager - Simplified cloud prover execution.

Executes prover jobs on the cloud using `certoraRun <conf_file.conf>` with
caching support.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, cast

from prover_output_utility import ProverOutputAPI, cloud_server_for_env # type: ignore[import-untyped]
from prover_output_utility.models import JobStatus as ProverJobStatus # type: ignore[import-untyped]

from certora_autosetup.cache.cache_fs import cache_path, get_fs
from .constants import DIR_CERTORA_INTERNAL, DIR_JOB_RESULT_CACHE
from .enhanced_config_manager import ConfigManager, ProverJobSpec
from .job_utils import extract_job_url_from_text
from .prover_runner import (
    EarlyTerminationCallback,
    ProverRunner,
    ResultTransformer,
)
from .runner_types import (
    JobHandle,
    JobStatus,
    ProverResult,
    RunnerType,
    SubmissionResult,
)
from .logger import log_with_contract


def extract_job_url(output: str) -> Optional[str]:
    """Extract job URL from certoraRun output."""
    return extract_job_url_from_text(output)


class CloudProverRunner(ProverRunner):
    """
    Cloud job manager for prover execution.

    Simplified implementation that executes certoraRun for cloud submissions.
    """

    # Timeout for job completion including job queue time - 150 minutes
    JOB_TIMEOUT_SECONDS = 7200 + 1800

    def __init__(
        self,
        project_root: Path,
        config_manager: ConfigManager,
        certora_run_path: str = "certoraRun",
        cloud_server: str | None = None,
        disable_cache: bool = False,
        cancel_jobs_on_cleanup: bool = True,
    ):
        """
        Initialize cloud job manager.

        Args:
            project_root: Root directory of the project
            config_manager: Configuration manager for dependency tracking
            certora_run_path: Path to certoraRun executable
            cloud_server: certoraRun --server value. When None (default), it is
                resolved from CI's GITHUB_ENVIRONMENT if set, else derived from
                the deployment env (AISS_ENV) via POU's cloud_server_for_env() —
                Autosetup does not hardcode a server.
            disable_cache: If True, disable caching (useful for tests)
            cancel_jobs_on_cleanup: If True, actually cancel jobs on Certora's servers during cleanup (default: True)
        """
        super().__init__(project_root, config_manager, use_local_api=False)
        self.component = "CloudRunner"
        self.certora_run_path = certora_run_path
        # Resolve the certoraRun --server: an explicit arg wins; otherwise honor
        # CI's GITHUB_ENVIRONMENT if set; otherwise derive from the deployment
        # env (AISS_ENV) via POU. No hardcoded prod default. (CloudProverRunner
        # is only used on the cloud path — the local path uses LocalProverRunner
        # with no server.)
        self.cloud_server = (
            cloud_server
            if cloud_server is not None
            else os.environ.get("GITHUB_ENVIRONMENT") or cloud_server_for_env()
        )
        self.disable_cache = disable_cache
        self.cancel_jobs_on_cleanup = cancel_jobs_on_cleanup
        self.job_wait_timeout = self.JOB_TIMEOUT_SECONDS

        # Progress tracking counters (read from spinner thread, written under asyncio lock)
        self._active_jobs = 0
        self._total_completed = 0

    @property
    def active_jobs_count(self) -> int:
        """Number of jobs currently being processed."""
        return self._active_jobs

    @property
    def completed_jobs_count(self) -> int:
        """Number of jobs completed so far."""
        return self._total_completed

    async def get_runner_type(self) -> RunnerType:
        """Get the type of this runner."""
        return RunnerType.CLOUD

    async def _check_with_prover_impl(self, job_spec: ProverJobSpec) -> ProverResult:
        """
        Execute prover on cloud with caching support.

        Args:
            job_spec: Complete job specification

        Returns:
            ProverResult with job status and results
        """
        # Use existing cache key functionality from config_manager
        cache_key = self._get_cache_key(job_spec)

        log_with_contract(
            self.component,
            "info",
            job_spec.contract_name,
            f"Cloud prover request for {job_spec.phase} (cache_key: {cache_key[:16]}...)",
        )

        # Step 1: Check result cache for completed results (fast path)
        if not self.disable_cache:
            cached_result = await self._check_cache(cache_key, job_spec)
            if cached_result:
                self.log(
                    f"Using cached completed result for {job_spec.contract_name}:{job_spec.phase}"
                )
                return cached_result

        # Step 2: Check job URL cache for submitted/running jobs (resume path)
        job_url = None
        if not self.disable_cache:
            submitted_job_handle = await self._check_submission_cache(cache_key)
            if submitted_job_handle:
                job_url = submitted_job_handle.job_id
                self.log(
                    f"Resuming job for {job_spec.contract_name}:{job_spec.phase} - {job_url}"
                )

        # Step 3: If no cached job, submit new job
        submission_result = None
        if not job_url:
            log_with_contract(
                self.component,
                "debug",
                job_spec.contract_name,
                f"Submitting new cloud job for {job_spec.phase}",
            )
            submission_result = await self._submit_new_job(job_spec, cache_key)
            if not submission_result.success:
                # Submission failed - create failed result with full output for filtering check
                return self._create_failed_result_with_output(
                    job_spec,
                    cache_key,
                    "Failed to submit job",
                    submission_result.output,
                    submission_result.return_code,
                )
            else:
                # Success case - get job URL from submission result
                job_url = submission_result.job_url
                assert job_url is not None, (
                    "job_url should not be None when submission is successful"
                )

        # Step 4: Wait for job completion and parse results using ProverOutputAPI
        result = await self._wait_and_parse_job_results(
            job_url, job_spec, cache_key, submission_result
        )

        # Step 5: Cache completed results and clean up submission cache
        if result.success and not self.disable_cache:
            await self._cache_result(cache_key, result)
            await self._remove_submission_cache(cache_key)

        return result

    async def submit_job(
        self, job_spec: ProverJobSpec, pre_execute_callback=None
    ) -> SubmissionResult:
        """Submit job to cloud (or return cached job URL). Fast operation - does not wait."""
        # Check cache BEFORE pre-execute callback (e.g. typechecker fix) to avoid
        # expensive compilation steps when results are already cached.
        # This is safe because the typechecker fix overwrites the config on disk,
        # so on subsequent runs the config is already fixed and the cache key matches.
        cache_key = self._get_cache_key(job_spec)
        if not self.disable_cache:
            cached_result = await self._check_cache(cache_key, job_spec)
            if cached_result:
                self.log(
                    f"Using cached completed result for {job_spec.contract_name}:{job_spec.phase}"
                )
                return SubmissionResult(
                    job_url=cached_result.job_handle.job_id,
                    output="Cached result",
                    return_code=0,
                )
            submitted_job_handle = await self._check_submission_cache(cache_key)
            if submitted_job_handle:
                job_url = submitted_job_handle.job_id
                self.log(
                    f"Resuming job for {job_spec.contract_name}:{job_spec.phase} - {job_url}"
                )
                return SubmissionResult(
                    job_url=job_url,
                    output="Resumed job",
                    return_code=0,
                )

        # Cache miss — apply pre-execute callback (e.g. typechecker fix) before submission
        if pre_execute_callback:
            try:
                pre_execute_callback(job_spec)
            except Exception as e:
                self.log(f"[{job_spec.contract_name}] Pre-execute callback failed: {e}")
                return SubmissionResult(
                    job_url=None,
                    output="",
                    return_code=-1,
                    error_message=f"Pre-execute callback failed: {e}",
                )
            # Recompute cache key — callback may have modified the config file
            cache_key = self._get_cache_key(job_spec)

        log_with_contract(
            self.component,
            "info",
            job_spec.contract_name,
            f"Cloud job submission for {job_spec.phase} (cache_key: {cache_key[:16]}...)",
        )

        # Submit new job
        log_with_contract(
            self.component,
            "debug",
            job_spec.contract_name,
            f"Submitting new cloud job for {job_spec.phase}",
        )
        submission_result = await self._submit_new_job(job_spec, cache_key)
        return submission_result

    async def wait_for_completion(
        self, job_url: str, job_spec: ProverJobSpec, completion_callback=None
    ) -> ProverResult:
        """Wait for job completion and parse results. Blocking operation."""
        cache_key = self._get_cache_key(job_spec)

        # Check if we already have cached results
        if not self.disable_cache:
            cached_result = await self._check_cache(cache_key, job_spec)
            if cached_result:
                self.log(
                    f"Using cached completed result for {job_spec.contract_name}:{job_spec.phase}"
                )

                if completion_callback:
                    completion_callback(cached_result)

                return cached_result

        # Wait for job completion and parse results
        result = await self._wait_and_parse_job_results(job_url, job_spec, cache_key)

        # Cache completed results and clean up submission cache
        if result.success and not self.disable_cache:
            await self._cache_result(cache_key, result)
            await self._remove_submission_cache(cache_key)

        if completion_callback:
            completion_callback(result)

        return result

    async def submit_jobs(
        self, job_specs: List[ProverJobSpec], pre_execute_callback=None
    ) -> List[SubmissionResult]:
        """Submit multiple jobs and return job URLs. Fast operation - does not wait."""
        tasks = [
            self.submit_job(job_spec, pre_execute_callback=pre_execute_callback)
            for job_spec in job_specs
        ]
        results_raw = await asyncio.gather(*tasks, return_exceptions=True)
        # Convert exceptions to SubmissionResults
        processed_results: List[SubmissionResult] = []
        for i, raw_result in enumerate(results_raw):
            if isinstance(raw_result, Exception):
                processed_results.append(SubmissionResult(
                    job_url=None,
                    output=str(raw_result),
                    return_code=-1,
                    error_message=f"Submission failed: {raw_result}"
                ))
            else:
                # raw_result is guaranteed to be SubmissionResult here
                processed_results.append(cast(SubmissionResult, raw_result))
        return processed_results



    async def _create_cancelled_result(
        self, job_spec: ProverJobSpec, job_url: str
    ) -> ProverResult:
        """Helper method to create ProverResult for cancelled jobs."""
        return ProverResult.create_cancelled_result(
            job_spec, job_url, await self.get_runner_type()
        )

    async def _wait_for_tasks_with_early_termination(
        self,
        tasks: List[asyncio.Task],
        job_specs: List[ProverJobSpec],
        early_termination_callback: "EarlyTerminationCallback",
    ) -> List[ProverResult]:
        """Wait for tasks with early termination support."""
        # Create mapping from task to job_spec
        task_to_job_spec = {tasks[i]: job_specs[i] for i in range(len(tasks))}

        completed_results = []
        pending_tasks = set(tasks)

        self.log(f"Starting parallel execution for {len(tasks)} jobs with early termination")

        # Main loop: wait for tasks to complete one by one
        while pending_tasks:
            # Wait for at least one task to complete
            done, pending_tasks = await asyncio.wait(
                pending_tasks, return_when=asyncio.FIRST_COMPLETED
            )

            # Process all completed tasks to collect their results
            newly_completed_results = []
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
                        failed_result = self._create_failed_result_for_spec(
                            job_spec, str(e)
                        )
                        completed_results.append(failed_result)
                        newly_completed_results.append(failed_result)

            # Check for early termination
            early_termination_triggered = False
            for result in newly_completed_results:
                if early_termination_callback.should_terminate(
                    result, completed_results
                ):
                    early_termination_triggered = True
                    break

            if early_termination_triggered:
                self.log(
                    f"Early termination triggered. Cancelling {len(pending_tasks)} remaining jobs."
                )

                # Cancel all remaining tasks and create cancelled results
                for pending_task in list(pending_tasks):
                    pending_task.cancel()
                    pending_job_spec = task_to_job_spec[pending_task]

                    # Create cancelled result
                    cancelled_result = await self._create_cancelled_result(
                        pending_job_spec, job_url=""
                    )
                    completed_results.append(cancelled_result)

                # Early termination - return immediately
                return completed_results

        return completed_results

    async def _submit_and_wait_single_job(
        self,
        job_spec: ProverJobSpec,
        completion_callback=None,
        pre_execute_callback=None,
        result_transformer: Optional["ResultTransformer"] = None,
    ) -> ProverResult:
        """Submit and wait for a single job to complete."""
        # Submit the job
        submission_result = await self.submit_job(job_spec, pre_execute_callback=pre_execute_callback)

        # Handle submission exceptions
        if isinstance(submission_result, BaseException):
            self.log(
                f"✗ Exception during submission for {job_spec.contract_name} for {job_spec.phase}: {str(submission_result)}"
            )
            return self._create_failed_result_for_spec(
                job_spec, str(submission_result)
            )

        # Handle submission failures
        if not submission_result.success:
            self.log(
                f"✗ Submission marked as failed for {job_spec.contract_name} for {job_spec.phase}"
            )
            return self._create_failed_result_for_spec_with_output(
                job_spec, submission_result
            )

        # Successfully submitted - wait for completion
        job_url = submission_result.job_url
        assert job_url is not None, (
            "job_url should not be None when submission is successful"
        )
        self.log(
            f"✓ Successfully submitted {job_spec.contract_name} for {job_spec.phase} - job_url: {job_url}"
        )

        # Wait for the job to complete
        result = await self.wait_for_completion(job_url, job_spec, completion_callback)

        # Apply result transformer if provided
        if result_transformer:
            result = self._add_transformed_result(result, result_transformer)

        return result

    async def submit_and_wait_for_jobs_with_transformer(
        self,
        job_specs: List[ProverJobSpec],
        completion_callback=None,
        pre_execute_callback=None,
        early_termination_callback: Optional["EarlyTerminationCallback"] = None,
        result_transformer: Optional["ResultTransformer"] = None,
        use_queue: bool = False,
    ) -> List[ProverResult]:
        """Submit multiple jobs and wait for all to complete. Complete parallel execution.

        If use_queue is True, uses a queue-based approach that allows the completion_callback
        to add new jobs dynamically. The completion_callback will receive (result, job_queue)
        and can call job_queue.put_nowait(new_job_spec) to add jobs.
        """
        if not job_specs:
            return []

        # Use queue-based processing when use_queue is True
        if use_queue:
            return await self._process_jobs_with_queue(
                job_specs,
                completion_callback,
                pre_execute_callback,
                result_transformer,
            )

        self.log(f"🚀 Submitting and processing {len(job_specs)} verification jobs...")

        async def _tracked_submit(spec: ProverJobSpec) -> ProverResult:
            self._active_jobs += 1
            try:
                return await self._submit_and_wait_single_job(
                    spec, completion_callback, pre_execute_callback, result_transformer
                )
            finally:
                self._active_jobs -= 1
                self._total_completed += 1

        # Create tasks for all jobs
        tasks = [asyncio.create_task(_tracked_submit(job_spec)) for job_spec in job_specs]

        # Wait for all jobs with optional early termination
        if early_termination_callback is None:
            # Simple case: wait for all
            results = await asyncio.gather(*tasks)
        else:
            # Complex case: wait with early termination support
            results = await self._wait_for_tasks_with_early_termination(
                tasks, job_specs, early_termination_callback
            )

        # Log completion results
        completed_count = sum(
            1
            for result in results
            if not isinstance(result, Exception)
            and result.success
        )
        self.log(
            f"✅ Completed {completed_count}/{len(job_specs)} jobs successfully"
        )

        return results

    async def _process_jobs_with_queue(
        self,
        initial_job_specs: List[ProverJobSpec],
        completion_callback,
        pre_execute_callback,
        result_transformer: Optional["ResultTransformer"],
    ) -> List[ProverResult]:
        """Process jobs using a queue, allowing dynamic addition of new jobs.

        Uses worker tasks that process jobs from the queue. The completion_callback
        receives (result, job_queue) and can add new jobs via job_queue.put_nowait().
        """
        job_queue: asyncio.Queue[ProverJobSpec] = asyncio.Queue()
        results: List[ProverResult] = []
        results_lock = asyncio.Lock()

        # Add initial jobs to queue
        for job_spec in initial_job_specs:
            await job_queue.put(job_spec)

        initial_count = len(initial_job_specs)
        self.log(f"🚀 Starting queue-based processing with {initial_count} initial jobs...")

        async def worker():
            while True:
                job_spec = await job_queue.get()
                self._active_jobs += 1
                try:
                    result = await self._submit_and_wait_single_job(
                        job_spec,
                        lambda r: completion_callback(r, job_queue) if completion_callback else None,
                        pre_execute_callback,
                        result_transformer,
                    )
                    async with results_lock:
                        results.append(result)
                except Exception as e:
                    self.log(f"Worker error: {e}", "ERROR")
                finally:
                    self._active_jobs -= 1
                    self._total_completed += 1
                    job_queue.task_done()

        # Start worker tasks - one per initial job for full parallelism
        num_workers = len(initial_job_specs) or 1
        workers = [asyncio.create_task(worker()) for _ in range(num_workers)]

        # Wait for all jobs to be processed (join waits for all task_done() calls)
        await job_queue.join()

        # Cancel workers (they're blocked waiting on the empty queue)
        for w in workers:
            w.cancel()

        # Log completion
        completed_count = sum(1 for r in results if r.success)
        total_count = len(results)
        generated_count = total_count - initial_count
        self.log(
            f"✅ Completed {completed_count}/{total_count} jobs successfully "
            f"({initial_count} initial + {generated_count} generated)"
        )

        return results

    async def submit_and_wait_for_jobs(
        self,
        job_specs: List[ProverJobSpec],
        completion_callback=None,
        pre_execute_callback=None,
        early_termination_callback: Optional[EarlyTerminationCallback] = None,
        use_queue: bool = False,
    ) -> List[ProverResult]:
        """
        Submit multiple jobs and wait for all to complete (backward compatibility).

        This method now just calls submit_and_wait_for_jobs_with_transformer
        since ProverResult now includes transformation support.
        """
        return await self.submit_and_wait_for_jobs_with_transformer(
            job_specs=job_specs,
            completion_callback=completion_callback,
            pre_execute_callback=pre_execute_callback,
            early_termination_callback=early_termination_callback,
            result_transformer=None,
            use_queue=use_queue,
        )

    def _add_transformed_result(
        self,
        prover_result: ProverResult,
        result_transformer: Optional[ResultTransformer] = None,
    ) -> ProverResult:
        """Helper method to enrich ProverResult with optional transformation."""
        if result_transformer and not isinstance(prover_result, Exception):
            try:
                prover_result.transformed_result = result_transformer(prover_result)
            except Exception as e:
                self.log(
                    f"Failed to transform result for {prover_result.job_spec.contract_name}: {e}"
                )
                # Continue without transformation
        return prover_result

    def _create_failed_result_for_spec(
        self, job_spec: ProverJobSpec, error_msg: str
    ) -> ProverResult:
        """Create a failed ProverResult for a job spec."""
        import time

        from .runner_types import JobStatus, RunnerType

        job_handle = JobHandle(
            job_id="failed",
            config_file=str(job_spec.config_file.path),
            config_content_hash="failed",
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=RunnerType.CLOUD,
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

    def _create_failed_result_for_spec_with_output(
        self, job_spec: ProverJobSpec, submission_result: SubmissionResult
    ) -> ProverResult:
        """Create a failed ProverResult for a job spec with full output data."""
        import time

        from .runner_types import JobStatus, RunnerType

        job_handle = JobHandle(
            job_id="failed",
            config_file=str(job_spec.config_file.path),
            config_content_hash="failed",
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=RunnerType.CLOUD,
            status=JobStatus.FAILED,
        )

        return ProverResult(
            job_handle=job_handle,
            success=False,
            report_path=None,
            output_data={
                "output": submission_result.output,
                "return_code": submission_result.return_code,
            },
            job_spec=job_spec,
            error_message=submission_result.error_message or "No job URL returned",
            transformed_result=None,
        )

    async def _submit_new_job(
        self, job_spec: ProverJobSpec, cache_key: str
    ) -> SubmissionResult:
        """Execute cloud certoraRun command."""
        # Create job handle for result tracking
        job_handle = JobHandle(
            job_id="",  # Will be updated with job URL after execution
            config_file=str(job_spec.config_file.path),
            config_content_hash=cache_key,
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=RunnerType.CLOUD,
            status=JobStatus.SUBMITTED,
        )

        start_time = time.time()

        try:
            # Build certoraRun command for cloud execution with server and version flags
            cmd = [self.certora_run_path, str(job_spec.config_file.path)]
            if not job_spec.extra_args or "--server" not in job_spec.extra_args:
                cmd += ["--server", self.cloud_server]

            # Append extra_args if present
            if job_spec.extra_args:
                cmd.extend(job_spec.extra_args)

            # Append msg if present
            if job_spec.msg:
                cmd.extend(["--msg", job_spec.msg])

            # In CI, add --wait_for_results none if not already present
            import os

            if os.getenv("CI"):
                if "--wait_for_results" not in cmd:
                    cmd = cmd + ["--wait_for_results", "none"]

            self.log(f"Executing: {' '.join(cmd)}")

            # Execute command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.project_root,
            )

            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8") if stdout else ""

            duration = time.time() - start_time
            return_code: int = process.returncode or -1

            # Check for symlink creation error which is not a real failure
            # This can appear in either stdout or stderr
            has_symlink_error = (
                "Failed to create the '.certora_internal/latest' symlink" in output
                and "[Errno 17] File exists" in output
            )

            # For cloud jobs, always try to extract job URL regardless of return code
            job_url = extract_job_url(output)

            if return_code == 0 or has_symlink_error or job_url:
                if has_symlink_error:
                    self.log(
                        f"Symlink race condition detected but handled gracefully for {job_spec.contract_name}"
                    )
                if job_url:
                    job_handle.job_id = job_url
                    job_handle.status = JobStatus.SUBMITTED

                    # Cache the submitted job for resume functionality
                    if not self.disable_cache:
                        await self._cache_submitted_job(cache_key, job_handle)

                    log_with_contract(
                        self.component,
                        "info",
                        job_spec.contract_name,
                        f"Cloud job submitted successfully in {duration:.1f}s - {job_url}",
                    )

                    return SubmissionResult(
                        job_url=job_url,
                        output=output,
                        return_code=return_code,
                    )
                else:
                    self.log(
                        f"❌ No job URL found in certoraRun output for {job_spec.contract_name}:{job_spec.phase}"
                    )
                    return SubmissionResult(
                        job_url=None,
                        output=output,
                        return_code=return_code,
                        error_message="No job URL found in certoraRun output",
                    )
            else:
                # Failure - log error and return failure info for filtering check
                error_msg = self._extract_error_message(output, return_code)
                self.log(
                    f"Cloud job submission failed after {duration:.1f}s: {error_msg}"
                )
                # Log output for debugging CI issues (truncated to avoid log spam)
                output_preview = output[:1000] + "..." if len(output) > 1000 else output
                self.log(f"certoraRun output: {output}")

                return SubmissionResult(
                    job_url=None,
                    output=output,
                    return_code=return_code,
                    error_message=error_msg,
                )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Cloud job submission failed: {str(e)}"
            self.log(error_msg)
            return SubmissionResult(
                job_url=None,
                output=str(e),
                return_code=-1,
                error_message=error_msg,
            )

    async def _wait_and_parse_job_results(
        self,
        job_url: str,
        job_spec: ProverJobSpec,
        cache_key: str,
        submission_result: Optional[SubmissionResult] = None,
    ) -> ProverResult:
        """Wait for job completion and parse results using ProverOutputAPI."""
        start_time = time.time()

        # Create job handle for tracking
        job_handle = JobHandle(
            job_id=job_url,
            config_file=str(job_spec.config_file.path),
            config_content_hash=cache_key,
            phase=job_spec.phase,
            submitted_at=start_time,
            runner_type=RunnerType.CLOUD,
            status=JobStatus.RUNNING,
        )

        try:
            # Initialize ProverOutputAPI
            prover_api = ProverOutputAPI(use_local=False)

            log_with_contract(
                self.component,
                "info",
                job_spec.contract_name,
                f"Waiting for job completion: {job_url}",
            )

            # Wait for job completion with configurable timeout
            job_wait_timeout = self.job_wait_timeout
            success, prover_start_time, prover_finish_time = await self._wait_for_job_completion_with_api(
                prover_api, job_url, job_wait_timeout
            )

            duration = time.time() - start_time

            # Fresh run (cache hits short-circuit before this method) — record the
            # prover's server-reported runtime, computed from the job's start/finish
            # timestamps (JobInfo.runtime). For a completed job this read is cheap
            # (cache-served from polling). Best-effort; the exception path below has
            # nothing to record anyway.
            try:
                self._record_prover_runtime_seconds(prover_api.get_job_info(job_url).runtime)
            except Exception as e:
                self.log(f"Could not record prover runtime for usage ledger: {e}", "DEBUG")

            if success:
                # Job completed successfully, parse results
                job_handle.status = JobStatus.COMPLETED

                log_with_contract(
                    self.component,
                    "info",
                    job_spec.contract_name,
                    f"Job completed successfully in {duration:.1f}s, parsing results...",
                )

                # Parse rule results using the inherited method
                rule_results = self.parse_rule_results_from_job(job_url)

                # Extract unresolved calls using the existing method (same as LocalRunner)
                unresolved_calls = self.extract_unresolved_calls(job_url)

                # Fetch parsed alerts using ProverOutputAPI
                alerts = []
                try:
                    alerts = prover_api.get_alerts(job_url)
                except Exception as e:
                    self.log(
                        f"Failed to fetch alerts for {job_url}: {e}", "WARNING"
                    )
                    # Continue without alerts - not a critical failure

                # Extract unresolved calls for setup completeness checking
                unresolved_calls = []
                try:
                    unresolved_calls = self.extract_unresolved_calls(job_url)
                except Exception as e:
                    self.log(
                        f"Failed to extract unresolved calls for {job_url}: {e}", "WARNING"
                    )
                    # Continue without unresolved calls - not a critical failure

                return ProverResult(
                    job_handle=job_handle,
                    success=True,
                    report_path=None,  # Could be extracted from ProverAPI if needed
                    output_data={
                        "job_url": job_url,
                        "rule_count": len(rule_results),
                        "output": submission_result.output if submission_result else "",
                        "return_code": submission_result.return_code
                        if submission_result
                        else 0,
                        "unresolved_calls": unresolved_calls,
                        "prover_start_time": prover_start_time,
                        "prover_finish_time": prover_finish_time,
                    },
                    job_spec=job_spec,
                    rule_results=rule_results,
                    alerts=alerts,
                    duration=duration,
                    transformed_result=None,
                )
            else:
                # Job failed or timed out
                job_handle.status = JobStatus.FAILED
                error_msg = f"Job failed or timed out after {duration:.1f}s"

                log_with_contract(
                    self.component,
                    "error",
                    job_spec.contract_name,
                    error_msg,
                )

                return ProverResult(
                    job_handle=job_handle,
                    success=False,
                    report_path=None,
                    output_data={
                        "job_url": job_url,
                        "output": submission_result.output if submission_result else "",
                        "return_code": submission_result.return_code
                        if submission_result
                        else 0,  # If we have a job URL, submission was successful
                    },
                    job_spec=job_spec,
                    error_message=error_msg,
                    duration=duration,
                    transformed_result=None,
                )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Error waiting for job completion: {str(e)}"
            job_handle.status = JobStatus.FAILED

            log_with_contract(
                self.component,
                "error",
                job_spec.contract_name,
                error_msg,
            )

            return ProverResult(
                job_handle=job_handle,
                success=False,
                report_path=None,
                output_data={"job_url": job_url},
                job_spec=job_spec,
                error_message=error_msg,
                duration=duration,
                transformed_result=None,
            )

    async def _wait_for_job_completion_with_api(
        self, prover_api: ProverOutputAPI, job_url: str, timeout_seconds: int
    ) -> tuple[bool, Optional[float], Optional[float]]:
        """Wait for job completion using ProverOutputAPI.

        Returns:
            Tuple of (success, prover_start_time, prover_finish_time).
            Times may be None if unavailable.
        """
        import asyncio

        start_time = time.time()
        poll_interval = 10  # Poll every 10 seconds for faster completion detection

        while time.time() - start_time < timeout_seconds:
            try:
                # Check job status using ProverOutputAPI with timeout
                elapsed = time.time() - start_time

                call_start = time.time()
                try:
                    # Wrap the API call with a timeout to prevent infinite hanging
                    job_info = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, prover_api.get_job_info, job_url
                        ),
                        timeout=60,  # 60 second timeout for each API call
                    )
                    call_duration = time.time() - call_start
                except asyncio.TimeoutError:
                    call_duration = time.time() - call_start
                    self.log(
                        f"❌ [{elapsed:.1f}s] get_job_info timed out after {call_duration:.1f}s"
                    )
                    continue  # Skip this iteration and try again

                if job_info and hasattr(job_info, "status"):
                    self.log(f"📊 Job status for {job_url}: {job_info.status}", "DEBUG")
                    # Log additional job info if available
                    prover_start = getattr(job_info, "start_time", None)
                    prover_finish = getattr(job_info, "finish_time", None)
                    if prover_start is not None:
                        self.log(f"   Start time: {prover_start}", "DEBUG")
                    if prover_finish is not None:
                        self.log(f"   Finish time: {prover_finish}", "DEBUG")
                    if job_info.status in [ProverJobStatus.SUCCEEDED, ProverJobStatus.HALTED]:
                        # Note: HALTED jobs are treated as successful in PreAudit because they often contain
                        # partial results for some rules that can still be analyzed
                        self.log(f"Job completed successfully: {job_url}")
                        return True, prover_start, prover_finish
                    elif job_info.status in [ProverJobStatus.FAILED, ProverJobStatus.CANCELED, ProverJobStatus.SERVICE_UNAVAILABLE, ProverJobStatus.UPLOAD_FAILED]:
                        self.log(f"Job failed with status {job_info.status}: {job_url}")
                        return False, prover_start, prover_finish
                    # Check if job has completed but with an unrecognized status
                    elif hasattr(job_info, "is_completed") and job_info.is_completed:
                        self.log(
                            f"❌ Job completed with unrecognized status '{job_info.status}' - "
                            f"treating as failed: {job_url}",
                            "WARNING"
                        )
                        return False, prover_start, prover_finish
                    # If status is 'RUNNING', 'QUEUED', etc., continue waiting
                else:
                    self.log(f"No job info returned for {job_url}")

                # Wait before next poll
                await asyncio.sleep(poll_interval)

            except Exception as e:
                self.log(f"Error checking job status for {job_url}: {e}", "WARNING")
                # If there are persistent authentication issues, assume job completed
                if "authentication" in str(e).lower() or "keyring" in str(e).lower():
                    self.log(
                        f"Authentication issue detected, assuming job completed: {job_url}"
                    )
                    return True, None, None
                await asyncio.sleep(poll_interval)

        # Timeout reached
        self.log(f"Job completion timeout after {timeout_seconds}s", "WARNING")
        return False, None, None

    def _create_failed_result(
        self, job_spec: ProverJobSpec, cache_key: str, error_msg: str
    ) -> ProverResult:
        """Create a failed ProverResult."""
        job_handle = JobHandle(
            job_id="failed",
            config_file=str(job_spec.config_file.path),
            config_content_hash=cache_key,
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=RunnerType.CLOUD,
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

    def _create_failed_result_with_output(
        self,
        job_spec: ProverJobSpec,
        cache_key: str,
        error_msg: str,
        output: str,
        return_code: int,
    ) -> ProverResult:
        """Create a failed ProverResult with full output for filtering check."""
        job_handle = JobHandle(
            job_id="failed",
            config_file=str(job_spec.config_file.path),
            config_content_hash=cache_key,
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=RunnerType.CLOUD,
            status=JobStatus.FAILED,
        )

        return ProverResult(
            job_handle=job_handle,
            success=False,
            report_path=None,
            output_data={
                "output": output,
                "return_code": return_code,
            },
            job_spec=job_spec,
            error_message=error_msg,
            transformed_result=None,
        )

    def _extract_error_message(self, output: str, return_code: int) -> str:
        """Extract meaningful error message from failed execution.

        Uses a tiered match so genuine errors win over warnings whose text
        happens to contain "error" (e.g. "WARNING: Could not find storage
        layout for IErrors ...").
        """
        if not output:
            return f"Cloud prover failed with return code {return_code}"

        # Tier A — explicit error prefixes.
        tier_a_prefixes = ("ERROR:", "CRITICAL:", "Error:")
        # Tier B — known-fatal phrases that always indicate a real failure.
        tier_b_phrases = (
            "does not contain a Certora key",
            "Stack too deep",
            "CompilerError",
            "Encountered an exception",
            "Traceback",
        )
        # Tier C — substring match like the old heuristic, but skip diagnostic-only lines.
        tier_c_substrings = ("error", "failed", "exception")
        tier_c_skip_prefixes = ("WARNING:", "INFO:")

        tier_a: list[str] = []
        tier_b: list[str] = []
        tier_c: list[str] = []
        for raw in output.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if line.startswith(tier_a_prefixes):
                tier_a.append(line)
                continue
            if any(phrase in line for phrase in tier_b_phrases):
                tier_b.append(line)
                continue
            if line.startswith(tier_c_skip_prefixes):
                continue
            lowered = line.lower()
            if any(needle in lowered for needle in tier_c_substrings):
                tier_c.append(line)

        for tier in (tier_a, tier_b, tier_c):
            if tier:
                return "; ".join(tier[:3])

        # Fallback to generic error with return code
        return f"Cloud prover failed with return code {return_code}"

    async def cleanup_completed_jobs(self) -> None:
        """Clean up tracking data for completed jobs."""
        # Simplified implementation - no persistent job tracking
        self.log("Cloud runner cleanup completed")
        pass

    async def cleanup_all_running_jobs(self) -> int:
        """
        Clean up all running/submitted jobs tracked in submission cache.

        Behavior controlled by self.cancel_jobs_on_cleanup:
        - If True: Cancel jobs on Certora's servers and remove submission cache for cancelled jobs
        - If False: Do nothing (jobs continue running, local tracking preserved)

        Returns:
            Number of jobs successfully cancelled (0 if cancel_jobs_on_cleanup is False)
        """
        # Find all submission cache files on the cache filesystem (S3 prefix in SaaS,
        # local in CLI) — matches where _cache_submitted_job persists them.
        fs = get_fs()
        submission_cache_pattern = f"{cache_path(DIR_CERTORA_INTERNAL, DIR_JOB_RESULT_CACHE)}/*_submission.json"
        cache_files = [str(p) for p in fs.glob(submission_cache_pattern)]

        if not cache_files:
            self.log("No running jobs found in submission cache", "DEBUG")
            return 0

        self.log(f"Found {len(cache_files)} submitted/running jobs in cache")

        cancelled_count = 0

        for cache_file_path in cache_files:
            try:
                # Read cache data
                with fs.open(cache_file_path, "r") as f:
                    cached_data = json.load(f)

                job_handle = JobHandle.from_dict(cached_data["job_handle"])
                job_url = job_handle.job_id

                self.log(f"Processing job: {job_url}")

                # Cancel the job if configured to do so
                if self.cancel_jobs_on_cleanup:
                    success = await self._cancel_cloud_job(job_url)
                    if success:
                        cancelled_count += 1
                        # Remove submission cache only if cancellation succeeded
                        fs.rm(cache_file_path)
                        self.log(f"✓ Cancelled job and removed cache: {job_url}")
                    else:
                        self.log(f"✗ Failed to cancel job: {job_url}", "WARNING")
                        self.log(f"Removing remaining cache file {cache_file_path} to avoid stale tracking", "DEBUG")
                        fs.rm(cache_file_path)

            except Exception as e:
                self.log(
                    f"Error processing cache file {cache_file_path}: {e}", "WARNING"
                )

        if self.cancel_jobs_on_cleanup:
            self.log(
                f"Cleanup complete: cancelled {cancelled_count}/{len(cache_files)} jobs"
            )
        else:
            self.log(
                "Cleanup complete: no jobs cancelled (cancel_jobs_on_cleanup=False)"
            )

        return cancelled_count

    async def _cancel_cloud_job(self, job_url: str) -> bool:
        """
        Cancel a cloud job using Certora's API.

        Args:
            job_url: The job URL to cancel

        Returns:
            True if cancellation succeeded, False otherwise
        """
        try:
            # Initialize ProverOutputAPI - call synchronously since we're already in async
            prover_api = ProverOutputAPI(use_local=False)

            # Try to cancel using the API - call directly without executor
            if hasattr(prover_api, "cancel_job"):
                # Call synchronously - the method itself is likely synchronous
                result = prover_api.cancel_job(job_url)
                # it looks like this very simple transformation to bool happens to be correct by accident:
                # when cancelling fails, the result we get is an empty list,
                # when it succeeds, it is a list with one element of the form [{'id': '<the id>', 'accepted': False}]
                # why False? Who knows. If this endpoint ever starts returning something more sane, this will break
                return bool(result)
            else:
                self.log("ProverOutputAPI does not have cancel_job method", "WARNING")
                return False

        except Exception as e:
            self.log(f"Exception while cancelling job {job_url}: {e}", "WARNING")
            return False
