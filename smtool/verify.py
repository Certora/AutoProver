"""Async prover runner + result parser — the mechanism behind smtool's "these rules hold" guarantee.

Wraps certora_autosetup's `ProverRunner` (Cloud/Local) so smtool can actually run a conformance conf
and read per-rule verdicts. POLICY (when / which confs to run) stays with the caller (the author
agent in wf1, a skill/human in wf2). The runner caches by conf content-hash, so re-running an
unchanged conf is a cache hit — selective re-verification during refinement falls out for free.

Async so a caller can `await` it and present it to an author agent.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from certora_autosetup.utils.enhanced_config_manager import ConfigManager, ProverJobSpec, FileContent
from certora_autosetup.utils.cloud_runner import CloudProverRunner
from certora_autosetup.utils.local_runner import LocalProverRunner
from certora_autosetup.utils.runner_types import ProverResult, RuleResult, JobStatus


@dataclass
class RuleVerdict:
    rule: str
    status: str                    # "VERIFIED" / "VIOLATED" / ...
    passed: bool
    method: str | None = None
    assert_message: str | None = None

    @classmethod
    def of(cls, r: RuleResult) -> "RuleVerdict":
        return cls(rule=r.rule_name, status=r.status, passed=r.passed,
                   method=r.method, assert_message=r.assert_message)


@dataclass
class VerifyResult:
    conf: str
    success: bool                  # job succeeded AND every rule passed
    job_url: str | None
    rules: list[RuleVerdict]
    error: str | None = None
    cex: dict = field(default_factory=dict)   # {label: counterexample_xml}, filled lazily on failure
    difficulty: str = ""           # ranked nonlinearity hotspots + inlined calls, filled lazily on TIMEOUT
    cancelled: bool = False        # early-terminated (not run to completion) — re-run next round, NOT a failure

    @classmethod
    def of(cls, conf: str, r: ProverResult) -> "VerifyResult":
        rules = [RuleVerdict.of(x) for x in r.rule_results]
        cancelled = getattr(r.job_handle, "status", None) == JobStatus.CANCELLED
        return cls(conf=conf, success=r.success and all(v.passed for v in rules),
                   job_url=r.job_url, rules=rules, error=r.error_message, cancelled=cancelled)

    def failures(self) -> list[RuleVerdict]:
        return [v for v in self.rules if not v.passed]


def _project_root(conf_path: Path, sources_root: str | Path | None) -> Path:
    # conf.verify/files are relative to the sources root (where certoraRun runs). Default heuristic:
    # conf at <root>/certora/conf/x.conf -> root = parents[2].
    return Path(sources_root).resolve() if sources_root else conf_path.resolve().parents[2]


def _cut_of(conf_path: Path) -> str:
    return json.loads(conf_path.read_text())["verify"].split(":", 1)[0]


def _runner(root: Path, cm: ConfigManager, certora_run_path: str, local: bool, disable_cache: bool):
    kw = dict(project_root=root, config_manager=cm, certora_run_path=certora_run_path,
              disable_cache=disable_cache)
    return LocalProverRunner(**kw) if local else CloudProverRunner(**kw)


async def verify(conf_path: str | Path, *, sources_root: str | Path | None = None,
                 certora_run_path: str = "certoraRun", local: bool = False,
                 disable_cache: bool = False, msg: str | None = None) -> VerifyResult:
    """Run ONE conformance conf; return its per-rule verdicts. Async; content-hash cached."""
    conf_path = Path(conf_path).resolve()
    root = _project_root(conf_path, sources_root)
    runner = _runner(root, ConfigManager(root), certora_run_path, local, disable_cache)
    job = ProverJobSpec(contract_name=_cut_of(conf_path), phase="conformance",
                        config_file=FileContent.from_file(conf_path),
                        msg=msg or f"smtool conformance: {conf_path.name}")
    return VerifyResult.of(str(conf_path), await runner.check_with_prover(job))


async def prune_reachable(project, reachable_conf_path: str | Path, *,
                          sources_root: str | Path | None = None, certora_run_path: str = "certoraRun",
                          local: bool = False, disable_cache: bool = False):
    """Run the reachable conf through ProverRunner and DROP every candidate invariant that did NOT
    VERIFY (timeouts/violations included), mutating `project` so its reachable/conformance artifacts
    keep only the proven set. This is the best-effort gate: only prover-VERIFIED invariants survive
    as `requireInvariant`s. Returns (kept, dropped, VerifyResult); RE-WRITE the project afterwards to
    emit the pruned artifacts. Prove-incrementally: add more invariants + call this again.
    TODO(discovery): source the candidate invariants from composer's generate->prove->cex pass."""
    res = await verify(reachable_conf_path, sources_root=sources_root, certora_run_path=certora_run_path,
                       local=local, disable_cache=disable_cache, msg="smtool reachable invariants")
    verified = {v.rule for v in res.rules if v.passed}
    candidates = set(project.reachable_invariant_names())
    kept = candidates & verified
    project.drop_invariants(candidates - verified)
    project.verified_invariants |= kept   # record the discharged survivors (H2 gate: check_consistency)
    return sorted(kept), sorted(candidates - verified), res


