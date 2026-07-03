"""Utilities for creating IV-specific copies of summary spec files with NONDET entries commented out."""
import re
import shutil
from pathlib import Path
from typing import List

from certora_autosetup.parsers.spec_imports import parse_imports_from_spec
from certora_autosetup.utils.logger import logger


IV_PREFIX = "iv_"
COMMENT_PREFIX = "// IV-DISABLED: "


def create_iv_summary_copies(summary_spec: Path) -> Path:
    """Create IV-specific copies of a summaries spec and all its transitive imports.

    Each copy gets an iv_ prefix on the filename. Within the copies:
    - Import statements are updated to reference iv_-prefixed filenames
    - NONDET summary entries are commented out

    Args:
        summary_spec: Path to the root summaries spec (e.g., ContractName_base_summaries.spec)

    Returns:
        Path to the iv_-prefixed root spec (e.g., iv_ContractName_base_summaries.spec)
    """
    all_imports = parse_imports_from_spec(summary_spec, recursive=True)
    all_specs = [summary_spec.resolve()] + [p.resolve() for p in all_imports]

    # Build a mapping from original resolved path to iv_ copy path
    path_mapping: dict[Path, Path] = {}
    for spec_path in all_specs:
        iv_name = IV_PREFIX + spec_path.name
        iv_path = spec_path.parent / iv_name
        path_mapping[spec_path] = iv_path

    # Copy each file and process it
    for original, iv_copy in path_mapping.items():
        shutil.copy2(original, iv_copy)
        _process_iv_copy(iv_copy, path_mapping)
        logger.info(f"Created IV summary copy: {iv_copy.name}")

    return path_mapping[summary_spec.resolve()]


def _process_iv_copy(iv_path: Path, path_mapping: dict[Path, Path]) -> None:
    """Process an iv_ copy: update imports and comment out NONDET entries."""
    content = iv_path.read_text()

    # Update import statements to reference iv_ prefixed files
    content = _update_imports(content, iv_path.parent, path_mapping)

    # Comment out NONDET entries in methods blocks
    content = _comment_out_nondet_entries(content)

    iv_path.write_text(content)


def _update_imports(content: str, spec_dir: Path, path_mapping: dict[Path, Path]) -> str:
    """Update import statements to reference iv_-prefixed filenames."""
    def replace_import(match: re.Match) -> str:
        import_path_str = match.group(1)
        resolved = (spec_dir / import_path_str).resolve()
        if resolved in path_mapping:
            iv_target = path_mapping[resolved]
            # Compute the new relative path
            new_rel = Path(import_path_str).parent / (IV_PREFIX + Path(import_path_str).name)
            return f'import "{new_rel}";'
        return match.group(0)

    return re.sub(r'import\s+"([^"]+)"\s*;', replace_import, content)


def _comment_out_nondet_entries(content: str) -> str:
    """Comment out NONDET summary entries in methods blocks.

    A NONDET entry is a sequence of lines inside a methods { } block that:
    - Starts with a line whose stripped content begins with 'function'
    - Ends with a line containing 'NONDET;'
    - Has no ';' on any line between the start and end (to distinguish from non-NONDET entries
      that end with a different summary kind)
    - May span multiple lines
    """
    lines = content.split("\n")
    result_lines: List[str] = []
    i = 0
    in_methods_block = False
    brace_depth = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track methods block boundaries
        if not in_methods_block:
            if stripped.startswith("methods") and "{" in stripped:
                in_methods_block = True
                brace_depth = stripped.count("{") - stripped.count("}")
                result_lines.append(line)
                i += 1
                continue
            result_lines.append(line)
            i += 1
            continue

        # Inside methods block - track brace depth
        brace_depth += stripped.count("{") - stripped.count("}")
        if brace_depth <= 0:
            in_methods_block = False
            result_lines.append(line)
            i += 1
            continue

        # Check if this line starts a function entry
        if stripped.startswith("function"):
            # Collect lines of this entry until we find a semicolon
            entry_lines = [line]
            found_semicolon = False
            j = i

            current_stripped = ""
            while j < len(lines):
                current_stripped = lines[j].strip()
                if j > i:
                    entry_lines.append(lines[j])

                if ";" in current_stripped:
                    found_semicolon = True
                    break
                j += 1

            if found_semicolon and current_stripped.rstrip().endswith("NONDET;"):
                for entry_line in entry_lines:
                    result_lines.append(COMMENT_PREFIX + entry_line)
                i = j + 1
                continue

        result_lines.append(line)
        i += 1

    return "\n".join(result_lines)
