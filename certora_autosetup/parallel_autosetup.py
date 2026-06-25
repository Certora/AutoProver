#!/usr/bin/env python3
"""
Parallel multi-contract preaudit runner.

Runs preaudit in parallel across git worktrees,
one worktree per main contract. Collects diffs and reports from each run.
"""

import argparse
import asyncio
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from certora_autosetup.cache.cache_fs import init_cache_fs
from certora_autosetup.parsers.build_system_detector import BuildSystem, BuildSystemDetector
from certora_autosetup.utils.constants import (
    CERTORA_REPORTS_DIR,
    DIR_CERTORA_INTERNAL,
    DIR_WORKTREE_LOGS,
)
from certora_autosetup.utils.llm_util import LlmUsageReport, UsageRow
from certora_autosetup.utils.logger import logger

COMPONENT = "ParallelPreaudit"


@dataclass
class ContractRunResult:
    contract_id: str
    contract_name: str
    run_cwd: Path
    success: bool
    return_code: int
    log_path: Path
    diff_path: Optional[Path] = None
    reports_dir: Optional[Path] = None
    elapsed_seconds: float = 0.0


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run PreAudit in parallel for multiple main contracts using git worktrees",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  certora-parallel-autosetup --main-contracts Vault.sol:Vault Token.sol:Token -v
  certora-parallel-autosetup --main-contracts A.sol:A B.sol:B --max-parallel 2 --extra-args "--server prover"
""",
    )
    parser.add_argument(
        "--main-contracts",
        nargs="+",
        required=True,
        metavar="FILE[:CONTRACT]",
        help="Main contracts to verify in parallel (e.g., Vault.sol Token.sol:AToken)",
    )
    parser.add_argument(
        "--worktree-dir",
        type=str,
        default=".certora_internal/worktrees",
        help="Base directory for git worktrees (default: .certora_internal/worktrees)",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        help="Maximum number of parallel preaudit runs (default: number of contracts)",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Build system profile (e.g., Foundry profile). Also passed through to preaudit.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        default=False,
        help="Remove existing worktrees before creating new ones. By default, existing worktrees are reused "
        "(preserving preaudit cache for faster reruns).",
    )
    parser.add_argument(
        "--teardown",
        action="store_true",
        default=False,
        help="Remove worktrees after completion. By default, worktrees are kept for faster reruns.",
    )
    parser.add_argument(
        "--reports-dir",
        type=str,
        default=None,
        help="Directory for report output (default: .CertoraProverLiteReports/<timestamp>)",
    )
    return parser


def extract_contract_name(contract_id: str) -> str:
    """Extract contract name from an identifier like 'File.sol:ContractName' or 'File.sol'."""
    if ":" in contract_id:
        return contract_id.split(":", 1)[1]
    return Path(contract_id).stem


def get_git_root(project_root: Path) -> Path:
    """Get the git repository root directory."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=project_root
    )
    if result.returncode != 0:
        logger.error("Not inside a git repository", COMPONENT)
        sys.exit(1)
    return Path(result.stdout.strip())


def validate_git_repo(project_root: Path) -> None:
    """Validate we're in a git repo and warn about dirty state."""
    # get_git_root already validates we're in a repo
    get_git_root(project_root)

    # Warn (not fail) if dirty
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=project_root)
    if result.stdout.strip():
        logger.warning(
            "Working tree has uncommitted changes. Worktrees will be based on HEAD and won't include these changes.",
            COMPONENT,
        )


def detect_artifacts_dir(project_root: Path, profile: Optional[str]) -> Path:
    """Detect build artifacts directory using BuildSystemDetector."""
    detected = BuildSystemDetector.detect(project_root)
    if detected == BuildSystem.UNKNOWN:
        logger.error("No build system detected (no foundry.toml or hardhat.config). Cannot determine artifacts dir.", COMPONENT)
        sys.exit(1)

    class MinimalScope:
        def is_file_in_scope(self, _file_path):
            return True

    ManagerClass = BuildSystemDetector.get_manager_class(detected)
    manager = ManagerClass(project_root, MinimalScope())  # type: ignore
    config = manager.auto_detect_config(profile)
    artifacts_dir_str = config.get_artifact_directory()
    artifacts_dir = Path(artifacts_dir_str)

    if not artifacts_dir.is_absolute():
        artifacts_dir = project_root / artifacts_dir

    if not artifacts_dir.exists():
        build_cmd = manager.get_build_command(profile)
        logger.error(
            f"Artifacts directory '{artifacts_dir}' does not exist. Run '{build_cmd}' first.",
            COMPONENT,
        )
        sys.exit(1)

    logger.info(f"Detected {detected.value} artifacts at: {artifacts_dir}", COMPONENT)
    return artifacts_dir


