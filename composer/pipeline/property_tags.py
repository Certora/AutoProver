"""``@property`` tags in CVL specs, and the checks of a properties suite against them.

A tag in the comment block directly above a rule or invariant names the properties the rule
establishes, by the ``id`` they have in the project's ``*.property-suite.json`` files
(``user_properties``)::

    /// @property burn_rounding_favors_vault, no_round_trip_profit
    rule burn_rounding_favors_vault_withdraw(env e, uint256 assets) { ... }

    /**
     * Withdrawals never take more than the caller's share.
     * @property withdraw_bounded_by_share
     */
    invariant shareBounded() ...

A tag names one or more ids, separated by commas; several tags add up. The block ends on the line
directly above the rule: a blank line in between parts them, and a tag anywhere else is stray.
The Prover drops comments, so a tag never changes what a spec checks.

``validate-properties <project root> <suite>`` checks a suite and the project's tags, and lists
each property with the rules tagged with it.
"""

import argparse
import os
import re
import sys
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from certora_autosetup.setup.summary_resolver import extract_cvl_ast
from pydantic import ValidationError

from composer.pipeline.user_properties import PROPERTY_FILE_SUFFIX, UserProperty, UserPropertyFile

PROPERTY_TAG = "@property"

_TAG = re.compile(re.escape(PROPERTY_TAG) + r"\s+([\w.-]+(?:\s*,\s*[\w.-]+)*)")

#: Directory names a project walk skips, wherever they sit: dependency trees and build output.
SKIPPED_DIR_NAMES = frozenset({"node_modules", "lib", "out", "cache", "artifacts"})


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comment:
    #: First and last line of the comment, counted from 1 as an editor counts them.
    start_line: int
    end_line: int
    #: The comment without its markers, stripped.
    text: str
    #: True when nothing but whitespace precedes the comment on its first line.
    own_line: bool


def comments(source: str) -> list[Comment]:
    """Every comment in the CVL ``source``, in order: ``//`` line comments and ``/* ... */``
    block comments, outside string literals."""
    out: list[Comment] = []
    i, line, n = 0, 1, len(source)
    code_on_line = False
    while i < n:
        ch = source[i]
        if ch == "\n":
            line += 1
            code_on_line = False
            i += 1
        elif ch == '"':
            # A string literal: skip to its closing quote, honoring backslash escapes.
            i += 1
            while i < n and source[i] != '"':
                if source[i] == "\n":
                    line += 1
                i += 2 if source[i] == "\\" else 1
            i += 1
            code_on_line = True
        elif source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            out.append(Comment(line, line, source[i + 2:end].strip(), not code_on_line))
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end
            body = source[i + 2:end]
            last = line + body.count("\n")
            text = " ".join(part.strip().lstrip("*").strip() for part in body.splitlines())
            out.append(Comment(line, last, text.strip(), not code_on_line))
            line = last
            i = end + 2
        else:
            if not ch.isspace():
                code_on_line = True
            i += 1
    return out


def block_above(found: Sequence[Comment], line: int) -> list[Comment]:
    """Of ``found``, the block of comment-only lines that ends directly above ``line``, in
    order."""
    above: list[Comment] = []
    by_end = {c.end_line: c for c in found if c.own_line}
    at = line - 1
    while (c := by_end.get(at)) is not None:
        above.insert(0, c)
        at = c.start_line - 1
    return above


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def tagged_ids(text: str) -> list[str]:
    """The ids the ``@property`` tags of ``text`` name, in order, each once."""
    out: list[str] = []
    for m in _TAG.finditer(text):
        for pid in (part.strip() for part in m.group(1).split(",")):
            if pid not in out:
                out.append(pid)
    return out


@dataclass(frozen=True)
class StrayTag:
    """A ``@property`` tag in a comment that is not directly above a rule or invariant."""
    spec_file: str
    line: int
    ids: tuple[str, ...]


@dataclass(frozen=True)
class SpecTags:
    """The tags of one spec file."""
    #: Per rule or invariant written in the file with at least one tag: the ids it names.
    by_rule: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: The file's tags that are above no rule or invariant, in line order.
    stray: tuple[StrayTag, ...] = ()


def read_tags(spec_file: str, source: str, first_lines: Mapping[str, int]) -> SpecTags:
    """The tags of ``source``, the text of ``spec_file``. ``first_lines`` gives the first line,
    counted from 1, of each rule and invariant the file declares (``rule_lines``, or a
    typechecked CVL AST)."""
    found = comments(source)
    by_rule: dict[str, tuple[str, ...]] = {}
    used: set[int] = set()
    for name, line in first_lines.items():
        above = block_above(found, line)
        used.update(c.start_line for c in above)
        if ids := tagged_ids(" ".join(c.text for c in above)):
            by_rule[name] = tuple(ids)
    stray = tuple(
        StrayTag(spec_file, c.start_line, tuple(ids))
        for c in found if c.start_line not in used and (ids := tagged_ids(c.text))
    )
    return SpecTags(by_rule, stray)


