"""Spell the starting tuning files the way a target's platform generation does.

The starting tuning files (:mod:`composer.spec.cvlr.tuning`) are written with ``solana_program::``
paths (``solana_program::account_info::AccountInfo``). From ``solana-program`` 2.2 on those are
re-exports. A demangled symbol carries the path of the crate that defines the item
(``solana_account_info::AccountInfo``), so a directive in the old spelling matches nothing. It
does not fail. It does not apply.

:class:`~composer.spec.cvlr_reference.PathAlias` pairs a canonical spelling with this generation's
spellings. This module applies those pairs.

One concept can have more than one spelling. ``solana-program`` kept its own
``invoke_signed_unchecked``, and the one on the call path is ``solana-cpi``'s, so one directive
becomes two.

An alias is used only when the target resolves the crate it names. The aliases are declared for
the post-split generation. Dropping the ones whose crate is absent keeps them safe on a 1.18
target, whose paths are already the canonical ones.
"""

import logging
import re
from dataclasses import dataclass
from collections.abc import Iterable

from composer.cargo.metadata import Workspace
from composer.spec.cvlr_reference import ChainReference, NamespacePattern, PathAlias

_log = logging.getLogger(__name__)

#: An inlining directive: the attribute and the pattern on one line.
_INLINE_LINE = re.compile(r"^(?P<lead>#\[inline(?:\(never\))?\]\s+)(?P<pattern>\S.*?)\s*$")

#: A points-to summary's type annotation. These *precede* the pattern they belong to, one per line,
#: so a summary whose pattern fans out has to carry its whole annotation block along with it.
_TYPE_LINE = re.compile(r"^#\[type\(.*\)\]\s*$")


def _crate_of(path: str) -> str:
    return path.split("::", 1)[0].replace("_", "-")


@dataclass(frozen=True)
class PathDialect:
    """How one target spells the paths in the starting tuning files.

    Built by :func:`dialect_for` from the aliases whose crates this target resolves.
    """

    aliases: tuple[PathAlias, ...] = ()

    def spellings(self, pattern: str) -> tuple[str, ...]:
        """``pattern`` in this dialect. Unchanged when it names nothing that moved.

        Longest canonical first, so a symbol alias wins over the module alias it sits inside.
        ``solana_program::program::invoke_signed_unchecked`` must not be rewritten by an alias for
        ``solana_program::program``.
        """
        rendered = [pattern]
        for alias in sorted(self.aliases, key=lambda a: -len(a.canonical)):
            if not any(alias.canonical in p for p in rendered):
                continue
            rendered = _unique(
                p.replace(alias.canonical, actual) for p in rendered for actual in alias.actual
            )
        return tuple(rendered)

    def render(self, text: str) -> str:
        """One tuning file with every directive spelled for this target.

        Comments, blank lines, and grouping are kept. A second spelling is inserted directly
        under the first.
        """
        out: list[str] = []
        annotations: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if _TYPE_LINE.match(stripped):
                annotations.append(line)
                continue
            inline = _INLINE_LINE.match(stripped)
            if inline is not None:
                spellings = self.spellings(inline["pattern"])
                # Unchanged lines are copied as they arrived, including trailing whitespace, so
                # the composite reads line for line against the starting layer.
                out += (
                    [line]
                    if spellings == (inline["pattern"],)
                    else [f"{inline['lead']}{p}" for p in spellings]
                )
                continue
            if stripped and not stripped.startswith(";"):
                spellings = self.spellings(stripped)
                if spellings == (stripped,):
                    out += [*annotations, line]
                    annotations = []
                    continue
                for n, pattern in enumerate(spellings):
                    # The starting layers separate annotated summary blocks with a blank line.
                    # Without one, two summaries read as a single block.
                    if n and annotations:
                        out.append("")
                    out += [*annotations, pattern]
                annotations = []
                continue
            # A comment or a blank line ends the annotation block. Flush it as written
            # instead of attaching it to a later pattern.
            out += annotations
            annotations = []
            out.append(line)
        return "\n".join(out + annotations) + "\n"


def _unique(patterns: Iterable[str]) -> list[str]:
    """``patterns`` with duplicates dropped, first occurrence kept.

    An alias can list its canonical spelling as one of its own replacements, for a symbol that
    exists on both sides of a split. Without this that directive would be emitted twice.
    """
    seen: dict[str, None] = {}
    for p in patterns:
        seen.setdefault(p, None)
    return list(seen)


def dialect_for(workspace: Workspace, reference: ChainReference) -> PathDialect:
    """The spelling ``workspace`` uses for the platform generation ``reference`` names.

    Aliases whose crate the target does not resolve are dropped. On an older generation this
    returns a dialect that changes nothing.
    """
    aliases: list[PathAlias] = []
    for alias in reference.platform.path_aliases:
        match alias:
            case PathAlias(canonical=canonical, actual=actual):
                usable = tuple(a for a in actual if workspace.resolved(_crate_of(a)) is not None)
                if usable:
                    aliases.append(PathAlias(canonical, usable))
            case NamespacePattern(canonical=canonical, actual=actual):
                aliases.append(PathAlias(canonical, (actual,)))
    dialect = PathDialect(tuple(aliases))
    _log.debug(
        "tuning-path dialect for %s: %d of %d aliases usable",
        reference.platform.label,
        len(aliases),
        len(reference.platform.path_aliases),
    )
    return dialect
