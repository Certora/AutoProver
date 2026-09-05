"""Rewrite imports whose spelling does not match the file that exists on disk.

A project authored on a case-insensitive filesystem (macOS, Windows) can carry an import
whose path differs from the real on-disk path — most often in letter case — and compile for
its authors while failing everywhere else. This module compares every component of an
import path against the actual directory entries, and when the filesystem names exactly one
thing the import was meant to name, rewrites the quoted path literal to that spelling.

Detection reads directory entries (``os.scandir``) instead of asking whether a path exists.
On a case-insensitive filesystem an existence check resolves the mis-spelled path, so a
fixer built on ``Path.exists`` would report nothing where it is developed and fire only in
production.

In scope: the letter case of the basename, of any directory component, and of the extension;
Windows separators and repeated slashes; and the right basename in the wrong directory, when
exactly one file in the project carries that basename.

Out of scope, each an explicit bail rather than a silent miss:

- Fuzzy or edit-distance matching. A near-miss name is a different contract, and pointing an
  import at it yields a project that compiles while verifying code nobody asked about.
- Guessing at name shape: CamelCase versus snake_case, singular versus plural.
- Inventing or changing an extension. Only its letter case may change, which follows from
  matching whole basenames case-insensitively.
- Renaming files on disk.
- Imports that resolve through a remapping or package prefix. Repairing those is the
  ``source_not_found_packages`` workaround's job, which rebuilds the conf's packages list.
- Imports naming a path inside a dependency checkout that does not resolve. That is a
  dependency that is absent or laid out differently, and the project's own same-named mock or
  interface is not what the import asked for.
- More than one candidate. Two files differing only in case are legal on a case-sensitive
  filesystem, so an ambiguous match rewrites nothing and says why.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from certora_autosetup.setup.solidity_import_patch import extract_imports_multiline
from certora_autosetup.setup.solidity_utils import DEPENDENCIES, walk_files_by_suffix
from certora_autosetup.utils.import_diagnostics import package_prefixes, parse_unresolved_imports
from certora_autosetup.utils.logger import logger as _logger

LogFunc = Callable[..., None]

SOLIDITY_SUFFIX = ".sol"

# Solidity string literals take either quote, and the rewrite keeps the one the file used.
QUOTES = ('"', "'")


def _default_log(message: str, level: str = "INFO") -> None:
    _logger.log(message, level)


@dataclass(frozen=True)
class ImportSpellingRewrite:
    """One planned replacement of a quoted import path literal.

    ``line`` is a 0-based index into the file's ``readlines()`` and ``column`` the offset of
    ``original`` within that line. Both ``original`` and ``updated`` include the surrounding
    quotes, so the replacement is scoped to the path literal rather than to the whole line,
    and neither contains a newline: the rewrite is single-line and preserves the file's line
    count, which is what the import patcher's line-indexed revert relies on.
    """

    file: Path
    line: int
    column: int
    original: str
    updated: str

    @property
    def original_path(self) -> str:
        """The import path as written, without its quotes."""
        return self.original[1:-1]

    @property
    def updated_path(self) -> str:
        """The import path this rewrite installs, without its quotes."""
        return self.updated[1:-1]


class _DirectoryEntries:
    """Cached ``os.scandir`` listings, one per directory.

    Entry names are grouped by their lowercased form, which is what lets a path component be
    matched against what the directory really holds instead of asking the filesystem to
    resolve the component for us.
    """

    def __init__(self) -> None:
        self._cache: Dict[Path, Dict[str, List[Tuple[str, bool]]]] = {}

    def of(self, directory: Path) -> Dict[str, List[Tuple[str, bool]]]:
        """Map lowercased entry name -> list of ``(real name, is_dir)`` in ``directory``."""
        cached = self._cache.get(directory)
        if cached is not None:
            return cached
        grouped: Dict[str, List[Tuple[str, bool]]] = {}
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    grouped.setdefault(entry.name.lower(), []).append(
                        (entry.name, entry.is_dir())
                    )
        except OSError:
            grouped = {}
        self._cache[directory] = grouped
        return grouped


def choose_spelling(
    component: str, names: Sequence[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Decide which real entry name ``component`` was meant to spell.

    Returns ``(name, None)`` when the filesystem decides it, and ``(None, reason)`` when it
    does not: nothing matches, or several entries do and choosing between them would be a
    guess. There is deliberately no tiebreak — a source rewrite is not recoverable the way a
    conf guess is.
    """
    if component in names:
        return component, None
    lowered = component.lower()
    matches = sorted(name for name in names if name.lower() == lowered)
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"no entry named '{component}' (case-insensitively)"
    return None, (
        f"'{component}' matches {len(matches)} entries case-insensitively: {matches}"
    )


