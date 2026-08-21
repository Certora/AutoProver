"""Classify solc's ``ParserError: Source "…" not found`` failures against the conf's packages.

A source-not-found failure has several distinct causes with different remedies, and the
distinction is decidable from the filesystem plus the packages list the conf actually carried:
solc reports the source unit name *after* remapping, so a prefix match of the reported name
against a package's target says whether a remapping fired, and ``is_dir()`` on that target — and,
when it is absent, on the ``node_modules/<pkg>`` above it — says whether the package is installed
at all. Anything the evidence does not decide stays ``UNMAPPED_IMPORT`` rather than being guessed
at.

What this deliberately does NOT try to say: why a package is missing (autosetup does not run the
dependency install and never sees its exit status), which version was intended when several
ancestors provide a package, or whether a file missing inside an installed package means a wrong
remapping suffix or a version mismatch — the evidence is identical for both. solc also truncates
its error list, so the absence of a class from one output is not evidence that it does not occur.
"""

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from certora_autosetup.utils.remappings import node_modules_package_root

# A solc source-location line, e.g. ``   --> contracts/Foo.sol:120:9:``. It names the offending
# file in a whole-project (non-autofinder) solc error, where there is no ``Compiling <path>...``
# progress line to recover it from.
_SOURCE_LOCATION_RE = re.compile(r"^\s*-->\s+(?P<path>.+?):\d+:\d+:?\s*$")


def path_from_source_location_line(line: str) -> Optional[str]:
    """Return ``<path>`` from a solc ``  --> <path>:<line>:<col>:`` source-location line, or
    None if ``line`` isn't one."""
    match = _SOURCE_LOCATION_RE.match(line)
    return match.group("path") if match else None


# Wrap-tolerant: solc hard-wraps its diagnostics at a fixed width, so the phrase is regularly
# split across newlines. Matched against whitespace-normalized output (see `_normalize_with_lines`).
_SOURCE_NOT_FOUND_RE = re.compile(r'ParserError:\s+Source\s+"(?P<src>[^"]+)"\s+not\s+found')

# certoraRun prefixes the diagnostic with the importing file: `path/Foo.sol:12:5: ParserError: …`.
_IMPORTER_PREFIX_RE = re.compile(r"(?P<path>\S+):\d+:\d+:\s*$")

# How many lines after the error to look for solc's `--> <path>:<line>:<col>` importer line.
_IMPORTER_LOOKAHEAD = 6


class UnresolvedImportKind(StrEnum):
    """Why one ``Source "…" not found`` happened, as far as the filesystem can decide."""

    # A remapping fired and neither its target directory nor the `node_modules/<pkg>` it names
    # exists: the dependency is not installed where the packages list points. This is the class
    # the ancestor walk resolves.
    PACKAGE_TARGET_MISSING = "package_target_missing"
    # A remapping fired, the `node_modules/<pkg>` it names is installed, but the remapped
    # subdirectory inside it is absent — the same state `resolve_node_modules_target` reports as
    # `subpath_missing`, reached only when no ancestor had the whole target either.
    PACKAGE_SUBPATH_MISSING = "package_subpath_missing"
    # A remapping fired, its target directory exists, but the file inside it does not. Rebuilding
    # the packages list cannot help — the suffix or the installed version is wrong.
    FILE_MISSING_IN_PACKAGE = "file_missing_in_package"
    # No package entry covers the source unit name, and it names no directory in the project tree.
    UNMAPPED_IMPORT = "unmapped_import"
    # The source unit is inside the project's own tree: a missing or misspelled project file.
    MISSING_PROJECT_FILE = "missing_project_file"


@dataclass
class UnresolvedImport:
    """One unresolved import, with the package entry (if any) that governs it."""

    source_unit: str
    kind: UnresolvedImportKind
    package_key: Optional[str] = None
    package_target: Optional[str] = None
    # The installed `node_modules/<pkg>` above a target that does not exist; set on
    # PACKAGE_SUBPATH_MISSING only, where it is what separates that class from a missing package.
    package_root: Optional[str] = None
    importer: Optional[str] = None
    hint: Optional[str] = None