def rule_lines(source: str) -> dict[str, tuple[int, int] | None] | None:
    """Each rule and invariant the CVL ``source`` declares itself — its rules, then its
    invariants — with its first and last line, counted from 1, or ``None`` when the parser gives
    no place for it. Read with the standalone CVL parser, so it needs no conf and no build.
    ``None`` when the text does not parse on its own."""
    payload = extract_cvl_ast(source)
    if payload is None:
        return None
    ast = payload.get("ast") or {}
    lines: dict[str, tuple[int, int] | None] = {}
    for rule in ast.get("rules", []):
        name = (rule.get("ruleIdentifier") or {}).get("displayName")
        if name:
            lines[name] = _range_lines(rule.get("range"))
    for inv in ast.get("invs", []):
        name = inv.get("id")
        if name:
            lines[name] = _range_lines(inv.get("range"))
    return lines


def _range_lines(rng: object) -> tuple[int, int] | None:
    """A ``utils.Range.Range`` as its first and last line, counted from 1 (the AST counts from
    0)."""
    if not isinstance(rng, dict):
        return None
    start, end = rng.get("start"), rng.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    first, last = start.get("line"), end.get("line")
    if not isinstance(first, int) or not isinstance(last, int):
        return None
    return first + 1, last + 1


# ---------------------------------------------------------------------------
# The repo's properties files
# ---------------------------------------------------------------------------


def iter_files(
    project_root: Path, suffix: str, skipped: Iterable[str] = (),
    skipped_names: Collection[str] = SKIPPED_DIR_NAMES,
) -> Iterator[Path]:
    """Project-relative paths of every file whose name ends with ``suffix``, outside hidden
    directories, directories named in ``skipped_names`` and the project-relative posix paths of
    ``skipped``, in path order."""
    skip = set(skipped)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        rel_dir = Path(dirpath).relative_to(project_root)
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in skipped_names
            and (rel_dir / d).as_posix() not in skip
        )
        found.extend(rel_dir / f for f in filenames if f.endswith(suffix))
    yield from sorted(found)


#: Per property id: each properties file that defines it, with its definition.
type Definitions = Mapping[str, Sequence[tuple[Path, UserProperty]]]


@dataclass(frozen=True)
class PropertyFiles:
    """What a project's properties files define."""
    #: Per property id: each file that defines it, with its definition.
    defined: dict[str, list[tuple[Path, UserProperty]]]
    #: Each file that does not read as a properties file, with why.
    unreadable: dict[Path, str]


def read_property_files(root: Path, files: Iterable[Path]) -> PropertyFiles:
    """What the properties ``files`` (relative to ``root``) define, read file by file: imports are
    not followed, each file is read for what it defines itself."""
    defined: dict[str, list[tuple[Path, UserProperty]]] = {}
    unreadable: dict[Path, str] = {}
    for rel in files:
        try:
            loaded = UserPropertyFile.load(root / rel)
        except (OSError, ValueError) as e:
            unreadable[rel] = _why_unreadable(e)
            continue
        for p in loaded.properties:
            defined.setdefault(p.id, []).append((rel, p))
    return PropertyFiles(defined, unreadable)


def _why_unreadable(e: OSError | ValueError) -> str:
    """Why a properties file did not load, without its path: each field error, or the OS error."""
    if isinstance(e.__cause__, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'file'}: {err['msg']}" for err in e.__cause__.errors()
        )
    return e.strerror or str(e) if isinstance(e, OSError) else str(e)


def defined_differently(ids: Iterable[str], defined: Definitions) -> dict[str, list[Path]]:
    """Of ``ids``, those that two properties files define with a different description or sort,
    with the files that define them."""
    return {
        pid: [path for path, _ in defined[pid]]
        for pid in sorted(ids)
        if pid in defined and len({(p.description, p.sort) for _, p in defined[pid]}) > 1
    }


# ---------------------------------------------------------------------------
# Checking a suite
# ---------------------------------------------------------------------------


type Severity = Literal["error", "warning", "info"]

type ProblemKind = Literal[
    "suite-does-not-load",
    "unreadable-property-file",
    "unreadable-spec",
    "unparsed-spec",
    "unknown-property",
    "stray-property-tag",
    "property-defined-differently",
    "untagged-property",
]


@dataclass(frozen=True)
class Problem:
    severity: Severity
    kind: ProblemKind
    message: str
    file: str | None = None
    line: int | None = None
    #: The rule or invariant the problem is about, if one.
    rule: str | None = None
    #: How to fix it.
    hint: str | None = None