@dataclass(frozen=True)
class _Resolution:
    """Where an import path leads on disk, and whether it was spelled correctly.

    ``exact`` is True only when every component matched an entry character for character —
    i.e. the import already resolves on a case-sensitive filesystem.
    """

    target: Optional[Path]
    exact: bool
    reason: Optional[str]


def _resolve_components(
    base_dir: Path, components: Sequence[str], entries: _DirectoryEntries
) -> _Resolution:
    """Walk ``components`` from ``base_dir`` one directory listing at a time.

    ``.`` is skipped and ``..`` moves up, so the returned target is the file the import names
    regardless of how the path was spelled.
    """
    current = base_dir
    exact = True
    last_index = len(components) - 1
    for index, component in enumerate(components):
        if component == ".":
            continue
        if component == "..":
            current = current.parent
            continue
        candidates = entries.of(current).get(component.lower(), [])
        chosen, reason = choose_spelling(component, [name for name, _ in candidates])
        if chosen is None:
            return _Resolution(None, False, f"in {current}: {reason}")
        if chosen != component:
            exact = False
        is_dir = dict(candidates)[chosen]
        current = current / chosen
        if index == last_index and is_dir:
            return _Resolution(None, False, f"{current} is a directory, not a file")
        if index != last_index and not is_dir:
            return _Resolution(None, False, f"{current} is not a directory")
    # Every component is now spelled exactly as the filesystem spells it, so a plain stat is
    # safe here and catches the paths made only of `.` and `..`, which name no entry at all.
    if not current.is_file():
        return _Resolution(None, False, f"{current} is not a file")
    return _Resolution(current, exact, None)


def _normalize_separators(import_path: str) -> str:
    """The same path with Windows separators and repeated slashes spelled the one way."""
    normalized = import_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _remapped_prefix(import_path: str, prefixes: Sequence[str]) -> Optional[str]:
    """The longest packages prefix solc would rewrite ``import_path`` with, if any."""
    matches = [prefix for prefix in prefixes if import_path.startswith(prefix)]
    return max(matches, key=len) if matches else None


def _is_relative_import(import_path: str) -> bool:
    """True for the two forms solc resolves against the importing file's directory."""
    return import_path.startswith("./") or import_path.startswith("../")


def inside_dependency_tree(path: Path, project_root: Path) -> bool:
    """True when ``path`` sits inside a dependency checkout, or outside the project.

    Dependency sources are read — an import may legitimately point into one — but never
    rewritten: they are not the project's code, and a dependency install overwrites them.
    """
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return True
    return any(part in DEPENDENCIES for part in relative.parts)


def _index_by_basename(files: Sequence[Path]) -> Dict[str, List[Path]]:
    """Map lowercased basename -> every file on disk carrying it, dependencies included.

    This is the index behind the "right basename, wrong directory" case, whose condition is
    that exactly one file on disk carries the basename. A dependency copy is one such file, so
    it counts towards the ambiguity even though only a project file is ever redirected to.
    """
    index: Dict[str, List[Path]] = {}
    for file in files:
        index.setdefault(file.name.lower(), []).append(file)
    return index


def mask_comments(lines: Sequence[str]) -> List[str]:
    """The same lines with every comment byte replaced by a space.

    Import scanning runs over the masked copy so that commented-out code is simply not there:
    a commented ``import`` is no longer read as one, and a ``;`` inside a comment no longer
    ends the statement that a real import began.

    Only comment bytes change, and each is replaced one-for-one, so a line's length and the
    column of everything outside a comment are the same in both copies. A position found in
    the masked lines therefore addresses the same byte of the real file, which is what lets
    the rewrite be located here and applied there.

    Comment starts are recognised by scanning each character in context rather than by
    matching ``//`` or ``/*`` directly, because both sequences occur inside ordinary string
    literals. ``"https://example.com"`` is the common one, and treating its ``//`` as a
    comment would blank the rest of a perfectly good line.
    """
    masked: List[str] = []
    in_block = False
    for line in lines:
        out: List[str] = []
        quote: Optional[str] = None
        escaped = False
        index = 0
        while index < len(line):
            char = line[index]
            pair = line[index : index + 2]
            if in_block:
                if pair == "*/":
                    out.append("  ")
                    index += 2
                    in_block = False
                    continue
                out.append(" " if not char.isspace() else char)
            elif quote is not None:
                out.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif pair == "//":
                # A line comment ends with the line, so blank the rest and keep the
                # terminator, which is what preserves the file's line structure.
                rest = line[index:]
                terminator = rest[len(rest.rstrip("\r\n")) :]
                out.append(" " * (len(rest) - len(terminator)))
                out.append(terminator)
                break
            elif pair == "/*":
                out.append("  ")
                index += 2
                in_block = True
                continue
            else:
                if char in QUOTES:
                    quote = char
                out.append(char)
            index += 1
        masked.append("".join(out))
    return masked


