"""Rendering the canonical tuning files in the path spelling a target's platform speaks.

The four files under ``envs/`` are vendored verbatim from upstream and written in the *monolith's*
spelling: ``solana_program::account_info::AccountInfo``, ``solana_program::pubkey::Pubkey``. On
``solana-program`` 2.2 and later those are re-export aliases, and a demangled Rust symbol carries the
path of the crate that *defines* a thing — ``solana_account_info::AccountInfo`` — so a directive
written in the canonical spelling matches nothing at all. It does not fail; it silently does not
apply, which is how fourteen directives and one blanket stopped taking effect on the first real
target this backend was pointed at. ``docs/cvlr-backend-plan.md`` §7.5.6 is that investigation.

So the vendored files are the **concept keys**, kept byte-identical to upstream so
:mod:`composer.scripts.refresh_cvlr_envs` stays a copy, and the platform generation says how to spell
those concepts (:class:`~composer.spec.cvlr_reference.PathAlias`). This module is the seam between
them.

Two properties are worth stating because they are the reason this is not a string substitution:

* **A concept can have more than one spelling.** ``solana-program`` kept a real
  ``invoke_signed_unchecked`` while the one on the call path is ``solana-cpi``'s, so one canonical
  directive becomes two.
* **A spelling only counts if the target resolves the crate.** Aliases are declared against the
  post-split generation; dropping the ones whose crate is absent is what makes them safe to apply to
  a 1.18 target, whose paths are already the canonical ones.
"""

import dataclasses
import logging
import re
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
    """The cargo crate name a Rust path's first segment belongs to."""
    return path.split("::", 1)[0].replace("_", "-")


@dataclasses.dataclass(frozen=True)
class PathDialect:
    """How one target spells the concepts the canonical tuning files name.

    Constructed by :func:`dialect_for` rather than declared, because a declared alias is only usable
    once the target's resolved graph has confirmed the crate it names.
    """

    aliases: tuple[PathAlias, ...] = ()

    def spellings(self, pattern: str) -> tuple[str, ...]:
        """``pattern`` in this dialect — itself unchanged when it names nothing that moved.

        Longest canonical first, so that a symbol-level alias beats the module-level one it sits
        inside: ``solana_program::program::invoke_signed_unchecked`` must not be decided by an alias
        for ``solana_program::program``.
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

        Line-oriented and order-preserving, because the files are read by people as well as by the
        prover: comments, blank lines and grouping survive, and a directive that gains a second
        spelling gains it immediately below the first.
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
                # Unchanged lines are emitted as they arrived, trailing whitespace and all. The
                # vendored files are a copy of upstream's, and a render that reflowed the lines it
                # did not change would make the next refresh report a diff that is ours.
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
                    # Upstream separates annotated summary blocks with a blank line, and the two
                    # blocks it hand-duplicated for the split follow that. Emitting a repeat without
                    # one runs two summaries together into something that reads like one.
                    if n and annotations:
                        out.append("")
                    out += [*annotations, pattern]
                annotations = []
                continue
            # A comment or a blank line: any annotations buffered before it belonged to nothing, so
            # they pass through as written rather than being silently attached to a later pattern.
            out += annotations
            annotations = []
            out.append(line)
        return "\n".join(out + annotations) + "\n"


def _unique(patterns: Iterable[str]) -> list[str]:
    """``patterns`` with duplicates dropped, first occurrence winning.

    An alias whose canonical spelling is one of its own ``actual`` entries — how a symbol that exists
    on *both* sides of a split is declared — would otherwise emit the same directive twice.
    """
    seen: dict[str, None] = {}
    for p in patterns:
        seen.setdefault(p, None)
    return list(seen)


def dialect_for(workspace: Workspace, reference: ChainReference) -> PathDialect:
    """The spelling ``workspace`` speaks, for the platform generation ``reference`` names.

    Aliases naming a crate the target does not resolve are dropped, so this is safe to call for a
    target on an older generation: it returns a dialect that changes nothing.
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