def tag_problems(
    tagged: Iterable[tuple[tuple[str, str], Sequence[str]]],
    stray: Iterable[StrayTag],
    suite_ids: Collection[str],
    files: PropertyFiles,
) -> list[Problem]:
    """The problems of a project's tags and properties files, for the suite of ``suite_ids``.
    ``tagged`` pairs each ``(spec file, rule)`` with at least one tag with the ids it names; ``stray``
    the tags above no rule; ``files`` what the project's properties files define.

    A properties file that does not read and a tag naming an id that no properties file defines
    are errors (a tag naming another suite's property is fine); a stray tag and a property of the
    suite that two files define differently are warnings."""
    problems = [
        Problem(
            "error", "unreadable-property-file", f"The file does not read as a properties file: {why}",
            path.as_posix(), hint="Fix the file, or rename it if it is not a property suite.",
        )
        for path, why in files.unreadable.items()
    ]
    for (spec, rule), ids in tagged:
        unknown = [pid for pid in ids if pid not in suite_ids and pid not in files.defined]
        if unknown:
            problems.append(Problem(
                "error", "unknown-property",
                f"Rule {rule} is tagged with {', '.join(unknown)}, which no properties file defines.",
                spec, rule=rule, hint="Define the property in a properties file, or correct the id in the tag.",
            ))
    problems += [
        Problem(
            "warning", "stray-property-tag",
            f"The tag ({', '.join(t.ids)}) is not directly above a rule or invariant, so it tags no rule.",
            t.spec_file, t.line,
            hint="Move the tag into the comment directly above its rule, with no blank line between them.",
        )
        for t in stray
    ]
    problems += [
        Problem(
            "warning", "property-defined-differently",
            f"Property {pid} has different definitions in {', '.join(p.as_posix() for p in paths)}.",
            hint="Define the property in one file, and import that file from every suite that uses it.",
        )
        for pid, paths in defined_differently(suite_ids, files.defined).items()
    ]
    return problems


@dataclass(frozen=True)
class SuiteCheck:
    """A suite checked against the project's specs and properties files."""
    #: Per property of the suite, in suite order: the ``(spec file, rule)`` pairs tagged with it.
    rules: Mapping[str, list[tuple[str, str]]]
    problems: list[Problem]

    @property
    def ok(self) -> bool:
        return not any(p.severity == "error" for p in self.problems)


def check_suite(root: Path, suite: str, skipped: Iterable[str] = ()) -> SuiteCheck:
    """Check the suite ``suite`` (relative to ``root``) and every spec of the project: the suite
    loads, every properties file reads, every tag names a property some properties file defines
    and sits directly above a rule or invariant, and no property of the suite has two different
    definitions. ``skipped`` holds project-relative posix paths of directories not to search
    (``iter_files``)."""
    try:
        loaded = UserPropertyFile.load_suite(root / suite)
    except (OSError, ValueError) as e:
        return SuiteCheck({}, [Problem("error", "suite-does-not-load", str(e), suite)])
    ids = [p.id for p in loaded.properties]
    skipped = list(skipped)
    problems: list[Problem] = []
    tagged: dict[tuple[str, str], tuple[str, ...]] = {}
    stray: list[StrayTag] = []
    for spec in iter_files(root, ".spec", skipped):
        try:
            text = (root / spec).read_text()
        except OSError as e:
            problems.append(Problem("error", "unreadable-spec", f"The spec cannot be read: {e}", spec.as_posix()))
            continue
        if not tagged_ids(text):
            continue
        lines = rule_lines(text)
        if lines is None:
            problems.append(Problem(
                "error", "unparsed-spec", "The spec does not parse on its own, so its tags were not read.",
                spec.as_posix(),
            ))
            continue
        tags = read_tags(spec.as_posix(), text, {r: ln[0] for r, ln in lines.items() if ln is not None})
        tagged.update(((spec.as_posix(), rule), pids) for rule, pids in tags.by_rule.items())
        stray += tags.stray
    files = read_property_files(root, iter_files(root, PROPERTY_FILE_SUFFIX, skipped))
    problems += tag_problems(tagged.items(), stray, ids, files)
    rules: dict[str, list[tuple[str, str]]] = {pid: [] for pid in ids}
    for ref, pids in tagged.items():
        for pid in pids:
            if pid in rules:
                rules[pid].append(ref)
    problems += [
        Problem("info", "untagged-property", f"No rule is tagged with property {pid}.")
        for pid, refs in rules.items() if not refs
    ]
    return SuiteCheck(rules, problems)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate-properties",
        description="Check a properties suite and the @property tags of a project's specs.",
    )
    parser.add_argument("project_root", type=Path)
    parser.add_argument("suite", help="The suite's properties file, relative to the project root.")
    parser.add_argument(
        "--skip", action="append", default=[], metavar="DIR",
        help="A directory, relative to the project root, not to search for specs and properties "
        "files (repeatable). Hidden directories and dependency and build trees are always skipped.",
    )
    args = parser.parse_args(argv)
    result = check_suite(args.project_root, args.suite, args.skip)
    for pid, refs in result.rules.items():
        print(pid)
        for spec, rule in refs:
            print(f"    {spec}:{rule}")
    for p in result.problems:
        where = f"{p.file}:{p.line}: " if p.file and p.line else (f"{p.file}: " if p.file else "")
        print(f"{p.severity}: {where}{p.message}", file=sys.stderr)
        if p.hint:
            print(f"    {p.hint}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
