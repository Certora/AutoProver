"""Centralized, extensible workarounds for failed prover jobs.

Each workaround inspects a FAILED ``ProverResult`` and, if it recognizes the failure, patches the job's
conf in place and returns ``True`` so the caller (``ProverRunner.check_with_prover``) can retry. Workarounds
MUST be idempotent: if their fix is already present in the conf they return ``False``, so a retry loop
terminates.

Adding a new workaround = add a function with the ``_Workaround`` signature and append it to ``_WORKAROUNDS``.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

import json5

from prover_output_utility.models import AlertType

from .logger import logger

if TYPE_CHECKING:  # type-only — avoids any import-order coupling with prover_runner
    from prover_output_utility import ParsedAlert, ProverOutputAPI

    from .enhanced_config_manager import ConfigManager
    from .runner_types import ProverResult

LOG_COMPONENT = "JobProblemFixes"

# Cap on workaround-and-retry passes per job, so a misbehaving workaround can't loop forever.
MAX_JOB_PROBLEM_FIXES = 3

# solc optimizer passes that inline internal functions. Disabling them keeps summarized internal functions
# locatable under via-IR (empirically confirmed on luca-money-vault) while via-IR still relieves the
# stack-too-deep that forces it on in the first place.
_DISABLE_OPTIMIZER_PASSES = ["cse", "peephole", "inliner", "deduplicate"]

_Workaround = Callable[["ProverResult", "ConfigManager", "ProverOutputAPI"], bool]


def _load_conf(conf_path: Path) -> dict:
    with conf_path.open() as f:
        return json5.load(f)


def _conf_to_patch(result: "ProverResult") -> Optional[Path]:
    """The conf whose solc settings govern this job.

    Normally the job's own conf; if it only references a base conf via ``override_base_config`` (PreAudit's
    per-checker confs do this), patch that base conf so sibling confs inherit the fix too.
    """
    try:
        conf_path = Path(result.job_spec.config_file.path)
    except Exception:
        return None
    if not conf_path.exists():
        return None
    try:
        base = _load_conf(conf_path).get("override_base_config")
    except Exception:
        return conf_path
    if base:
        base_path = Path(base)
        if not base_path.is_absolute():
            base_path = (Path.cwd() / base_path).resolve()
        if base_path.exists():
            return base_path
    return conf_path


def _fetch_alerts(result: "ProverResult", prover_api: "ProverOutputAPI") -> "List[ParsedAlert]":
    """Fetch the job's alerts from the alert-report endpoint (the single source of truth; it works on
    FAILED jobs even when tree-view data is missing). Best-effort: a fetch failure must not abort the run."""
    job_url = result.job_url
    if not job_url:
        return []
    try:
        return prover_api.get_alerts(job_url)
    except Exception as e:
        logger.log(f"Could not fetch alerts for {job_url} to diagnose job failure: {e}", "WARNING", LOG_COMPONENT)
        return []


def _fix_disable_solc_optimizers(
    result: "ProverResult", config_manager: "ConfigManager", prover_api: "ProverOutputAPI"
) -> bool:
    """The prover hard-fails when a summary targets an internal function the solc optimizer inlined (so it
    can't be located) and recommends ``--disable_solc_optimizers``. Add the inlining passes to the conf."""
    conf_path = _conf_to_patch(result)
    if conf_path is None:
        return False

    # Existing disable_solc_optimizers may already carry other passes; keep them. Coerce a lone str to a list.
    try:
        existing = _load_conf(conf_path).get("disable_solc_optimizers") or []
    except Exception:
        existing = []
    if isinstance(existing, str):
        existing = [existing]

    # Idempotent: if all our passes are already present, there's nothing to add (and re-running would loop).
    missing = [p for p in _DISABLE_OPTIMIZER_PASSES if p not in existing]
    if not missing:
        return False

    alerts = _fetch_alerts(result, prover_api)
    needs_disable = any(
        alert.alert_type == AlertType.SUMMARIZATION
        and alert.severity == "ERROR"
        and (
            "disable_solc_optimizers" in (alert.message + (alert.hint or ""))
            or "unable to locate this function" in alert.message
        )
        for alert in alerts
    )
    if not needs_disable:
        return False

    # Union: append our missing passes to whatever was already there, so we never clobber other values.
    merged = list(existing) + missing
    logger.log(
        "Prover could not locate a summarized internal function (solc optimizer inlining) — adding "
        f"disable_solc_optimizers={merged} to {conf_path.name} and retrying",
        "WARNING",
        LOG_COMPONENT,
    )
    config_manager.update_config_with_properties(conf_path, {"disable_solc_optimizers": merged})
    return True


# Registry of workarounds, tried in order. The first one that patches a conf wins (caller retries).
_WORKAROUNDS: List[_Workaround] = [_fix_disable_solc_optimizers]


def on_job_problem(
    result: "ProverResult", config_manager: "ConfigManager", prover_api: "ProverOutputAPI"
) -> bool:
    """Apply the first applicable workaround to a failed prover result, patching its conf in place.

    Returns True iff a conf was modified — the caller should then re-run the job. No-op (False) for a
    successful result or when no workaround applies.
    """
    if result.success:
        return False
    for workaround in _WORKAROUNDS:
        try:
            if workaround(result, config_manager, prover_api):
                return True
        except Exception as e:
            logger.log(f"Job-problem workaround {workaround.__name__} raised: {e}", "WARNING", LOG_COMPONENT)
    return False
