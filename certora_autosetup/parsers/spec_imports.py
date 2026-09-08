"""Utilities for parsing CVL spec file imports."""
from collections import deque
from pathlib import Path

from certora_autosetup.utils.logger import logger


def imports_in_cvl(text: str) -> list[str]:
    """Every ``import "<target>"`` target in CVL ``text``, in source order.

    A char-level port of the CVL lexer's import rule: a string literal is one whole token and
    comments are trivia, so an ``import`` written inside a string or a ``//`` / ``/* */`` comment is
    never a real import — exactly the property that makes the parser AST robust where a line regex is
    not (the parser's `Ast.importedSpecFiles` is built the same way; see EVMVerifier `cvl.jflex` /
    `com.certora.certoraprover.cvl.JavaAst`). We reproduce that rule in Python rather than shelling
    out to the CVL parser because this runs on hot paths (per-job cache keys, per-edit buffer
    digests) where a JVM subprocess per call is untenable. Not line-limited, so multiple imports on
    one line and an import after an inline block comment are all found. Soundness matters: callers
    hash a spec's imports to invalidate caches, so a missed import would let a stale result survive an
    edit to that import.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':  # string literal — skip it, honoring backslash escapes
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == '\\' else 1
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':  # line comment
            nl = text.find('\n', i)
            i = n if nl == -1 else nl + 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':  # block comment
            end = text.find('*/', i + 2)
            i = n if end == -1 else end + 2
            continue
        if text.startswith('import', i) and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == '_')):
            j = i + len('import')
            # A keyword here, not the prefix of an identifier like `importFoo`.
            if j < n and not (text[j].isalnum() or text[j] == '_'):
                k = j
                while k < n and text[k] in ' \t\r\n':
                    k += 1
                if k < n and text[k] == '"':  # import "<target>"
                    m, buf = k + 1, []
                    while m < n and text[m] != '"':
                        if text[m] == '\\' and m + 1 < n:
                            buf.append(text[m + 1]); m += 2
                        else:
                            buf.append(text[m]); m += 1
                    out.append(''.join(buf))
                    i = m + 1
                    continue
        i += 1
    return out


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
        for import_path in imports_in_cvl(path.read_text()):
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