class _StopOnFirstFailure:
    """EarlyTerminationCallback: terminate the batch the moment a job finishes WITHOUT fully verifying.
    NB: `ProverResult.success` means the JOB RAN, not that every rule passed — a VIOLATED or TIMEOUT
    rule leaves success=True with a failing rule_result. So we must also check the rule verdicts, or
    early-stop would only fire on a job crash and never on an actual conformance failure."""
    def should_terminate(self, completed_result, all_completed_results) -> bool:
        r = completed_result
        return not (r.success and all(rr.passed for rr in r.rule_results))


async def verify_all_early(conf_paths, *, sources_root: str | Path | None = None,
                           certora_run_path: str = "certoraRun", local: bool = False,
                           disable_cache: bool = False, msg: str | None = None) -> dict[str, VerifyResult]:
    """Submit every conf CONCURRENTLY on ONE runner; the moment one finishes FAILING (violation or
    timeout), CANCEL the rest and return, instead of blocking on the slowest job (verify_all is a
    barrier — a 40-min job stalls the whole round even after another already failed). Uses the runner's
    built-in early_termination_callback (it cancels the remaining tasks). Returns {conf: VerifyResult};
    confs cancelled mid-flight come back with `cancelled=True` (re-run next round) — NOT real failures.

    SIMPLIFICATION (deliberate, for now): we cancel ALL remaining jobs on the first failure. That's
    ideal when the fix changes the SHARED MODEL (which re-runs every rule anyway), but too aggressive
    when the failure is fixable per-rule WITHOUT touching the model (e.g. a violated add_helper_lemma
    lives in one method's conformance spec) — there the still-running jobs stay valid and cancelling
    them is avoidable redo. TODO: cancel only when the refine will touch the shared model."""
    paths = [Path(c).resolve() for c in conf_paths]
    if not paths:
        return {}
    root = _project_root(paths[0], sources_root)
    runner = _runner(root, ConfigManager(root), certora_run_path, local, disable_cache)
    specs = [ProverJobSpec(contract_name=_cut_of(c), phase="conformance",
                           config_file=FileContent.from_file(c),
                           msg=msg or f"smtool conformance: {c.name}") for c in paths]
    results = await runner.submit_and_wait_for_jobs(specs, early_termination_callback=_StopOnFirstFailure())
    by_conf = {str(Path(r.job_spec.config_file.path).resolve()): r for r in results if r is not None}
    out: dict[str, VerifyResult] = {}
    for c in paths:
        r = by_conf.get(str(c))
        out[str(c)] = (VerifyResult.of(str(c), r) if r is not None else
                       VerifyResult(conf=str(c), success=False, job_url=None, rules=[],
                                    error="no result (cancelled)", cancelled=True))
    return out


async def verify_all(conf_paths, *, sources_root: str | Path | None = None,
                     certora_run_path: str = "certoraRun", local: bool = False,
                     disable_cache: bool = False, stop_on_fail: bool = False) -> dict[str, VerifyResult]:
    """Run several confs. Unchanged confs are cache hits, so this is cheap after a targeted fix.
    stop_on_fail: run sequentially and bail on the first failure (else run concurrently)."""
    paths = [Path(c).resolve() for c in conf_paths]
    common = dict(sources_root=sources_root, certora_run_path=certora_run_path,
                  local=local, disable_cache=disable_cache)
    if not stop_on_fail:
        results = await asyncio.gather(*[verify(c, **common) for c in paths])
        return {str(c): r for c, r in zip(paths, results)}
    out: dict[str, VerifyResult] = {}
    for c in paths:
        r = await verify(c, **common)
        out[str(c)] = r
        if not r.success:
            break
    return out
