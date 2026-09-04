"""Which source files a cargo build actually compiled, read back from rustc's dep-info.

The Rust answer to the EVM backend's ``EditsNotCompiled``: an edit that lands in a file the build
never reads changes nothing and reports nothing, and the run goes on to claim a verdict about code it
did not verify. On Solana that failure has a specific and common shape — an attribute inserted into a
file the ``certora`` feature gates out, or into a module no enabled feature declares — and until now
nothing noticed it (``docs/munge-and-working-copies.md`` §8 gap 2).

rustc emits a Makefile-style ``.d`` beside each artifact listing every source it read, and cargo keeps
them under ``target/<profile>/deps/``. So "did my edit reach the build" is a set-membership question
against a file the compiler wrote.

**Finding the right ``.d`` is the whole problem, and a marker solves it.** Several feature variants of
one crate coexist in a shared ``target/`` (that is the point of
``docs/single-working-tree.md`` §3), each with its own ``.d``, and nothing in the filename says which
feature set produced it. Rather than guess from mtimes, the caller names a file it *knows* this build
compiled — the unit's own harness module, which only this unit's feature declares — and the dep-info
that mentions it is this build's by construction. A caller that can name no such file gets ``None``
and should say it could not check, rather than reporting a false pass.
"""

import logging
import re
from pathlib import Path

_log = logging.getLogger(__name__)

#: Rustc escapes a space in a path as ``\ ``; every other byte is literal. Splitting on unescaped
#: whitespace is therefore the whole parse.
_UNESCAPED_SPACE = re.compile(r"(?<!\\)\s+")


def _targets(text: str) -> list[list[str]]:
    """The dependency lists in one ``.d``, one per rule.

    A dep-info file carries a rule per emitted artifact (``foo.d:`` and ``libfoo.rmeta:`` name the
    same sources) followed by an empty rule per source. Empty right-hand sides are dropped, which is
    what removes the trailing per-source stanzas without having to recognise them.
    """
    lists: list[list[str]] = []
    for line in text.splitlines():
        _, sep, rhs = line.partition(": ")
        if not sep or not rhs.strip():
            continue
        lists.append([p.replace("\\ ", " ") for p in _UNESCAPED_SPACE.split(rhs.strip()) if p])
    return lists


def _resolve(raw: str, roots: tuple[Path, ...]) -> Path | None:
    """A dep-info path as an absolute one, or ``None`` if it names nothing here.

    Paths are relative to the directory cargo invoked rustc from, which is the workspace root for a
    workspace and the package root for a lone crate — and the two are the same directory often
    enough that guessing wrong is easy to miss. Both are tried, and a path that resolves under
    neither is dropped rather than fabricated.
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve() if candidate.exists() else None
    for root in roots:
        resolved = (root / candidate).resolve()
        if resolved.exists():
            return resolved
    return None


def compiled_sources(
    workdir: Path, package: str, *, marker: Path, package_root: Path | None = None
) -> frozenset[Path] | None:
    """Absolute paths of every source read by the build that compiled ``marker``.

    ``package`` is the cargo package name; its dep-info files are named after the crate, with ``-``
    normalised to ``_``. ``marker`` identifies which feature variant's build to read — see the module
    docstring. ``None`` means no dep-info naming ``marker`` was found, which is the honest answer
    when the build did not happen, was a no-op from a previous session, or wrote somewhere this does
    not look.
    """
    crate = package.replace("-", "_")
    roots = tuple(dict.fromkeys(r for r in (workdir, package_root) if r is not None))
    wanted = marker.resolve()
    found: set[Path] = set()
    seen_marker = False
    for dep_file in sorted((workdir / "target").glob(f"*/deps/{crate}-*.d")) + sorted(
        (workdir / "target").glob(f"*/*/deps/{crate}-*.d")
    ):
        try:
            text = dep_file.read_text()
        except OSError:
            continue
        for sources in _targets(text):
            resolved = {p for raw in sources if (p := _resolve(raw, roots)) is not None}
            if wanted in resolved:
                seen_marker = True
                found |= resolved
    if not seen_marker:
        _log.info("cargo: no dep-info for %s mentions %s", crate, marker)
        return None
    return frozenset(found)
