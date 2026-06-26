#!/usr/bin/env python3
"""
ProverRunner - Abstract base class for Certora prover execution.

Provides unified interface for both local and cloud prover execution with
automatic result caching and resume support.
"""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
)

from prover_output_utility import AssertType, ParsedAlert, ProverOutputAPI
from prover_output_utility.models import CallResolutionInfo

from certora_autosetup.cache.cache_fs import cache_path, get_fs
from .constants import DIR_CERTORA_INTERNAL, DIR_JOB_RESULT_CACHE
from .enhanced_config_manager import ConfigManager, FileContent, ProverJobSpec
from .file_utils import atomic_write_json_fsspec
from .job_problem_fixes import MAX_JOB_PROBLEM_FIXES, on_job_problem
from .logger import logger
from .runner_types import (
    JobHandle,
    NotificationType,
    ProverResult,
    RuleResult,
    RunnerType,
    SubmissionResult,
)


# Generic types for job context data and transformed results
ContextT = TypeVar("ContextT", covariant=True)
TransformedResultT = TypeVar("TransformedResultT", covariant=True)

# Type alias for result transformer function
ResultTransformer = Callable[
    ["ProverResult"], TransformedResultT
]


class EarlyTerminationCallback(Protocol, Generic[ContextT, TransformedResultT]):
    """Protocol for early termination callbacks in parallel job execution."""

    def should_terminate(
        self,
        completed_result: ProverResult,
        all_completed_results: List[ProverResult],
    ) -> bool:
        """
        Check if early termination should occur based on a completed job.

        Args:
            completed_result: The prover result that just completed
            all_completed_results: All completed prover results (including this one)

        Returns:
            bool: True to cancel all remaining jobs and terminate early, False to continue
        """
        ...