def ensure_gitignore(project_root: Path) -> None:
    """Ensure .gitignore has entries for parallel preaudit artifacts."""
    gitignore_path = project_root / ".gitignore"
    required_lines = [
        "**/.certora_internal",
    ]

    if gitignore_path.exists():
        existing = gitignore_path.read_text().splitlines()
    else:
        existing = []

    missing = [line for line in required_lines if line not in existing]
    if missing:
        with open(gitignore_path, "a") as f:
            if existing and not gitignore_path.read_text().endswith("\n"):
                f.write("\n")
            if existing:
                f.write("\n# Certora / PreAudit\n")
            for line in missing:
                f.write(f"{line}\n")
        logger.info(f"Updated .gitignore with: {', '.join(missing)}", COMPONENT)


def get_submodule_paths(project_root: Path) -> list[Path]:
    """Get all submodule paths using git submodule status."""
    result = subprocess.run(
        ["git", "submodule", "status"], capture_output=True, text=True, cwd=project_root
    )
    if result.returncode != 0:
        return []

    paths = []
    for line in result.stdout.strip().splitlines():
        # Format: " <hash> <path> (<describe>)" or "-<hash> <path>" (uninitialized)
        parts = line.strip().lstrip("-+").split()
        if len(parts) >= 2:
            submodule_path = project_root / parts[1]
            if submodule_path.exists():
                paths.append(submodule_path)
            else:
                logger.warning(f"Submodule path does not exist: {submodule_path}", COMPONENT)
    return paths


def setup_worktree(
    git_root: Path,
    worktree_base: Path,
    contract_id: str,
    artifacts_dir: Path,
    submodule_paths: list[Path],
    subdir_offset: Path,
    fresh: bool,
) -> Path:
    """Create a git worktree for a contract and symlink dependencies.

    If fresh=False and the worktree already exists, it is reused as-is
    (preserving preaudit cache from previous runs).
    """
    contract_name = extract_contract_name(contract_id)
    worktree_path = worktree_base / f"{contract_name}_preaudit"

    if worktree_path.exists():
        if fresh:
            logger.info(f"Removing existing worktree (--fresh): {worktree_path}", COMPONENT)
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=git_root,
                capture_output=True,
            )
            if worktree_path.exists():
                shutil.rmtree(worktree_path)
        else:
            logger.info(f"Reusing existing worktree for {contract_name} at {worktree_path}", COMPONENT)
            return worktree_path

    # Create worktree (detached HEAD — no branch name needed)
    result = subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "HEAD", "--detach"],
        cwd=git_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"Failed to create worktree for {contract_name}: {result.stderr}", COMPONENT)
        sys.exit(1)

    # Symlink artifacts dir
    _symlink_dir(artifacts_dir, worktree_path / artifacts_dir.relative_to(git_root))

    # Symlink each submodule
    for submod in submodule_paths:
        rel = submod.relative_to(git_root)
        target = worktree_path / rel
        _symlink_dir(submod, target)

    # Symlink dependency directories that aren't git submodules (these live in cwd, not necessarily git root)
    run_cwd = git_root / subdir_offset
    for dep_dir_name in ["node_modules", "lib"]:
        dep_dir = run_cwd / dep_dir_name
        if dep_dir.exists():
            _symlink_dir(dep_dir, worktree_path / subdir_offset / dep_dir_name)

    logger.info(f"Created worktree for {contract_name} at {worktree_path}", COMPONENT)
    return worktree_path


def _symlink_dir(source: Path, target: Path) -> None:
    """Create a symlink from target to source, replacing any existing directory."""
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            target.unlink()
        else:
            shutil.rmtree(target)
    # Ensure parent directory exists
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(source.resolve(), target)


