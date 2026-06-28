#!/usr/bin/env python3
"""
Shared utilities for working with Solidity files.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from certora_autosetup.utils.logger import logger as _logger
from certora_autosetup.utils.types import ContractHandle

DEPENDENCIES = [
    "node_modules",
    "lib",
    "forge-std",
    "dependencies",
    ".git",
]
# Compiled regex patterns for efficient path filtering
_DEPENDENCY_PATTERN = re.compile(rf'/?(?:{"|".join(DEPENDENCIES)})/')
_CERTORA_PATTERN = re.compile(r'/?certora/')
_TEST_PATTERN = re.compile(r'/?(?:tests?|scripts?)/')


def walk_files_by_suffix(base_dir: Path, suffix: str) -> List[Path]:
    """Recursively collect files under ``base_dir`` whose name ends with ``suffix``.

    Uses ``os.walk`` with in-place pruning of hidden directories — the same
    tree-iteration approach as ``find_all_solidity_files`` (rather than ``Path.rglob``).
    Returns sorted absolute paths; an empty list if ``base_dir`` doesn't exist.

    TODO: ``find_all_solidity_files`` predates this helper and reimplements the same
    walk with extra project-specific filtering; it could be refactored to build on this.
    """
    if not base_dir.exists():
        return []
    found: List[Path] = []
    for root, dirs, files in os.walk(base_dir):
        # Prune hidden directories during traversal (don't descend into them).
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(suffix):
                found.append(Path(root) / name)
    return sorted(found)


def find_all_solidity_files(
    include_test_files: bool = False,
    include_dependencies: bool = False,
    include_certora: bool = False,
    verbose: bool = False,
    log_func=None
) -> List[str]:
    """Find all Solidity files in the current project.

    Args:
        include_test_files: Whether to include test (.t.sol) and script (.s.sol) files
        include_dependencies: Whether to include files in dependency directories
        include_certora: Whether to include files in certora directories
        verbose: Whether to log verbose output
        log_func: Optional logging function to use (defaults to print)

    Returns:
        List of Solidity file paths
    """
    if log_func is None:
        log_func = lambda msg, level="INFO": _logger.log(msg, level)

    solidity_files = []

    # Common directories to search for Solidity files
    search_dirs = [
        Path.cwd(),
        Path.cwd() / "contracts",
        Path.cwd() / "src"
    ]

    # Find all .sol files, excluding system directories from traversal
    for search_dir in search_dirs:
        if search_dir.exists():
            all_sol_files = []

            # Walk through directory and exclude hidden directories from traversal
            for root, dirs, files in os.walk(search_dir):
                # Remove hidden directories and certora dir from dirs list to prevent os.walk from entering them
                dirs[:] = [d for d in dirs if not d.startswith('.') and (include_certora or d != 'certora')]

                # Add .sol files from this directory
                for file in files:
                    if file.endswith('.sol'):
                        # Store relative path from current working directory
                        abs_path = Path(root) / file
                        try:
                            rel_path = abs_path.relative_to(Path.cwd())
                            all_sol_files.append(rel_path)
                        except ValueError:
                            # If file is outside cwd, store absolute path
                            all_sol_files.append(abs_path)

            # Filter files based on settings
            filtered_files = []

            for sol_file in all_sol_files:
                path_str = str(sol_file)
                file_name = sol_file.name

                # Skip files in dependency directories unless explicitly included
                if not include_dependencies:
                    if _DEPENDENCY_PATTERN.search(path_str):
                        if verbose:
                            log_func(f"Skipping dependency file: {sol_file}", "DEBUG")
                        continue

                # Skip files in certora directories unless explicitly included
                if not include_certora:
                    if _CERTORA_PATTERN.search(path_str):
                        if verbose:
                            log_func(f"Skipping certora file: {sol_file}", "DEBUG")
                        continue

                # Skip test and script files unless explicitly included
                if not include_test_files:
                    if file_name.endswith('.t.sol') or file_name.endswith('.s.sol'):
                        if verbose:
                            log_func(f"Skipping test/script file: {sol_file}", "DEBUG")
                        continue
                    if _TEST_PATTERN.search(path_str):
                        if verbose:
                            log_func(f"Skipping test directory file: {sol_file}", "DEBUG")
                        continue

                filtered_files.append(str(sol_file))

            solidity_files.extend(filtered_files)

    # Remove duplicates
    solidity_files = list(set(solidity_files))

    # Build descriptive message
    search_info = []
    if include_dependencies:
        search_info.append("dependencies: yes")
    else:
        search_info.append("dependencies: no")

    if include_test_files:
        search_info.append("scripts/tests: yes")
    else:
        search_info.append("scripts/tests: no")

    log_func(f"Found {len(solidity_files)} Solidity file(s) in the project ({', '.join(search_info)})")

    # List files based on verbosity level
    if verbose and len(solidity_files) <= 10:
        for f in solidity_files:
            log_func(f"  - {f}")

    return solidity_files


def extract_definitions_from_solidity(
    file_path: str,
    definition_type: Optional[str] = None
) -> List[str]:
    """Extract contract and/or library definitions from a Solidity file.

    This function safely parses Solidity source code to extract top-level
    contract and library definitions, properly handling:
    - Single-line comments (//)
    - Multi-line comments (/* */)
    - Nested definitions (only top-level definitions are extracted)

    Args:
        file_path: Path to the Solidity file
        definition_type: Type of definitions to extract:
            - 'contract': Only extract contracts
            - 'library': Only extract libraries
            - None: Extract both contracts and libraries (default)

    Returns:
        List of contract/library names found at the top level of the file

    Raises:
        FileNotFoundError: If the file doesn't exist
        ValueError: If definition_type is invalid
    """
    if definition_type not in (None, 'contract', 'library'):
        raise ValueError(f"Invalid definition_type: {definition_type}. Must be 'contract', 'library', or None")

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Solidity file not found: {file_path}")

    # Step 1: Remove single-line comments
    # Match // followed by anything until end of line
    content = re.sub(r'//.*?$', '', content, flags=re.MULTILINE)

    # Step 2: Remove multi-line comments
    # Match /* followed by anything (including newlines) until */
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)

    # Step 3: Track brace depth and extract top-level definitions
    definitions = []
    brace_depth = 0

    # Build pattern based on definition_type
    if definition_type == 'contract':
        pattern = r'\bcontract\s+([A-Za-z0-9_]+)'
    elif definition_type == 'library':
        pattern = r'\blibrary\s+([A-Za-z0-9_]+)'
    else:
        # Match both contract and library
        pattern = r'\b(contract|library)\s+([A-Za-z0-9_]+)'

    # Process the content character by character to track brace depth
    i = 0
    while i < len(content):
        char = content[i]

        # Track braces
        if char == '{':
            brace_depth += 1
        elif char == '}':
            brace_depth -= 1

        # Only look for definitions at depth 0
        if brace_depth == 0:
            # Try to match pattern starting at current position
            match = re.match(pattern, content[i:])
            if match:
                # Extract the name (group 2 for combined pattern, group 1 for single type)
                if definition_type is None:
                    # Combined pattern: group 1 is keyword, group 2 is name
                    name = match.group(2)
                else:
                    # Single type pattern: group 1 is name
                    name = match.group(1)

                definitions.append(name)
                # Skip past the match
                i += match.end()
                continue

        i += 1

    return definitions


def find_all_library_files(
    include_test_files: bool = False,
    include_dependencies: bool = False,
    include_certora: bool = False,
    verbose: bool = False,
    log_func=None
) -> List[str]:
    """Find all Solidity library files in the current project.

    A library file is identified by containing 'library' keyword in its content.

    Args:
        include_test_files: Whether to include test (.t.sol) and script (.s.sol) files
        include_dependencies: Whether to include files in dependency directories
        include_certora: Whether to include files in certora directories
        verbose: Whether to log verbose output
        log_func: Optional logging function to use (defaults to print)

    Returns:
        List of Solidity library file paths
    """
    return list(
        find_all_library_files_and_names(
            include_test_files,
            include_dependencies,
            include_certora,
            verbose,
            log_func
        ).keys()
    )

def find_all_library_files_and_names(
    include_test_files: bool = False,
    include_dependencies: bool = False,
    include_certora: bool = False,
    verbose: bool = False,
    log_func=None
) -> dict[str, list[str]]:
    """Find all Solidity library files in the current project and the names of the libraries in them.

    A library file is identified by containing 'library' keyword in its content.

    Args:
        include_test_files: Whether to include test (.t.sol) and script (.s.sol) files
        include_dependencies: Whether to include files in dependency directories
        include_certora: Whether to include files in certora directories
        verbose: Whether to log verbose output
        log_func: Optional logging function to use (defaults to print)

    Returns:
        Dict of Solidity library file paths to the names of the libraries in them
    """
    if log_func is None:
        log_func = lambda msg, level="INFO": _logger.log(msg, level)

    # Get all Solidity files
    all_files = find_all_solidity_files(
        include_test_files=include_test_files,
        include_dependencies=include_dependencies,
        include_certora=include_certora,
        verbose=verbose,
        log_func=log_func
    )

    library_files = {}
    for file_path in all_files:
        try:
            # Use safe extraction that handles comments and nested definitions
            libraries = extract_definitions_from_solidity(file_path, definition_type='library')
            if libraries:
                library_files[file_path] = libraries
        except Exception as e:
            if verbose:
                log_func(f"Error reading {file_path}: {e}", "WARNING")

    log_func(f"Found {len(library_files)} library file(s)")
    if verbose and library_files:
        for f in library_files:
            log_func(f"  - {f}")

    return library_files


def build_library_name_index(library_files: Dict[str, List[str]]) -> Dict[str, str]:
    """Invert ``library_files`` (file→names) into a ``name→file`` map, deduping by
    first-definition-wins.

    Duplicate names typically come from hand-vendored libraries pulled into multiple
    dependency trees (different OZ versions, etc.) — large projects can easily produce
    hundreds of these. To keep the log readable, all duplicates are summarised into a
    single ``WARNING`` line listing affected library names; the full per-name keep/ignore
    breakdown goes to ``DEBUG``. Pass ``--verbose`` (or equivalent) to see the detail.
    """
    library_name_to_file: Dict[str, str] = {}
    duplicates: Dict[str, List[str]] = {}  # name → list of ignored file paths
    for file_path, names in library_files.items():
        for name in names:
            existing = library_name_to_file.setdefault(name, file_path)
            if existing != file_path:
                duplicates.setdefault(name, []).append(file_path)
                _logger.log(
                    f"Library '{name}' defined in multiple files: keeping '{existing}', "
                    f"ignoring '{file_path}'.",
                    "DEBUG",
                )

    if duplicates:
        names = sorted(duplicates)
        _logger.log(
            f"Found {len(duplicates)} library name(s) defined in multiple files "
            f"(first-definition-wins applied; run with --verbose for per-file detail): "
            f"{', '.join(names)}",
            "WARNING",
        )
    return library_name_to_file


def find_libraries_used_by(
    contract_name: str,
    library_name_to_file: Dict[str, str],
    methods: Iterable[Dict[str, Any]],
) -> List[ContractHandle]:
    """Return ContractHandles for libraries whose methods appear in ``contract_name``'s compilation unit.

    A library counts as "used by" ``contract_name`` if at least one method in
    ``methods`` has ``originatingContract == contract_name`` and ``contractName ==
    {library_name}``. Use this to add only the libraries the contract actually
    calls to the prover scene, instead of dumping every library file in the project.

    Args:
        contract_name: Compilation unit to scan (the deployable that owns the artifact).
        library_name_to_file: Library-name → defining-file map, as produced by
            ``build_library_name_index``. Build once per run and reuse — this function
            does not warn on duplicates.
        methods: All known methods (typically ``MethodParser.get_all_methods()``).

    Returns:
        Sorted list of ContractHandles, one per used library. Empty if no library
        methods appear in ``contract_name``'s compilation unit.
    """
    used: set[str] = set()
    for method in methods:
        if method.get("originatingContract") != contract_name:
            continue
        ref = method.get("contractName")
        if ref in library_name_to_file:
            used.add(ref)

    return [
        ContractHandle(contract_name=name, source_file=library_name_to_file[name])
        for name in sorted(used)
    ]
