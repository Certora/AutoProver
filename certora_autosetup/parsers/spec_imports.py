"""Utilities for parsing CVL spec file imports."""
import re
from collections import deque
from pathlib import Path

from certora_autosetup.utils.logger import logger


def parse_imports_from_spec(spec_path: Path, recursive: bool = True) -> list[Path]:
    """
    Parse import statements from a spec file and resolve to absolute paths.

    Args:
        spec_path: Path to the spec file to parse
        recursive: If True (default), recursively resolve all transitive imports.
                   If False, only return direct imports.

    Returns:
        List of absolute paths to imported spec files
    """
    def _parse_direct_imports(path: Path) -> list[Path]:
        """Parse direct imports from a single spec file."""
        imports = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("import") and '"' in line:
                    import_match = re.search(r'import\s+"([^"]+)"', line)
                    if import_match:
                        import_path = import_match.group(1)
                        resolved_path = (path.parent / import_path).resolve()
                        if resolved_path.exists():
                            imports.append(resolved_path)
                        else:
                            logger.warning(f"Could not resolve import '{import_path}' from {path}")
        return imports

    if not recursive:
        return _parse_direct_imports(spec_path)

    # BFS for transitive closure
    all_imports: list[Path] = []
    visited: set[Path] = {spec_path.resolve()}
    queue = deque(_parse_direct_imports(spec_path))

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        all_imports.append(current)

        for imported in _parse_direct_imports(current):
            if imported not in visited:
                queue.append(imported)

    return all_imports


def find_shared_specs(spec_files: list[Path]) -> set[str]:
    """
    Find specs that are imported by other specs (shared/library specs).

    Args:
        spec_files: List of spec file paths to analyze

    Returns:
        Set of spec stems (filenames without .spec extension) that are imported
        by other specs and should be treated as shared.
    """
    shared_specs: set[str] = set()
    for spec_file in spec_files:
        imports = parse_imports_from_spec(spec_file)
        for imported in imports:
            shared_specs.add(imported.stem)
    return shared_specs