def _build_child_cmd(contract_id: str, passthrough_args: list[str]) -> list[str]:
    return [sys.executable, "-m", "certora_autosetup.autosetup", "--main-contract", contract_id] + passthrough_args


async def run_single_contract(
    contract_id: str,
    run_cwd: Path,
    passthrough_args: list[str],
    logs_dir: Path,
    semaphore: asyncio.Semaphore,
) -> ContractRunResult:
    """Run autosetup for a single contract in its worktree."""
    contract_name = extract_contract_name(contract_id)
    log_path = logs_dir / f"{contract_name}.log"

    cmd = _build_child_cmd(contract_id, passthrough_args)

    async with semaphore:
        logger.info(f"Starting autosetup for {contract_name} (log: {log_path})", COMPONENT)
        start = time.monotonic()

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            # To debug CI hangs, replace stdout=log_file with stdout=asyncio.subprocess.PIPE
            # and stream lines to both the log file and sys.stdout with a [contract_name] prefix.
            DEBUG_CI_HANGS = False
            with open(log_path, "w") as log_file:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=run_cwd,
                    stdout=log_file if not DEBUG_CI_HANGS else asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                if DEBUG_CI_HANGS and proc.stdout:
                    prefix = f"[{contract_name}] "
                    async for line in proc.stdout:
                        decoded = line.decode("utf-8", errors="replace")
                        log_file.write(decoded)
                        sys.stdout.write(prefix + decoded)
                        if not decoded.endswith("\n"):
                            sys.stdout.write("\n")
                        sys.stdout.flush()
                return_code = await proc.wait()
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=20)
                except asyncio.TimeoutError:
                    proc.kill()
            raise

        elapsed = time.monotonic() - start
        success = return_code == 0

        if success:
            logger.success(f"{contract_name} finished in {elapsed:.1f}s", COMPONENT)
        else:
            logger.error(f"{contract_name} failed (exit code {return_code}) after {elapsed:.1f}s. See {log_path}", COMPONENT)

        return ContractRunResult(
            contract_id=contract_id,
            contract_name=contract_name,
            run_cwd=run_cwd,
            success=success,
            return_code=return_code,
            log_path=log_path,
            elapsed_seconds=elapsed,
        )


async def run_all_contracts(
    contract_runs: dict[str, tuple[str, Path]],
    passthrough_args: list[str],
    logs_dir: Path,
    max_parallel: int,
) -> list[ContractRunResult]:
    """Run preaudit for all contracts in parallel."""
    semaphore = asyncio.Semaphore(max_parallel)
    tasks = [
        run_single_contract(cid, run_cwd, passthrough_args, logs_dir, semaphore)
        for _, (cid, run_cwd) in contract_runs.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to failed results
    final_results = []
    items = list(contract_runs.items())
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            name, (cid, err_run_cwd) = items[i]
            logger.error(f"{name} raised an exception: {result}", COMPONENT)
            final_results.append(
                ContractRunResult(
                    contract_id=cid,
                    contract_name=name,
                    run_cwd=err_run_cwd,
                    success=False,
                    return_code=-1,
                    log_path=Path("/dev/null"),
                    elapsed_seconds=0.0,
                )
            )
        else:
            final_results.append(result)
    return final_results


def collect_diffs(results: list[ContractRunResult], diffs_dir: Path) -> None:
    """Collect git diffs from each worktree."""
    diffs_dir.mkdir(parents=True, exist_ok=True)

    for result in results:
        name = result.contract_name

        # Stage all changes (including new files) to capture in diff — git finds the worktree root automatically
        subprocess.run(["git", "add", "-A"], cwd=result.run_cwd, capture_output=True)
        # Unstage symlinks so they don't appear in the diff (paths from git are relative to worktree root)
        git_root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=result.run_cwd, capture_output=True, text=True,
        )
        wt_root = Path(git_root_result.stdout.strip())
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=result.run_cwd, capture_output=True, text=True,
        )
        for staged_file in staged.stdout.strip().splitlines():
            if (wt_root / staged_file).is_symlink():
                subprocess.run(["git", "reset", "HEAD", "--", staged_file], cwd=result.run_cwd, capture_output=True)
        diff_result = subprocess.run(["git", "diff", "--cached"], cwd=result.run_cwd, capture_output=True, text=True)

        if diff_result.stdout.strip():
            diff_path = diffs_dir / f"{name}.diff"
            diff_path.write_text(diff_result.stdout)
            result.diff_path = diff_path
            logger.info(f"Saved diff for {name}: {diff_path}", COMPONENT)
        else:
            logger.warning(f"No changes detected for {name}", COMPONENT)


