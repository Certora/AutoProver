import json
import os
import re
from pathlib import Path
from shutil import copyfile
from certora_autosetup.utils.progress_display import make_tqdm
from certora_autosetup.setup.solidity_utils import DEPENDENCIES, find_all_solidity_files
from certora_autosetup.utils.logger import logger as _logger

PATCH_FILE = ".certora_internal/import_patch.json"
KNOWN_IMPORT_ROOTS = ["contracts", "src"] + DEPENDENCIES

IMPORT_RE = re.compile(r'^\s*import\s+(?:\{[^}]+\}\s+from\s+)?["\'](.+?)["\']\s*;')

def extract_imports_multiline(lines):
    """
    Generator yielding (start_line, end_line, original_import_path) for each import statement.
    Handles multi-line structured imports like:
        import {
            A,
            B
        } from "./path.sol";
    """
    pattern = re.compile(r'^\s*import\b')
    path_pattern = re.compile(r'["\'](.+?)["\']')

    i = 0
    while i < len(lines):
        if not pattern.match(lines[i]):
            i += 1
            continue

        # Accumulate lines until semicolon
        start = i
        stmt = lines[i]
        i += 1
        while ";" not in stmt and i < len(lines):
            stmt += lines[i]
            i += 1

        # Extract import path from the full statement
        match = path_pattern.search(stmt)
        if match:
            yield start, i - 1, match.group(1)


def relative_to_project(abs_path, project_root):
    abs_path = Path(abs_path).resolve()
    project_root = Path(project_root).resolve()
    try:
        return str(abs_path.relative_to(project_root)).replace("\\", "/")
    except ValueError:
        raise Exception(f"{abs_path} is outside project root.")

def build_import_map(project_dir, all_sol_files):
    abs_imports = {}
    seen_targets = set()

    _logger.log("Building import map...")
    for file_path in make_tqdm(all_sol_files, desc="Analyzing imports", unit="file", disable=_logger.muted):
        with open(file_path, "r") as f:
            lines = f.readlines()

        for start, end, original_import in extract_imports_multiline(lines):
            if not original_import.startswith("."):
                continue

            abs_target = (file_path.parent / original_import).resolve()
            abs_target_str = str(abs_target)

            if abs_target_str in seen_targets:
                continue
            seen_targets.add(abs_target_str)

            if not abs_target.is_file():
                _logger.log(f"Skipping: '{original_import}' in {file_path} → file does not exist", "WARNING")
                continue

            # Try to create a canonical path within project, fallback to absolute path
            try:
                canonical_path = relative_to_project(abs_target, project_dir)
            except Exception:
                # Fallback to unique identifier using a prefix
                canonical_path = f"external/{abs_target.name}"

            abs_imports[abs_target_str] = canonical_path

    return abs_imports


def make_absolute(import_path, current_file):
    if not import_path.startswith("."):
        return import_path  # already absolute

    current_dir = Path(current_file).parent
    abs_path = (current_dir / import_path).resolve()

    for root in KNOWN_IMPORT_ROOTS:
        try:
            root_idx = abs_path.parts.index(root)
            return "/".join(abs_path.parts[root_idx:])
        except ValueError:
            continue

    raise Exception(f"Cannot determine absolute import for: {import_path} in {current_file}")

def create_patch(project_dir):
    project_dir = Path(project_dir).resolve()
    _logger.log("Scanning for Solidity files...")
    # Change to project directory for find_all_solidity_files to work correctly
    original_cwd = os.getcwd()
    os.chdir(project_dir)
    try:
        all_sol_files = [Path(f) for f in find_all_solidity_files(
            include_test_files=False,
            include_dependencies=True,
            include_certora=False,
            verbose=False,
            log_func=lambda msg, level="INFO": None  # Suppress logs, we print our own
        )]
    finally:
        os.chdir(original_cwd)
    _logger.log(f"Found {len(all_sol_files)} Solidity files")

    import_map = build_import_map(project_dir, all_sol_files)

    patch_data = []

    for file_path in make_tqdm(all_sol_files, desc="Processing files", unit="file", disable=_logger.muted):
        with open(file_path, "r") as f:
            lines = f.readlines()

        changes = []
        for i, line in enumerate(lines):
            match = IMPORT_RE.match(line)
            if not match:
                continue

            original_import = match.group(1)
            if original_import.startswith("."):
                abs_target = (file_path.parent / original_import).resolve()
                abs_target_str = str(abs_target)

                if abs_target_str not in import_map:
                    _logger.log(f"Skipping unresolved import: {original_import} in {file_path}", "WARNING")
                    continue

                canonical_import = import_map[abs_target_str]
                if original_import.startswith("."):
                    changes.append({
                        "line": i,
                        "original": original_import,
                        "updated": canonical_import
                    })

        if changes:
            patch_data.append({
                "file": str(file_path),
                "changes": changes
            })

    # Ensure the directory exists
    Path(PATCH_FILE).parent.mkdir(parents=True, exist_ok=True)

    with open(PATCH_FILE, "w") as f:
        json.dump(patch_data, f, indent=2)
    _logger.log(f"✓ Patch created with {len(patch_data)} files modified: {PATCH_FILE}")


def apply_patch():
    if not os.path.exists(PATCH_FILE):
        raise Exception("Patch file not found.")

    with open(PATCH_FILE) as f:
        patch_data = json.load(f)

    _logger.log(f"Applying patch to {len(patch_data)} files...")
    backups = {}

    try:
        for entry in make_tqdm(patch_data, desc="Applying patches", unit="file", disable=_logger.muted):
            file_path = entry["file"]
            try:
                with open(file_path, "r") as f:
                    lines = f.readlines()

                original_lines = lines[:]

                # Apply all changes to this file at once
                for change in entry["changes"]:
                    i = change["line"]
                    # Direct replacement is faster than regex for exact strings
                    lines[i] = lines[i].replace(change["original"], change["updated"])

                backups[file_path] = original_lines
                with open(file_path, "w") as f:
                    f.writelines(lines)
            except FileNotFoundError:
                _logger.log(f"File not found during patch application: {file_path}", "WARNING")
                continue
    except Exception as e:
        _logger.log(f"Error: {e}. Reverting all changes...", "ERROR")
        for file_path, original in make_tqdm(backups.items(), desc="Reverting", unit="file", disable=_logger.muted):
            with open(file_path, "w") as f:
                f.writelines(original)
        raise
    _logger.log("✓ Patch applied successfully.")

def revert_patch():
    if not os.path.exists(PATCH_FILE):
        raise Exception("Patch file not found.")

    with open(PATCH_FILE) as f:
        patch_data = json.load(f)

    _logger.log(f"Reverting patch from {len(patch_data)} files...")

    for entry in make_tqdm(patch_data, desc="Reverting patches", unit="file", disable=_logger.muted):
        file_path = entry["file"]
        try:
            with open(file_path, "r") as f:
                lines = f.readlines()

            # Apply all reversions to this file at once
            for change in entry["changes"]:
                i = change["line"]
                # Direct replacement is faster than regex for exact strings
                lines[i] = lines[i].replace(change["updated"], change["original"])

            with open(file_path, "w") as f:
                f.writelines(lines)
        except FileNotFoundError:
            _logger.log(f"File not found during reversion: {file_path}", "WARNING")
            continue
    _logger.log("✓ Patch reverted successfully.")
