"""Pre-compilation fixes for certora-fixconf.

These run before the compilation workaround loop to fix conf issues that
would fail before compilation even starts (malformed prover_args, wrong
file paths, mismatched contract names, solc naming convention).
"""

import difflib
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Dict

from certora_autosetup.setup.solidity_utils import find_all_solidity_files
from certora_autosetup.utils.constants import SolcConvention
from certora_autosetup.utils.contract_utils import parse_contract_files
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.prover_arg_names import VALID_PROVER_ARG_NAMES
from certora_autosetup.utils.solc_version_resolver import extract_pragma_spec, resolve_pragma_to_version


def fix_prover_args(conf: Dict[str, Any]) -> int:
    """Fix malformed prover_args entries in the conf.

    Applies three sub-fixes to each arg string in order:
    1. Add missing '-' prefix: destructiveOptimizations twostage -> -destructiveOptimizations twostage
    2. Replace '=' separator with space: -splitParallel=true -> -splitParallel true
    3. Fix typos via fuzzy match against known prover arg names

    Returns the number of args that were modified.
    """
    if "prover_args" not in conf or not isinstance(conf["prover_args"], list):
        return 0

    fixes = 0
    fixed_args = []

    for arg in conf["prover_args"]:
        if not isinstance(arg, str):
            fixed_args.append(arg)
            continue

        original = arg

        # Sub-fix 1: Add missing '-' prefix
        stripped = arg.lstrip()
        if stripped and not stripped.startswith("-"):
            arg = "-" + stripped

        # Sub-fix 2: Replace '=' separator with space after flag name
        arg = re.sub(r"^(-\w+)=", r"\1 ", arg)

        # Sub-fix 3: Fix typos in flag name via fuzzy matching
        match = re.match(r"^-(\w+)(.*)", arg, re.DOTALL)
        if match:
            flag_name = match.group(1)
            rest = match.group(2)
            if flag_name not in VALID_PROVER_ARG_NAMES:
                candidates = difflib.get_close_matches(flag_name, VALID_PROVER_ARG_NAMES, n=1, cutoff=0.8)
                if candidates:
                    arg = f"-{candidates[0]}{rest}"
                    logger.log(
                        f"  prover_args: fixed typo '{flag_name}' -> '{candidates[0]}'", "INFO", "fixconf"
                    )

        if arg != original:
            fixes += 1
            logger.log(f"  prover_args: '{original}' -> '{arg}'", "INFO", "fixconf")

        fixed_args.append(arg)

    conf["prover_args"] = fixed_args
    return fixes


def fix_file_paths(conf: Dict[str, Any], project_root: Path) -> int:
    """Fix broken file paths in the conf's 'files' list.

    For each file that doesn't exist at the specified path, searches for
    a file with the same basename in the project, preferring the path
    closest to the root.

    Returns the number of paths that were fixed.
    """
    if "files" not in conf or not isinstance(conf["files"], list):
        return 0

    sol_index: dict[str, list[Path]] | None = None
    fixes = 0
    fixed_files = []

    for entry in conf["files"]:
        if ":" in entry:
            file_part, contract_suffix = entry.split(":", 1)
        else:
            file_part, contract_suffix = entry, None

        if (project_root / file_part).exists():
            fixed_files.append(entry)
            continue

        # Lazy-build the search index on first miss
        if sol_index is None:
            sol_index = _build_sol_index()

        basename = Path(file_part).name
        candidates = sol_index.get(basename, [])
        if not candidates:
            logger.log(f"  files: '{file_part}' not found and no candidate with basename '{basename}'", "WARNING", "fixconf")
            fixed_files.append(entry)
            continue

        # Pick shortest path (closest to root)
        best = min(candidates, key=lambda p: len(str(p)))
        new_path = str(best)
        new_entry = f"{new_path}:{contract_suffix}" if contract_suffix else new_path
        logger.log(f"  files: '{file_part}' -> '{new_path}'", "INFO", "fixconf")
        fixed_files.append(new_entry)
        fixes += 1

    conf["files"] = fixed_files
    return fixes


def fix_verify_contract(conf: Dict[str, Any], project_root: Path) -> int:
    """Fix the contract name in 'verify' if it doesn't match any file handle.

    Uses fuzzy matching to find the closest contract name from the files list.
    Also updates 'parametric_contracts' if it references the old name.

    Returns the number of fixes (0 or 1).
    """
    if "verify" not in conf or ":" not in conf["verify"]:
        return 0

    contract_name, spec_path = conf["verify"].split(":", 1)

    # Build contract name set from files
    handles = parse_contract_files(conf.get("files", []), project_root=project_root, strict=False)
    handle_names = [h.contract_name for h in handles]

    if contract_name in handle_names:
        return 0

    candidates = difflib.get_close_matches(contract_name, handle_names, n=1, cutoff=0.6)
    if not candidates:
        logger.log(
            f"  verify: contract '{contract_name}' not in files and no close match found", "WARNING", "fixconf"
        )
        return 0

    new_name = candidates[0]
    conf["verify"] = f"{new_name}:{spec_path}"
    logger.log(f"  verify: contract '{contract_name}' -> '{new_name}'", "INFO", "fixconf")

    # Update parametric_contracts if it references the old name
    if "parametric_contracts" in conf:
        pc = conf["parametric_contracts"]
        if isinstance(pc, str) and pc == contract_name:
            conf["parametric_contracts"] = new_name
        elif isinstance(pc, list):
            conf["parametric_contracts"] = [new_name if x == contract_name else x for x in pc]

    return 1


def detect_solc_convention(project_root: Path) -> SolcConvention:
    """Detect whether the machine uses Certora-style (solc8.34) or solc-select-style (solc-0.8.34) binaries.

    Scans .sol files for pragma versions, resolves the most common one, then checks
    which binary naming convention exists on PATH.
    """
    # Collect pragma versions from source files
    versions: list[str] = []
    for sol_path_str in find_all_solidity_files():
        sol_file = Path(sol_path_str)
        try:
            content = sol_file.read_text(errors="ignore")
        except OSError:
            continue
        pragma_spec = extract_pragma_spec(content)
        if pragma_spec:
            version = resolve_pragma_to_version(pragma_spec)
            if version:
                versions.append(version)

    if not versions:
        return SolcConvention.CERTORA

    # Pick the most common version
    primary_version = Counter(versions).most_common(1)[0][0]

    # Check which naming convention exists on PATH
    parts = primary_version.split(".")
    if len(parts) == 3:
        major_minor = f"{parts[1]}.{parts[2]}"  # e.g., "8.34"
        certora_name = f"solc{major_minor}"  # solc8.34
        solc_select_name = f"solc-{primary_version}"  # solc-0.8.34

        has_certora = shutil.which(certora_name) is not None
        has_solc_select = shutil.which(solc_select_name) is not None

        if has_solc_select and not has_certora:
            logger.log(
                f"Detected solc-select naming convention (found '{solc_select_name}' on PATH)", "INFO", "fixconf"
            )
            return SolcConvention.SOLC_SELECT

    return SolcConvention.CERTORA


def _build_sol_index() -> dict[str, list[Path]]:
    """Build an index of basename -> [relative_paths] for all .sol files in the project."""
    index: dict[str, list[Path]] = {}
    for sol_path_str in find_all_solidity_files():
        p = Path(sol_path_str)
        index.setdefault(p.name, []).append(p)
    return index