def _locate_literal(
    lines: Sequence[str], start: int, end: int, import_path: str
) -> Tuple[Optional[Tuple[int, int, str]], Optional[str]]:
    """Find the one quoted occurrence of ``import_path`` inside one import statement.

    Searching the statement's whole line range rather than its first line is what makes a
    multi-line ``import {A, B} from "..."`` reachable: the path literal sits on the closing
    line. A literal that occurs more than once gives no unambiguous position to patch, so it
    is left alone.
    """
    hits: List[Tuple[int, int, str]] = []
    for index in range(start, min(end, len(lines) - 1) + 1):
        line = lines[index]
        for quote in QUOTES:
            needle = f"{quote}{import_path}{quote}"
            column = line.find(needle)
            while column != -1:
                hits.append((index, column, needle))
                column = line.find(needle, column + 1)
    if len(hits) != 1:
        return None, f"the quoted path literal occurs {len(hits)} times in the statement"
    return hits[0], None


def _derive_import_text(
    target: Path, source_file: Path, project_root: Path, relative: bool
) -> Optional[str]:
    """Spell ``target`` the way this import spells paths, or None if it cannot be spelled.

    A relative import stays relative to the importing file (with the leading ``./`` solc
    needs to treat it as relative at all); a project-relative one stays relative to the
    project root, and cannot express a target outside it.
    """
    if relative:
        text = Path(os.path.relpath(target, source_file.parent)).as_posix()
        return text if text.startswith(".") else f"./{text}"
    try:
        return target.relative_to(project_root).as_posix()
    except ValueError:
        return None


def _plan_one_import(
    source_file: Path,
    lines: Sequence[str],
    start: int,
    end: int,
    import_path: str,
    project_root: Path,
    entries: _DirectoryEntries,
    basename_index: Dict[str, List[Path]],
    remap_prefixes: Sequence[str],
    log_func: LogFunc,
) -> Optional[ImportSpellingRewrite]:
    """Plan the rewrite for one import statement, or explain why there is none."""

    def skip(reason: str) -> None:
        log_func(f"Import spelling: leaving '{import_path}' in {source_file} alone — {reason}")

    normalized = _normalize_separators(import_path)
    relative = _is_relative_import(normalized)
    components = normalized.split("/")

    if relative:
        base_dir = source_file.parent
    else:
        # A non-relative import is resolved by solc through remappings and include paths. One
        # the conf remaps leads wherever the remapping points, which the project tree cannot
        # tell us, so it is left to the packages workaround — the prefix wins over the leading
        # segment naming a real project directory, because that is what solc does.
        remapped = _remapped_prefix(normalized, remap_prefixes)
        if remapped is not None:
            skip(f"the conf remaps the prefix '{remapped}', so it does not resolve against the project root")
            return None
        # Otherwise only an import whose leading segment names a real project directory is ours
        # to fix; anything else goes through a package prefix the conf does not carry either.
        first = components[0]
        root_dirs = [
            name for name, is_dir in entries.of(project_root).get(first.lower(), []) if is_dir
        ]
        if not first or not root_dirs:
            skip(f"'{first}' is not a directory in the project root, so it resolves through a package prefix")
            return None
        base_dir = project_root

    resolution = _resolve_components(base_dir, components, entries)
    target = resolution.target
    if target is None:
        # Nothing resolves the path as written. A path that names a location inside a
        # dependency checkout is out: an absent or differently-laid-out dependency is a
        # dependency problem, and redirecting such an import at a same-named project file
        # would verify the project's own mock in place of the dependency it names.
        named = Path(os.path.normpath(base_dir / normalized))
        if inside_dependency_tree(named, project_root):
            skip(f"{resolution.reason}; it names a path inside a dependency checkout")
            return None
        # The remaining in-scope case is the right basename in the wrong directory, and only
        # when exactly one file on disk carries that basename. Two or more is an ambiguity,
        # none is a genuinely absent file.
        basename = components[-1]
        candidates = [
            candidate
            for candidate in basename_index.get(basename.lower(), [])
            if candidate != source_file
        ]
        if len(candidates) != 1:
            skip(
                f"{resolution.reason}; {len(candidates)} files on disk are named "
                f"'{basename}' (case-insensitively)"
            )
            return None
        target = candidates[0]
        if inside_dependency_tree(target, project_root):
            skip(
                f"{resolution.reason}; the only file named '{basename}' is {target}, inside a "
                f"dependency checkout"
            )
            return None
    elif resolution.exact and normalized == import_path:
        return None

    updated_path = _derive_import_text(target, source_file, project_root, relative)
    if updated_path is None:
        skip(f"{target} cannot be named relative to the project root")
        return None
    if updated_path == import_path:
        return None

    located, reason = _locate_literal(lines, start, end, import_path)
    if located is None:
        skip(str(reason))
        return None
    line_index, column, needle = located
    return ImportSpellingRewrite(
        file=source_file,
        line=line_index,
        column=column,
        original=needle,
        updated=f"{needle[0]}{updated_path}{needle[0]}",
    )