def collect_certora_dirs(results: list[ContractRunResult], dest_base: Path) -> None:
    """Copy the certora/ directory from each worktree to an aggregated location, namespaced by contract."""
    for result in results:
        certora_dir = result.run_cwd / "certora"
        if certora_dir.exists():
            dest = dest_base / result.contract_name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(certora_dir, dest, dirs_exist_ok=True)
            logger.info(f"Copied certora dir for {result.contract_name} to {dest}", COMPONENT)


def collect_reports(results: list[ContractRunResult], reports_dir: Path) -> None:
    """Copy only the current run's reports from each worktree to an aggregated location.

    Args:
        results: List of contract run results (each has run_cwd pointing to the worktree).
        reports_dir: The exact timestamped reports directory (e.g. .CertoraProverLiteReports/20260305_120345).
    """
    for result in results:
        wt_reports = result.run_cwd / reports_dir
        if wt_reports.exists():
            dest = reports_dir / result.contract_name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(wt_reports, dest, dirs_exist_ok=True)
            result.reports_dir = dest
            logger.info(f"Copied reports for {result.contract_name} to {dest}", COMPONENT)


def aggregate_llm_usage(results: list[ContractRunResult], dest: Path) -> None:
    """Merge each child's per-run llm_usage.json into one aggregate file.

    Each child autosetup process writes ``llm_usage.json`` into its reports dir;
    ``collect_reports`` copies that into ``<dest>/<contract>/llm_usage.json``. We
    read those copies (plain local Path I/O, matching the collect_* idiom), stamp
    each row with its contract, and roll the combined rows up into per-contract /
    per-model / per-component totals written to ``<dest>/llm_usage_aggregate.json``.
    Failed or usage-less children are skipped with a warning.

    Must run AFTER collect_reports, which performs the copy into ``<dest>/<contract>/``.
    """
    rows: list[UsageRow] = []
    for result in results:
        if not result.success:
            logger.warning(f"Skipping llm_usage for failed contract {result.contract_name}", COMPONENT)
            continue
        usage_path = dest / result.contract_name / "llm_usage.json"
        if not usage_path.exists():
            logger.warning(f"No llm_usage.json for {result.contract_name} at {usage_path}", COMPONENT)
            continue
        try:
            report = LlmUsageReport.from_dict(json.loads(usage_path.read_text()))
        except Exception as e:
            logger.warning(f"Could not read llm_usage.json for {result.contract_name}: {e}", COMPONENT)
            continue
        for row in report.llm_usage:
            row.contract = result.contract_name
            rows.append(row)

    dest.mkdir(parents=True, exist_ok=True)
    out_path = dest / "llm_usage_aggregate.json"
    out_path.write_text(json.dumps(LlmUsageReport.from_rows(rows).to_dict(), indent=2))
    logger.info(
        f"Aggregated LLM usage for {len(rows)} calls across {len(results)} contracts to {out_path}",
        COMPONENT,
    )


def cleanup_worktrees(project_root: Path, worktree_base: Path, worktree_paths: list[Path]) -> None:
    """Remove all worktrees and prune."""
    for wt_path in worktree_paths:
        try:
            subprocess.run(
                ["git", "worktree", "remove", str(wt_path), "--force"],
                cwd=project_root,
                capture_output=True,
            )
        except Exception:
            pass
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

    subprocess.run(["git", "worktree", "prune"], cwd=project_root, capture_output=True)

    # Remove worktree base if empty
    if worktree_base.exists() and not any(worktree_base.iterdir()):
        worktree_base.rmdir()