class ProverRunner(ABC):
    """
    Abstract base class for Certora prover execution.

    Provides unified interface for both local (`certoraRun.py <conf>`) and
    cloud (`certoraRun.py <conf> --server production`) execution with
    automatic caching and resume support.
    """

    def __init__(
        self,
        project_root: Path,
        config_manager: ConfigManager,
        use_local_api: bool = False,
    ):
        """
        Initialize prover runner.

        Args:
            project_root: Root directory of the project
            config_manager: Configuration manager for dependency tracking
            use_local_api: Whether to use local API for emv-* folders
        """
        self.project_root = project_root
        self.config_manager = config_manager
        self.use_local_api = use_local_api
        self.component = "ProverRunner"

        # Cache for job results — addressed through the cache filesystem (local FS
        # in CLI, the S3 prefix in SaaS) so prover results persisted on one run are
        # reused on the next. The dir is created lazily on first write via get_fs().
        self.prover_api = ProverOutputAPI(use_local=use_local_api)

    def _cache_file(self, cache_key: str, *, submission: bool = False) -> str:
        """fsspec cache path for a job's result (or submission) record.

        Single source of truth for all reads/writes/deletes below, so the store
        lands on the SaaS cache prefix instead of the container's local disk."""
        suffix = "_submission" if submission else ""
        return cache_path(DIR_CERTORA_INTERNAL, DIR_JOB_RESULT_CACHE, f"{cache_key}{suffix}.json")

    def log(self, message: str, level: str = "INFO"):
        """Log message using centralized logger."""
        logger.log(message, level, self.component)

    async def check_with_prover(self, job_spec: ProverJobSpec) -> ProverResult:
        """Run a single prover job, applying job-problem workarounds and retrying.

        The per-runner submission/caching logic lives in ``_check_with_prover_impl``; this wrapper adds a
        central recovery layer: on failure, ``on_job_problem`` may patch the conf and signal a retry. Each
        workaround is idempotent, so the loop terminates once none applies (bounded by
        ``MAX_JOB_PROBLEM_FIXES``). A conf patched here propagates to later phases that derive from the base
        conf (warmup copy, ConfRunner ``override_base_config``).
        """
        result = await self._check_with_prover_impl(job_spec)

        attempts = 0
        while (
            not result.success
            and attempts < MAX_JOB_PROBLEM_FIXES
            and on_job_problem(result, self.config_manager, self.prover_api)
        ):
            attempts += 1
            # on_job_problem patched the conf on disk; re-hash it so the cache key reflects
            # the new content and we submit a fresh job instead of resuming the failed one.
            job_spec.config_file = FileContent.from_file(job_spec.config_file.path)
            result = await self._check_with_prover_impl(job_spec)

        return result

    @abstractmethod
    async def _check_with_prover_impl(self, job_spec: ProverJobSpec) -> ProverResult:
        """
        Execute prover with automatic resume support and caching.

        Handles:
        1. Content-based cache checking using existing config_manager functionality
        2. Resume logic for existing jobs based on config content hash
        3. New job submission if needed
        4. Result retrieval and caching

        Args:
            job_spec: Complete job specification

        Returns:
            ProverResult with job status and results
        """
        pass

    @abstractmethod
    async def get_runner_type(self) -> RunnerType:
        """Get the type of this runner."""
        pass

    @abstractmethod
    async def cleanup_completed_jobs(self) -> None:
        """Clean up tracking data for completed jobs."""
        pass

    @abstractmethod
    async def submit_jobs(
        self, job_specs: List[ProverJobSpec], pre_execute_callback=None
    ) -> List[SubmissionResult]:
        """
        Submit multiple jobs and return submission results.
        Fast operation - does not wait for completion.

        Args:
            job_specs: List of job specifications to submit
            pre_execute_callback: Optional callback before job execution

        Returns:
            List of SubmissionResult objects
        """
        pass

    @abstractmethod
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
        Submit multiple jobs and wait for all to complete, with optional result transformation.
        Complete two-phase parallel execution with internal logging.

        Args:
            job_specs: List of job specifications to execute
            completion_callback: Optional callback for job completion
            pre_execute_callback: Optional callback before job execution
            early_termination_callback: Optional callback for early termination
            result_transformer: Optional function to transform results
            use_queue: If True, use queue-based processing that allows completion_callback
                       to add new jobs dynamically via job_queue.put_nowait()

        Returns:
            List of ProverResult objects (with transformed_result field populated if transformer provided)
        """
        pass

    async def submit_and_wait_for_jobs(
        self,
        job_specs: List[ProverJobSpec],
        completion_callback=None,
        pre_execute_callback=None,
        early_termination_callback: Optional[EarlyTerminationCallback] = None,
        use_queue: bool = False,
    ) -> List[ProverResult]:
        """
        Submit multiple jobs and wait for all to complete.
        Complete two-phase parallel execution with internal logging.

        This method now just calls submit_and_wait_for_jobs_with_transformer
        since ProverResult now includes transformation support.

        Args:
            job_specs: List of job specifications to submit and wait for
            early_termination_callback: Optional callback to determine if early termination should occur
            use_queue: If True, use queue-based processing that allows completion_callback
                       to add new jobs dynamically via job_queue.put_nowait()

        Returns:
            List of ProverResult objects (cancelled jobs will have success=False and status=CANCELLED)
        """
        return await self.submit_and_wait_for_jobs_with_transformer(
            job_specs=job_specs,
            completion_callback=completion_callback,
            pre_execute_callback=pre_execute_callback,
            early_termination_callback=early_termination_callback,
            result_transformer=None,
            use_queue=use_queue,
        )

    async def cleanup_all_running_jobs(self) -> int:
        """
        Cancel all running jobs to save cloud costs.

        Returns:
            Number of jobs that were cancelled
        """
        # For now, just return 0 - actual implementation would cancel jobs
        # This is a compatibility method for orchestrator integration
        return 0

    def track_running_job(
        self, job_handle, split_key: str, is_cached: bool = False
    ) -> None:
        """
        Track a running job for management purposes.

        Args:
            job_handle: Handle to the running job
            split_key: Unique key for this job split
            is_cached: Whether this job result was cached
        """
        # Compatibility method for orchestrator integration
        # Actual implementation would track jobs for monitoring/cancellation
        pass

    def track_job_failure(self, split_key: str, error_msg: str) -> None:
        """
        Track a job failure for reporting purposes.

        Args:
            split_key: Unique key for this job split
            error_msg: Error message describing the failure
        """
        # Compatibility method for orchestrator integration
        # Actual implementation would log/store failure information
        pass

    # Common utility methods for all runners

    def _get_cache_key(self, job_spec: ProverJobSpec) -> str:
        """Get cache key for job specification."""
        return job_spec.get_cache_key(self.config_manager)

    async def _check_cache(self, cache_key: str, job_spec: ProverJobSpec) -> Optional[ProverResult]:
        """Check if we have a cached result for this cache key."""
        fs = get_fs()
        cache_file = self._cache_file(cache_key)

        if not fs.exists(cache_file):
            return None

        try:
            with fs.open(cache_file, "r") as f:
                cached_data = json.load(f)

            # Reconstruct ProverResult from cached data
            job_handle = JobHandle.from_dict(cached_data["job_handle"])

            # Check cache compatibility based on runner type
            current_runner_type = (
                RunnerType.LOCAL if self.use_local_api else RunnerType.CLOUD
            )
            if job_handle.runner_type != current_runner_type:
                self.log(
                    f"Skipping incompatible cached result ({job_handle.runner_type.value} vs {current_runner_type.value}): {cache_key[:16]}",
                    "INFO",
                )
                return None

            # Check if emv folder still exists (for local runner results)
            if (
                job_handle.runner_type == RunnerType.LOCAL
                and job_handle.job_id
                and not Path(job_handle.job_id).exists()
            ):
                self.log(
                    f"Invalidating cached result - emv folder no longer exists: {job_handle.job_id}",
                    "DEBUG",
                )
                return None

            # Reconstruct rule_results from cached data
            rule_results = []
            if "rule_results" in cached_data:
                for rule_data in cached_data["rule_results"]:
                    # Convert string values back to Enums
                    if "notifications" in rule_data:
                        rule_data["notifications"] = [
                            NotificationType(n) for n in rule_data["notifications"]
                        ]
                    if "assert_type" in rule_data and rule_data["assert_type"] is not None:
                        rule_data["assert_type"] = AssertType(rule_data["assert_type"])
                    rule_results.append(RuleResult(**rule_data))

            # Convert cached alert dicts back to ParsedAlert objects
            alerts = [
                ParsedAlert.from_dict(alert_data)
                for alert_data in cached_data.get("alerts", [])
            ]

            return ProverResult(
                job_handle=job_handle,
                success=cached_data["success"],
                report_path=cached_data.get("report_path"),
                output_data=cached_data.get("output_data", {}),
                job_spec=job_spec,
                error_message=cached_data.get("error_message"),
                duration=cached_data.get("duration"),
                rule_results=rule_results,
                alerts=alerts,
                transformed_result=None,
            )

        except Exception as e:
            self.log(
                f"Failed to load cached result for {cache_key[:16]}: {e}", "WARNING"
            )
            return None

    async def _cache_result(self, cache_key: str, result: ProverResult) -> None:
        """Cache successful result for future use."""
        cache_file = self._cache_file(cache_key)

        import time

        # Convert output_data to JSON-serializable format
        output_data = result.output_data.copy() if result.output_data else {}
        if "unresolved_calls" in output_data and output_data["unresolved_calls"]:
            # Convert CallResolutionInfo objects to dictionaries
            serializable_calls = []
            for call in output_data["unresolved_calls"]:
                if hasattr(call, "to_dict"):
                    # CallResolutionInfo object - convert to dict
                    serializable_calls.append(call.to_dict())
                else:
                    # Already a dictionary
                    serializable_calls.append(call)
            output_data["unresolved_calls"] = serializable_calls

        def serialize_value(v):
            """Convert Enums to their values, recursively handling lists."""
            if isinstance(v, Enum):
                return v.value
            elif isinstance(v, list):
                return [serialize_value(item) for item in v]
            return v

        def enum_dict_factory(data):
            return {k: serialize_value(v) for k, v in data}

        cache_data = {
            "job_handle": result.job_handle.to_dict(),
            "success": result.success,
            "report_path": result.report_path,
            "output_data": output_data,
            "error_message": result.error_message,
            "duration": result.duration,
            "rule_results": [asdict(rule, dict_factory=enum_dict_factory) for rule in result.rule_results],
            "alerts": [alert.to_dict() for alert in result.alerts],
            "cached_at": time.time(),
        }

        try:
            atomic_write_json_fsspec(cache_file, cache_data)
            self.log(f"Cached result for cache_key {cache_key[:16]}", "DEBUG")
        except Exception as e:
            self.log(f"Failed to cache result: {e}", "WARNING")

    async def _cache_submitted_job(self, cache_key: str, job_handle: JobHandle) -> None:
        """Cache submitted job for resume functionality."""
        submission_cache_file = self._cache_file(cache_key, submission=True)

        import time

        cache_data = {
            "job_handle": job_handle.to_dict(),
            "status": job_handle.status.value,
            "submitted_at": time.time(),
            "cache_key": cache_key,
        }

        try:
            atomic_write_json_fsspec(submission_cache_file, cache_data)
            self.log(f"Cached submitted job for cache_key {cache_key[:16]}", "DEBUG")
        except Exception as e:
            self.log(f"Failed to cache submitted job: {e}", "WARNING")

    async def _check_submission_cache(self, cache_key: str) -> Optional[JobHandle]:
        """Check if we have a cached submitted job for this cache key."""
        fs = get_fs()
        submission_cache_file = self._cache_file(cache_key, submission=True)

        if not fs.exists(submission_cache_file):
            return None

        try:
            with fs.open(submission_cache_file, "r") as f:
                cached_data = json.load(f)

            # Reconstruct JobHandle from cached data
            job_handle = JobHandle.from_dict(cached_data["job_handle"])

            self.log(f"Found cached submitted job: {job_handle.job_id}", "INFO")
            return job_handle

        except Exception as e:
            self.log(
                f"Failed to load cached submitted job for {cache_key[:16]}: {e}",
                "WARNING",
            )
            return None

    async def _update_job_status_in_cache(
        self, cache_key: str, job_handle: JobHandle
    ) -> None:
        """Update job status in submission cache."""
        fs = get_fs()
        submission_cache_file = self._cache_file(cache_key, submission=True)

        if not fs.exists(submission_cache_file):
            return

        try:
            import time

            with fs.open(submission_cache_file, "r") as f:
                cache_data = json.load(f)

            # Update status and job_handle
            cache_data["job_handle"] = job_handle.to_dict()
            cache_data["status"] = job_handle.status.value
            cache_data["updated_at"] = time.time()

            atomic_write_json_fsspec(submission_cache_file, cache_data)
        except Exception as e:
            self.log(f"Failed to update job status in cache: {e}", "WARNING")

    async def _remove_submission_cache(self, cache_key: str) -> None:
        """Remove submission cache after job completion."""
        fs = get_fs()
        submission_cache_file = self._cache_file(cache_key, submission=True)
        try:
            if fs.exists(submission_cache_file):
                fs.rm(submission_cache_file)
                self.log(f"Removed submission cache for {cache_key[:16]}", "DEBUG")
        except Exception as e:
            self.log(f"Failed to remove submission cache: {e}", "WARNING")

    def parse_rule_results_from_job(self, job_identifier: str) -> List[RuleResult]:
        """
        Parse rule results from a prover job using ProverOutputUtility.

        Args:
            job_identifier: Cloud job ID or local emv-* folder path to parse results from

        Returns:
            List of RuleResult objects converted from prover checks
        """
        if not job_identifier:
            self.log("No job identifier", "WARNING")
            return []

        try:
            all_checks = self.prover_api.get_all_checks(job_identifier)

            # Convert checks to RuleResult objects
            rule_results = []
            sanity_rule_count = 0
            for check_result in all_checks:
                # Parse notifications from check result (convert strings to NotificationType objects)
                notifications = self._check_notifications(
                    [{"message": msg} for msg in check_result.notifications]
                )

                rule_name = check_result.rule_name
                assert_message = check_result.assert_message
                method_name = check_result.method_name

                # Sanity rules are identified by:
                # rule_name == "sanity" AND assert_message contains "Satisfy_Reaching_end_of_methods_code_"
                is_sanity_rule = (
                    rule_name == "sanity"
                    and "Satisfy_Reaching_end_of_methods_code_" in str(assert_message)
                )

                if is_sanity_rule:
                    sanity_rule_count += 1

                # Extract additional fields
                duration = check_result.duration
                assert_type = check_result.assert_type

                rule_result = RuleResult(
                    rule_name=rule_name,
                    status=check_result.status.value
                    if hasattr(check_result.status, "value")
                    else str(check_result.status),
                    contract=check_result.contract_name or "Unknown",
                    method=method_name,
                    assert_message=assert_message,
                    is_sanity_rule=is_sanity_rule,
                    notifications=notifications,
                    duration=duration,
                    assert_type=assert_type,
                )
                rule_results.append(rule_result)

            self.log(
                f"Parsed {len(rule_results)} rule results from {len(all_checks)} checks in job {job_identifier}",
                "INFO",
            )

            return rule_results

        except Exception as e:
            self.log(
                f"Failed to parse results for job {job_identifier}: {e}", "WARNING"
            )
            return []

    def extract_unresolved_calls(self, job_identifier: str) -> List[CallResolutionInfo]:
        """
        Extract unresolved calls from a prover job using ProverOutputUtility.

        Args:
            job_identifier: Cloud job ID or local emv-* folder path

        Returns:
            List of unresolved calls
        """
        try:
            # Get all call resolutions from the API
            all_call_resolutions = self.prover_api.get_call_resolutions(job_identifier)

            # Filter for only warning calls (is_warning=True means the call needs attention)
            unresolved_calls = [c for c in all_call_resolutions if c.is_warning]

            self.log(
                f"Extracted {len(unresolved_calls)} unresolved calls from job {job_identifier}",
                "DEBUG",
            )
            return unresolved_calls

        except Exception as e:
            self.log(
                f"Failed to extract unresolved calls from job {job_identifier}: {e}",
                "WARNING",
            )
            return []

    # Patterns for matching notification messages to NotificationType
    _NOTIFICATION_PATTERNS: Dict[NotificationType, re.Pattern[str]] = {
        NotificationType.ONLY_REVERTING_PATHS: re.compile(r"only reverting paths", re.IGNORECASE),
        NotificationType.EXPANDED_TOO_MANY_COMMANDS: re.compile(
            r"expanded to too many commands:\s*\d+\s*>\s*\d+"
        ),
        NotificationType.LOOP_UNROLLING_LIMIT: re.compile(
            r"loop\s+unrolling\s+limit|unrolling\s+bound", re.IGNORECASE
        ),
        NotificationType.MEMORY_COMPLEXITY: re.compile(
            r"memory\s+complexity|memory\s+usage", re.IGNORECASE
        ),
    }

    def _check_notifications(
        self, notifications: List[Dict[str, Any]]
    ) -> List[NotificationType]:
        """
        Check notifications for known notification types.

        Args:
            notifications: List of notification dictionaries

        Returns:
            List of NotificationType enums found in notifications
        """
        found_notifications = []

        for notification in notifications:
            if isinstance(notification, dict):
                message_content = notification.get("message", "")
                message = str(message_content) if message_content else ""

                for notification_type, pattern in self._NOTIFICATION_PATTERNS.items():
                    if pattern.search(message):
                        found_notifications.append(notification_type)
                        break  # Only match one type per notification

        return found_notifications

    @staticmethod
    def get_cache_status(project_root: Path) -> Dict[str, Any]:
        """
        Get cache status for the project.

        Args:
            project_root: Root directory of the project

        Returns:
            Dictionary with cache statistics and information
        """
        # Cache lives on the cache filesystem (S3 prefix in SaaS, project_root locally).
        fs = get_fs()
        cache_dir = cache_path(DIR_CERTORA_INTERNAL, DIR_JOB_RESULT_CACHE)

        if not fs.exists(cache_dir):
            return {
                "cache_exists": False,
                "cache_dir": str(cache_dir),
                "total_entries": 0,
                "total_size_bytes": 0,
                "entries": [],
            }

        try:
            entries = []
            total_size = 0

            for cache_file_obj in fs.glob(f"{cache_dir}/*.json"):
                cache_file = str(cache_file_obj)
                cache_name = cache_file.rsplit("/", 1)[-1]
                if cache_name == "active_jobs.json":
                    continue  # Skip active jobs tracking file

                try:
                    file_size = int(fs.size(cache_file) or 0)
                    total_size += file_size

                    with fs.open(cache_file, "r") as f:
                        cache_data = json.load(f)

                    # Extract information from cached result
                    job_handle = cache_data.get("job_handle", {})
                    contract_name = "Unknown"
                    phase = job_handle.get("phase", "Unknown")

                    # Try to extract contract name from config file path
                    config_file = job_handle.get("config_file", "")
                    if config_file:
                        config_path = Path(config_file)
                        if config_path.exists():
                            try:
                                with config_path.open("r") as cf:
                                    config_data_inner = json.load(cf)
                                verify = config_data_inner.get("verify", "")
                                if ":" in verify:
                                    contract_name = verify.split(":", 1)[0]
                            except:
                                pass

                    entry = {
                        "cache_key": cache_name[:-len(".json")],
                        "contract_name": contract_name,
                        "phase": phase,
                        "success": cache_data.get("success", False),
                        "runner_type": job_handle.get("runner_type", "unknown"),
                        "cached_at": cache_data.get("cached_at"),
                        "file_size_bytes": file_size,
                        "config_file": config_file,
                    }
                    entries.append(entry)

                except Exception as e:
                    # Skip corrupted cache files
                    logger.warning(f"Skipping corrupted cache file {cache_file}: {e}")
                    continue

            return {
                "cache_exists": True,
                "cache_dir": str(cache_dir),
                "total_entries": len(entries),
                "total_size_bytes": total_size,
                "entries": sorted(
                    entries, key=lambda x: x.get("cached_at") or 0, reverse=True
                ),
            }

        except Exception as e:
            logger.error(f"Failed to get cache status: {e}")
            return {
                "cache_exists": True,
                "cache_dir": str(cache_dir),
                "total_entries": 0,
                "total_size_bytes": 0,
                "entries": [],
                "error": str(e),
            }

    @staticmethod
    def clear_cache(project_root: Path) -> Dict[str, Any]:
        """
        Clear all cached results for the project.

        Args:
            project_root: Root directory of the project

        Returns:
            Dictionary with information about the clearing operation
        """
        fs = get_fs()
        cache_dir = cache_path(DIR_CERTORA_INTERNAL, DIR_JOB_RESULT_CACHE)

        if not fs.exists(cache_dir):
            return {
                "cache_existed": False,
                "files_removed": 0,
                "bytes_freed": 0,
                "message": "No cache directory found",
            }

        try:
            files_removed = 0
            bytes_freed = 0

            # Remove all .json cache files (but preserve directory structure)
            for cache_file_obj in fs.glob(f"{cache_dir}/*.json"):
                cache_file = str(cache_file_obj)
                try:
                    file_size = int(fs.size(cache_file) or 0)
                    fs.rm(cache_file)
                    files_removed += 1
                    bytes_freed += file_size
                except Exception as e:
                    logger.warning(f"Failed to remove cache file {cache_file}: {e}")

            return {
                "cache_existed": True,
                "files_removed": files_removed,
                "bytes_freed": bytes_freed,
                "message": f"Cleared {files_removed} cache entries, freed {bytes_freed} bytes",
            }

        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            return {
                "cache_existed": True,
                "files_removed": 0,
                "bytes_freed": 0,
                "error": str(e),
                "message": f"Failed to clear cache: {e}",
            }