def plan_import_spelling_fixes(
    project_root: Path,
    log_func: Optional[LogFunc] = None,
    compiler_output: str = "",
    packages: Sequence[str] = (),
) -> List[ImportSpellingRewrite]:
    """Plan every import rewrite the filesystem decides, for the project at ``project_root``.

    The scan is independent of how the compilation failure was classified: a mis-cased import
    behind a remapping is classified differently from one in the project tree, and the fixer's
    reach should not be tied to that taxonomy. ``compiler_output`` is therefore used for
    logging only — it names the imports solc actually could not resolve, which is what tells a
    reader whether the planned rewrites address the failure at hand.

    ``packages`` is the conf's packages list. It is what says whether a non-relative import
    resolves through a remapping instead of against the project tree, which the filesystem
    alone cannot tell: a remapping prefix is free to be spelled like one of the project's own
    root directories.
    """
    log = log_func or _default_log
    project_root = Path(project_root).resolve()

    if compiler_output:
        unresolved = parse_unresolved_imports(compiler_output)
        if unresolved:
            log(
                "Import spelling: solc reported unresolved imports: "
                + ", ".join(f"'{source}'" for source, _ in unresolved)
            )

    all_files = walk_files_by_suffix(project_root, SOLIDITY_SUFFIX)
    entries = _DirectoryEntries()
    basename_index = _index_by_basename(all_files)
    remap_prefixes = package_prefixes(list(packages))

    rewrites: List[ImportSpellingRewrite] = []
    for source_file in all_files:
        if inside_dependency_tree(source_file, project_root):
            continue
        try:
            # newline="" keeps each line's own terminator, so a rewrite of one path literal
            # leaves the rest of the file byte-identical whatever the sources' line endings are.
            with open(source_file, "r", encoding="utf-8", newline="") as handle:
                lines = handle.readlines()
        except (OSError, UnicodeDecodeError) as error:
            log(f"Import spelling: cannot read {source_file}: {error}", "WARNING")
            continue

        # Scanning and locating both run over the comment-masked copy, so a commented-out
        # import is never planned and a literal is never located inside a comment. Columns
        # are shared with the real lines, which is what the rewrite is applied to.
        masked = mask_comments(lines)
        for start, end, import_path in extract_imports_multiline(masked):
            rewrite = _plan_one_import(
                source_file=source_file,
                lines=masked,
                start=start,
                end=end,
                import_path=import_path,
                project_root=project_root,
                entries=entries,
                basename_index=basename_index,
                remap_prefixes=remap_prefixes,
                log_func=log,
            )
            if rewrite is not None:
                rewrites.append(rewrite)

    log(f"Import spelling: planned {len(rewrites)} rewrite(s)")
    return rewrites


