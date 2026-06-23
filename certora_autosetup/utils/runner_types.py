#!/usr/bin/env python3
"""
Runner types for PreAudit - Copied from AutoSetup and adapted.

Core types for prover execution and job tracking.
"""


from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from certora_autosetup.utils.enhanced_config_manager import ProverJobSpec

# Import types from ProverOutputUtility to avoid duplication
from prover_output_utility import AlertType, AssertType, ParsedAlert


class NotificationType(Enum):
    """Types of notifications that can appear in prover results."""

    # Rule execution notifications
    ONLY_REVERTING_PATHS = "only_reverting_paths"
    EXPANDED_TOO_MANY_COMMANDS = "expanded_too_many_commands"
    LOOP_UNROLLING_LIMIT = "loop_unrolling_limit"
    MEMORY_COMPLEXITY = "memory_complexity"

    # Catch-all
    OTHER = "other"


class RunnerType(Enum):
    """Type of prover runner."""

    LOCAL = "local"
    CLOUD = "cloud"


class JobStatus(Enum):
    """Status of a cloud job."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SubmissionResult:
    """Result from job submission attempt."""
    
    job_url: Optional[str]  # None if submission failed
    output: str  # Full stdout/stderr from certoraRun
    return_code: int  # Process return code
    error_message: Optional[str] = None  # Extracted error message if failed
    
    @property
    def success(self) -> bool:
        """True if submission was successful (has job_url)."""
        return self.job_url is not None


@dataclass
class JobHandle:
    """Handle for tracking cloud jobs with content-based resume support."""

    job_id: str
    config_file: str
    config_content_hash: str  # Hash of config + dependencies for resume logic
    phase: str
    submitted_at: float
    runner_type: RunnerType  # Track whether this was a local or cloud run
    status: JobStatus = JobStatus.SUBMITTED
    job_url: Optional[str] = None  # URL for cloud job tracking

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "job_id": self.job_id,
            "config_file": self.config_file,
            "config_content_hash": self.config_content_hash,
            "phase": self.phase,
            "submitted_at": self.submitted_at,
            "status": self.status.value,
            "runner_type": self.runner_type.value,
            "job_url": self.job_url,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobHandle":
        """Create from dictionary."""
        return cls(
            job_id=data["job_id"],
            config_file=data["config_file"],
            config_content_hash=data["config_content_hash"],
            phase=data["phase"],
            submitted_at=data["submitted_at"],
            status=JobStatus(data["status"]),
            runner_type=RunnerType(data["runner_type"]),
            job_url=data.get("job_url"),  # Use get() for backward compatibility
        )


@dataclass
class RuleResult:
    """Result for an individual rule verification."""

    rule_name: str
    status: str
    contract: str
    method: Optional[str] = None
    sub_rule: Optional[str] = None
    assert_message: Optional[str] = None
    duration: Optional[float] = None  # duration in seconds
    is_sanity_rule: bool = False
    is_leaf: bool = True
    level: int = 0
    notifications: List[NotificationType] = field(default_factory=list)  # All rule notifications/warnings
    assert_type: Optional[AssertType] = None  # Type of assertion that was violated (for SAT results)

    @property
    def passed(self) -> bool:
        """Whether this rule passed verification."""
        return self.status in ["VERIFIED", "SUCCESS"]

    @property
    def failed(self) -> bool:
        """Whether this rule failed verification."""
        return not self.passed


@dataclass
class ProverResult:
    """Result from prover execution."""

    job_handle: JobHandle
    success: bool
    report_path: Optional[str]
    output_data: Dict[str, Any]
    job_spec: Any  # ProverJobSpec - using Any to avoid circular import
    error_message: Optional[str] = None
    duration: Optional[float] = None
    rule_results: List[RuleResult] = field(default_factory=list)
    alerts: List[ParsedAlert] = field(default_factory=list)
    transformed_result: Optional[Any] = None  # Generic transformed result

    @property
    def job_url(self) -> Optional[str]:
        """Get job URL from job_handle or output data."""
        # First check job_handle.job_url (preferred)
        if self.job_handle.job_url:
            return self.job_handle.job_url

        # Fallback to output_data for backward compatibility
        if "job_url" in self.output_data:
            return self.output_data["job_url"]

        # For cloud jobs, construct URL from job_id if available
        if (self.job_handle.runner_type == RunnerType.CLOUD and
            self.job_handle.job_id and
            self.job_handle.job_id.startswith("http")):
            return self.job_handle.job_id

        return None

    def is_skipped(self) -> bool:
        """
        Determine if this ProverResult should be considered skipped due to filtering.

        Logic:
        - "no valid instantiations" in output: skipped (filtering case)

        Returns:
            True if this should be considered skipped, False otherwise
        """
        output = self.output_data.get("output", "")
        return "remains with no valid instantiations" in output

    def is_failure(self) -> bool:
        """
        Determine if this ProverResult should be considered a failure.

        Logic:
        - return_code == 0: success
        - skipped results (no valid instantiations): not failure
        - return_code != 0: failure

        Returns:
            True if this should be considered a failure, False otherwise
        """
        return_code = self.output_data.get("return_code", 0)

        # Return code 0 is always success
        if return_code == 0:
            return False

        # Check if result is skipped (filtering case) - not a failure
        if self.is_skipped():
            return False

        # All other non-zero return codes are failures
        return True

    def is_success(self) -> bool:
        """
        Determine if this ProverResult should be considered a success.

        Logic:
        - Not skipped and not a failure

        Returns:
            True if this should be considered a success, False otherwise
        """
        return not self.is_skipped() and not self.is_failure()

    @classmethod
    def create_cancelled_result(
        cls,
        job_spec: "ProverJobSpec[Any]",
        job_url: Optional[str] = None,
        runner_type: RunnerType = RunnerType.CLOUD
    ) -> "ProverResult":
        """Create a ProverResult for a cancelled job."""
        import time
        from .job_utils import extract_job_id_from_url

        job_id = extract_job_id_from_url(job_url) if job_url else "cancelled"

        job_handle = JobHandle(
            job_id=job_id,
            config_file=str(job_spec.config_file.path),
            config_content_hash=job_spec.config_file.content_hash,
            phase=job_spec.phase,
            submitted_at=time.time(),
            runner_type=runner_type,
            job_url=job_url,
            status=JobStatus.CANCELLED
        )

        return cls(
            job_handle=job_handle,
            success=False,
            report_path=None,
            output_data={},
            job_spec=job_spec,
            error_message="Job cancelled by early termination",
            duration=None,
            rule_results=[],
            alerts=[],
            transformed_result=None
        )