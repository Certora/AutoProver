"""
Type definitions for the conf runner.
"""

from dataclasses import dataclass, field


@dataclass
class ConfRunnerConfig:
    """Configuration for the ConfRunner."""

    extra_args: list[str] = field(default_factory=list)
    max_configs: int | None = None
    require_all_submissions: bool = True
    retry_difficult: bool = False
    skip_breadcrumbs: bool = False
    skip_setup_check: bool = False
    certora_run_command: str = "certoraRun"
    verbose: int = 0
    cancel_cloud_jobs_on_cleanup: bool = True
    keep_intermediate_typechecker_files: bool = False