def print_summary(results: list[ContractRunResult]) -> None:
    """Print a summary table of all results."""
    print()
    print("=" * 80)
    print("PARALLEL PREAUDIT SUMMARY")
    print("=" * 80)
    print(f"{'Contract':<30} {'Status':<10} {'Time':<12} {'Log'}")
    print("-" * 80)
    for r in results:
        status = "SUCCESS" if r.success else "FAILED"
        minutes = int(r.elapsed_seconds // 60)
        seconds = int(r.elapsed_seconds % 60)
        time_str = f"{minutes}m {seconds:02d}s"
        print(f"{r.contract_name:<30} {status:<10} {time_str:<12} {r.log_path}")
    print("-" * 80)

    succeeded = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    print(f"Total: {len(results)} contracts — {succeeded} succeeded, {failed} failed")

    # List diffs
    diffs = [r for r in results if r.diff_path]
    if diffs:
        print("\nDiff files:")
        for r in diffs:
            print(f"  {r.diff_path}")
    print()


def main():
    parser = create_parser()
    args, passthrough_args = parser.parse_known_args()

    init_cache_fs()

    # If --profile was specified, also pass it through to preaudit
    if args.profile and "--profile" not in passthrough_args:
        passthrough_args.extend(["--profile", args.profile])

    # Determine the reports directory for this run
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.reports_dir:
        reports_dir = Path(args.reports_dir)
    else:
        reports_dir = Path(CERTORA_REPORTS_DIR) / run_timestamp

    # Pass through to child preaudit runs so they write into the same timestamped dir
    if "--reports-dir" not in passthrough_args:
        passthrough_args.extend(["--reports-dir", str(reports_dir)])

    project_root = Path.cwd()

    # Validate environment
    validate_git_repo(project_root)

    # Determine git root and the subdirectory offset (project_root may be a subdir of the git repo)
    git_root = get_git_root(project_root)
    subdir_offset = project_root.resolve().relative_to(git_root.resolve())

    ensure_gitignore(git_root)

    # Detect artifacts directory
    artifacts_dir = detect_artifacts_dir(project_root, args.profile)

    # Check for duplicate contract names
    names = [extract_contract_name(s) for s in args.main_contracts]
    if len(names) != len(set(names)):
        dupes = [n for n in names if names.count(n) > 1]
        logger.error(f"Duplicate contract names: {set(dupes)}. Use unique contract names.", COMPONENT)
        sys.exit(1)

    # Parse submodule paths
    submodule_paths = get_submodule_paths(git_root)

    worktree_base = project_root / args.worktree_dir
    worktree_base.mkdir(parents=True, exist_ok=True)

    logs_dir = project_root / DIR_CERTORA_INTERNAL / DIR_WORKTREE_LOGS / run_timestamp
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Create worktrees sequentially (git operations touch shared .git)
    contract_runs: dict[str, tuple[str, Path]] = {}
    worktree_paths: list[Path] = []
    for contract_id in args.main_contracts:
        name = extract_contract_name(contract_id)
        wt_path = setup_worktree(git_root, worktree_base, contract_id, artifacts_dir, submodule_paths, subdir_offset, args.fresh)
        worktree_paths.append(wt_path)
        contract_runs[name] = (contract_id, wt_path / subdir_offset)

    max_parallel = args.max_parallel or len(args.main_contracts)

    # Initialized before the try so the finally block never hits a NameError if
    # the run is interrupted before asyncio.run() returns.
    results: list[ContractRunResult] = []
    try:
        # Run preaudit in parallel
        results = asyncio.run(
            run_all_contracts(contract_runs, passthrough_args, logs_dir, max_parallel)
        )
    except KeyboardInterrupt:
        logger.warning("Interrupted by user, terminating child processes...", COMPONENT)
        sys.exit(130)
    finally:
        # Collect outputs from worktrees
        certora_dir = project_root / "certora"
        collect_certora_dirs(results, certora_dir)
        collect_diffs(results, certora_dir / "diffs")
        collect_reports(results, reports_dir)
        aggregate_llm_usage(results, reports_dir)

        if args.teardown:
            cleanup_worktrees(git_root, worktree_base, worktree_paths)

    print_summary(results)

    failed = [r for r in results if not r.success]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