def _normalize_with_lines(output: str) -> Tuple[str, List[int]]:
    """Whitespace-normalized output plus, per character, the line it came from.

    The line map is what lets a match found in the normalized text be located back in the raw
    output, where the ``-->`` importer line still exists as its own line.
    """
    parts: List[str] = []
    line_of_char: List[int] = []
    for index, line in enumerate(output.splitlines()):
        for token in line.split():
            if parts:
                parts.append(" ")
                line_of_char.append(index)
            parts.append(token)
            line_of_char.extend([index] * len(token))
    return "".join(parts), line_of_char


def parse_unresolved_imports(output: str) -> List[Tuple[str, Optional[str]]]:
    """Return ``(source_unit, importer)`` for every source-not-found error in ``output``.

    ``importer`` is the file whose import failed, taken from solc's ``--> <path>:<line>:<col>``
    location line when there is one, or from the ``<path>:<line>:<col>:`` prefix certoraRun puts
    in front of the diagnostic. It is None when neither is printed.
    """
    normalized, line_of_char = _normalize_with_lines(output)
    raw_lines = output.splitlines()

    matches = list(_SOURCE_NOT_FOUND_RE.finditer(normalized))
    results: List[Tuple[str, Optional[str]]] = []
    for position, match in enumerate(matches):
        start_line = line_of_char[match.start()] if match.start() < len(line_of_char) else 0
        next_line = (
            line_of_char[matches[position + 1].start()]
            if position + 1 < len(matches)
            else len(raw_lines)
        )
        importer: Optional[str] = None
        for line in raw_lines[start_line + 1: min(start_line + 1 + _IMPORTER_LOOKAHEAD, next_line)]:
            importer = path_from_source_location_line(line)
            if importer is not None:
                break
        if importer is None:
            prefix_match = _IMPORTER_PREFIX_RE.search(normalized[:match.start()])
            if prefix_match:
                importer = prefix_match.group("path")
        results.append((match.group("src"), importer))
    return results


def _absolute(path: str, run_root: Path) -> str:
    """Absolute, textually normalized form of a possibly run-root-relative path."""
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(str(run_root), path))


def _split_package(entry: str) -> Optional[Tuple[str, str, str]]:
    """Split a packages entry into ``(context, prefix, target)``; None if it has no ``=``."""
    if "=" not in entry:
        return None
    key, target = entry.split("=", 1)
    context, _, prefix = key.rpartition(":")
    return context, prefix, target