def _prefix_shifts(
    rewrites: Sequence[ImportSpellingRewrite],
) -> Dict[Tuple[int, int], int]:
    """Per rewrite, how far its recorded column moves once the ones left of it are applied.

    Recorded columns are positions in the file as it was planned. Two import statements can
    share a line, and then applying the left one moves the right one's column by the
    difference in literal lengths. Keyed by ``(line, column)``, which is unique within a plan.
    """
    by_line: Dict[int, List[ImportSpellingRewrite]] = {}
    for rewrite in rewrites:
        by_line.setdefault(rewrite.line, []).append(rewrite)

    shifts: Dict[Tuple[int, int], int] = {}
    for group in by_line.values():
        running = 0
        for rewrite in sorted(group, key=lambda r: r.column):
            shifts[(rewrite.line, rewrite.column)] = running
            running += len(rewrite.updated) - len(rewrite.original)
    return shifts


def _write_replacements(
    rewrites: Sequence[ImportSpellingRewrite],
    revert: bool,
    log_func: LogFunc,
) -> List[ImportSpellingRewrite]:
    """Replace one recorded slice per rewrite, verifying each before writing.

    Within a line, applying runs left to right and reverting right to left, so in both
    directions every rewrite left of the one being written is in the state its recorded
    column was computed for. Each replacement re-checks that the slice on disk is still the
    text it recorded; a slice that changed under us is reported and skipped, never
    overwritten. Both texts are single-line, so the file's line count is untouched.
    """
    action = "reverted" if revert else "rewrote"
    by_file: Dict[Path, List[ImportSpellingRewrite]] = {}
    for rewrite in rewrites:
        by_file.setdefault(rewrite.file, []).append(rewrite)

    written: List[ImportSpellingRewrite] = []
    for file, file_rewrites in by_file.items():
        try:
            with open(file, "r", encoding="utf-8", newline="") as handle:
                lines = handle.readlines()
        except (OSError, UnicodeDecodeError) as error:
            log_func(f"Import spelling: cannot read {file}: {error}", "WARNING")
            continue

        shifts = _prefix_shifts(file_rewrites)
        applied_here: List[ImportSpellingRewrite] = []
        for rewrite in sorted(file_rewrites, key=lambda r: (r.line, r.column), reverse=revert):
            expected = rewrite.updated if revert else rewrite.original
            replacement = rewrite.original if revert else rewrite.updated
            if rewrite.line >= len(lines):
                log_func(
                    f"Import spelling: {file} has no line {rewrite.line + 1} any more, "
                    f"skipping {action} of {expected}",
                    "WARNING",
                )
                continue
            line = lines[rewrite.line]
            column = rewrite.column + shifts[(rewrite.line, rewrite.column)]
            found = line[column: column + len(expected)]
            if found != expected:
                log_func(
                    f"Import spelling: {file}:{rewrite.line + 1} no longer holds {expected} "
                    f"at column {column} (found {found!r}), skipping {action}",
                    "WARNING",
                )
                continue
            lines[rewrite.line] = (
                line[:column] + replacement + line[column + len(expected):]
            )
            applied_here.append(rewrite)

        if not applied_here:
            continue
        try:
            with open(file, "w", encoding="utf-8", newline="") as handle:
                handle.writelines(lines)
        except OSError as error:
            log_func(f"Import spelling: cannot write {file}: {error}", "ERROR")
            continue
        for rewrite in applied_here:
            old, new = (
                (rewrite.updated, rewrite.original) if revert
                else (rewrite.original, rewrite.updated)
            )
            # At WARNING because masking a genuinely absent file — a shallow clone, an
            # uninitialised submodule, a deleted dependency — looks exactly like a spelling
            # fix from here, so every rewrite has to be visible in the log.
            log_func(
                f"Import spelling: {file}:{rewrite.line + 1} {action} {old} -> {new}",
                "WARNING",
            )
        written.extend(applied_here)
    return written


def apply_import_spelling_fixes(
    rewrites: Sequence[ImportSpellingRewrite], log_func: Optional[LogFunc] = None
) -> List[ImportSpellingRewrite]:
    """Write the planned rewrites; returns the ones that were written."""
    return _write_replacements(rewrites, revert=False, log_func=log_func or _default_log)


def revert_import_spelling_fixes(
    rewrites: Sequence[ImportSpellingRewrite], log_func: Optional[LogFunc] = None
) -> List[ImportSpellingRewrite]:
    """Put the imports back the way they were spelled; returns the ones reverted."""
    return _write_replacements(rewrites, revert=True, log_func=log_func or _default_log)
