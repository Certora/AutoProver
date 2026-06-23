#!/usr/bin/env python3
"""
certoraRunAutoSolc - Wrapper around certoraRun that automatically resolves the solc version
from the pragma directives in all source files listed in the conf.

Reads all .sol files from the conf's "files" array, extracts their pragma constraints,
and finds the highest solc version satisfying all of them.

Usage:
    python3 path/to/certoraRunAutoSolc.py <conf_file> [extra certoraRun args...]
    certora-run-auto-solc <conf_file> [--certora-run-command CMD] [extra certoraRun args...]

Example:
    certora-run-auto-solc certora/confs/myconf.conf --server production --wait_for_results none
    certora-run-auto-solc certora/confs/myconf.conf --certora-run-command certoraRun_beta
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Allow direct invocation: python3 path/to/certoraRunAutoSolc.py from any working directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging.version import Version

from certora_autosetup.utils.contract_utils import parse_contract_files
from certora_autosetup.parsers.prover_config_parser import parse_prover_config
from certora_autosetup.utils.enhanced_config_manager import ConfigManager
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.solc_version_resolver import (
    extract_pragma_spec,
    fetch_available_solc_versions,
    parse_pragma_constraint,
)


def resolve_solc_for_conf(conf_path: Path) -> str:
    """
    Resolve the solc version for a conf file by reading all source files' pragmas.

    Finds the highest solc version that satisfies all pragma constraints across
    every .sol file in the conf's "files" array.

    Returns the solc version in Certora format (e.g., "solc8.26").
    """
    config = parse_prover_config(conf_path)
    if not config:
        print(f"Error: Could not parse conf file: {conf_path}", file=sys.stderr)
        sys.exit(1)

    files = config.get("files", [])
    if not files:
        print(f"Error: No 'files' field in conf file: {conf_path}", file=sys.stderr)
        sys.exit(1)

    project_root = Path.cwd()
    handles = parse_contract_files(files, project_root=project_root, strict=False)

    # Collect pragma constraints from all source files
    constraints = []
    for handle in handles:
        source = project_root / handle.source_file
        if not source.exists():
            print(f"Warning: Source file not found: {source}", file=sys.stderr)
            continue
        source_text = source.read_text()
        pragma_spec = extract_pragma_spec(source_text)
        if not pragma_spec:
            continue
        constraint = parse_pragma_constraint(pragma_spec)
        if constraint is not None:
            logger.log(f"{handle.source_file}: pragma solidity {pragma_spec}", "INFO", "AutoSolc")
            constraints.append((handle.source_file, pragma_spec, constraint))

    if not constraints:
        print("Error: No pragma solidity found in any source file", file=sys.stderr)
        sys.exit(1)

    # Find highest version satisfying all constraints
    available_versions = fetch_available_solc_versions()
    matching = [
        v for v in available_versions if all(Version(v) in constraint for _, _, constraint in constraints)
    ]

    if not matching:
        print("Error: No solc version satisfies all pragma constraints:", file=sys.stderr)
        for source_file, pragma_spec, _ in constraints:
            print(f"  {source_file}: {pragma_spec}", file=sys.stderr)
        sys.exit(1)

    highest = max(matching, key=Version)
    certora_solc = ConfigManager.convert_solc_version_to_certora_format(highest)
    logger.log(f"Resolved solc version: {highest} -> {certora_solc}", "INFO", "AutoSolc")
    return certora_solc


def main():
    parser = argparse.ArgumentParser(
        description="Wrapper around certoraRun that auto-resolves the solc version from source file pragmas.",
        usage="certora-run-auto-solc <conf_file> [--certora-run-command CMD] [extra certoraRun args...]",
    )
    parser.add_argument("conf_file", help="Path to the Certora conf file")
    parser.add_argument(
        "--certora-run-command", default="certoraRun", help="certoraRun command to use (default: certoraRun)"
    )

    args, extra_args = parser.parse_known_args()

    conf_path = Path(args.conf_file)
    if not conf_path.exists():
        print(f"Error: Conf file not found: {conf_path}", file=sys.stderr)
        sys.exit(1)

    # If the conf already specifies a versioned solc (solcX.YY format), skip resolution
    config = parse_prover_config(conf_path)
    existing_solc = config.get("solc", "") if config else ""
    if re.match(r"^solc\d\.\d+$", existing_solc):
        logger.log(f"Conf already specifies solc: {existing_solc}, skipping resolution", "INFO", "AutoSolc")
        cmd = [args.certora_run_command, str(conf_path), *extra_args]
    else:
        solc_version = resolve_solc_for_conf(conf_path)
        cmd = [args.certora_run_command, str(conf_path), "--solc", solc_version, *extra_args]

    logger.log(f"Running: {' '.join(cmd)}", "INFO", "AutoSolc")

    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