def _is_under(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` itself or lives inside it (textual, both absolute)."""
    parent = parent.rstrip("/")
    return child == parent or child.startswith(parent + "/")


def classify_unresolved_import(
    source_unit: str,
    packages: List[str],
    run_root: Path,
    importer: Optional[str] = None,
) -> UnresolvedImport:
    """Decide why ``source_unit`` did not resolve, given the packages the conf carried.

    solc names the source unit *after* applying a remapping, so a source unit living under a
    package's target proves that package's remapping fired — and then a single ``is_dir()`` on
    the target separates "the dependency is not installed there" from "the dependency is there
    but this file is not". When no target covers the source unit, a key that textually prefixes
    it means a remapping was declared but did not apply, which for a context-scoped key is
    explained by the context not prefixing the importer.
    """
    absolute_source = _absolute(source_unit, run_root)

    # Longest matching target wins, so nested package targets classify against the one that
    # actually produced this source unit name.
    best: Optional[Tuple[str, str, str]] = None
    for entry in packages:
        split = _split_package(entry)
        if split is None:
            continue
        _, prefix, target = split
        absolute_target = _absolute(target, run_root)
        if _is_under(absolute_source, absolute_target) and (
            best is None or len(absolute_target) > len(_absolute(best[2], run_root))
        ):
            best = (entry.split("=", 1)[0], prefix, target)

    if best is not None:
        key, _, target = best
        absolute_target = _absolute(target, run_root)
        # A target that does not exist has two very different causes, and the package root above
        # it decides which: no `node_modules/<pkg>` at all (nothing is installed) versus an
        # installed package whose remapped subdirectory is absent. Without this split the second
        # case is described as "the dependency is not installed", contradicting
        # `resolve_node_modules_target`, which already found the package directory.
        package_root = node_modules_package_root(absolute_target)
        if Path(absolute_target).is_dir():
            kind = UnresolvedImportKind.FILE_MISSING_IN_PACKAGE
            package_root = None
        elif (
            package_root is not None
            and package_root != absolute_target
            and Path(package_root).is_dir()
        ):
            kind = UnresolvedImportKind.PACKAGE_SUBPATH_MISSING
        else:
            kind = UnresolvedImportKind.PACKAGE_TARGET_MISSING
            package_root = None
        return UnresolvedImport(
            source_unit=source_unit,
            kind=kind,
            package_key=key,
            package_target=target,
            package_root=package_root,
            importer=importer,
        )

    # No target covers it, but a declared key does: the remapping exists and did not apply.
    for entry in packages:
        split = _split_package(entry)
        if split is None:
            continue
        context, prefix, _ = split
        if not prefix or not source_unit.startswith(prefix):
            continue
        hint = None
        if context and importer is not None and not importer.startswith(context):
            hint = (
                f"remapping '{context}:{prefix}' is scoped to context '{context}', which does not "
                f"prefix the importing file '{importer}', so it never applied"
            )
        elif context:
            hint = (
                f"remapping '{context}:{prefix}' is scoped to context '{context}'; solc did not "
                f"name the importing file, so whether the context applied is undecidable here"
            )
        return UnresolvedImport(
            source_unit=source_unit,
            kind=UnresolvedImportKind.UNMAPPED_IMPORT,
            package_key=entry.split("=", 1)[0],
            importer=importer,
            hint=hint,
        )

    first_segment = source_unit.replace(os.sep, "/").split("/")[0]
    if first_segment and (run_root / first_segment).is_dir():
        return UnresolvedImport(
            source_unit=source_unit,
            kind=UnresolvedImportKind.MISSING_PROJECT_FILE,
            importer=importer,
        )

    return UnresolvedImport(
        source_unit=source_unit,
        kind=UnresolvedImportKind.UNMAPPED_IMPORT,
        importer=importer,
    )


def _describe_one(failure: UnresolvedImport) -> str:
    """One line naming the source unit and the remedy its class implies."""
    if failure.kind == UnresolvedImportKind.PACKAGE_TARGET_MISSING:
        return (
            f'"{failure.source_unit}": package \'{failure.package_key}\' maps to '
            f"{failure.package_target}, which does not exist — the dependency is not installed "
            f"there and was not found in any ancestor node_modules up to the run root"
        )
    if failure.kind == UnresolvedImportKind.PACKAGE_SUBPATH_MISSING:
        return (
            f'"{failure.source_unit}": package \'{failure.package_key}\' maps to '
            f"{failure.package_target}; the package is installed at {failure.package_root} but "
            f"the remapped subdirectory inside it is missing — the remapping suffix or the "
            f"installed version is wrong"
        )
    if failure.kind == UnresolvedImportKind.FILE_MISSING_IN_PACKAGE:
        return (
            f'"{failure.source_unit}": package \'{failure.package_key}\' maps to '
            f"{failure.package_target}, which exists but does not contain this file — the "
            f"remapping suffix or the installed version is wrong, so rebuilding the packages "
            f"list cannot help"
        )
    if failure.kind == UnresolvedImportKind.MISSING_PROJECT_FILE:
        return (
            f'"{failure.source_unit}": inside the project tree but absent — a missing or '
            f"misspelled project file, not a dependency problem"
        )
    detail = f" ({failure.hint})" if failure.hint else ""
    return (
        f'"{failure.source_unit}": no package entry resolves this import{detail}'
    )


def describe_unresolved_imports(failures: List[UnresolvedImport]) -> str:
    """Human-readable summary, grouped by kind so a long failure list stays readable."""
    if not failures:
        return ""
    by_kind: Dict[UnresolvedImportKind, List[UnresolvedImport]] = {}
    for failure in failures:
        by_kind.setdefault(failure.kind, []).append(failure)

    lines: List[str] = []
    for kind, group in by_kind.items():
        lines.append(f"{kind.value} ({len(group)}):")
        lines.extend(f"  - {_describe_one(failure)}" for failure in group)
    return "\n".join(lines)
